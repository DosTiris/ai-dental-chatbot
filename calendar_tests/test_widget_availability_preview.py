# calendar_tests/test_widget_availability_preview.py
#
# C2-A.1 — public read-only widget availability preview
# (GET /chat/calendar/availability-preview).
#
# Proves, at the REAL HTTP layer wherever transport behavior is the claim:
#   - calendar_picker_enabled defaults False and parses STRICTLY (only the
#     JSON booleans true/false; strings, numbers, null, containers all read
#     as the fail-safe default);
#   - the preview requires ALL THREE flags explicitly true; every
#     incomplete combination, an unknown client_key, and an inactive tenant
#     return the SAME status and body (the public /chat/config
#     indistinguishability convention) — with the availability owner
#     provably never invoked, including with MALFORMED dates (the
#     raw-string gate-ordering proof);
#   - a fully enabled tenant gets the established 422 contract for invalid
#     dates / reversed ranges / the 31-day inclusive cap, and exactly 31
#     inclusive days is accepted;
#   - the tenant timezone owns every date boundary, including a real
#     DST fall-back day (America/New_York 2026-11-01: a 25-hour local day);
#   - the response vocabulary is pinned to the B1 owner's ALL_DAY_STATES,
#     carries the authoritative earliest/latest bookable-day bounds, none
#     of the forbidden fields, and Cache-Control: no-store;
#   - previews are STRICTLY READ-ONLY: active and expired holds are
#     interpreted by the existing availability rules while every relevant
#     row stays byte-unchanged and no appointment / slot / hold /
#     booking-state / notification write occurs;
#   - the route DELEGATES to the existing build_availability_preview owner
#     (spy wraps the route binding and hands through to the real B1
#     service) rather than duplicating availability computation;
#   - default-off leaves existing behavior unchanged (/chat/config for the
#     same tenant still answers normally while the preview stays 404).
#
# FIXTURES: shared db / client_row / engine from conftest.py UNCHANGED;
# http / office fixtures local to this file (test_availability_preview_route
# pattern).
#
# Run: ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes TEST_DATABASE_URL=... \
#      pytest calendar_tests/test_widget_availability_preview.py -v
# The pure settings/vocabulary tests at the top run without a database.

import sys
import typing
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402

from app.services.availability_preview_service import ALL_DAY_STATES  # noqa: E402
from app.services.calendar_settings_service import (  # noqa: E402
    DEFAULT_CALENDAR_PICKER_ENABLED,
    load_calendar_settings,
)

UTC = ZoneInfo("UTC")
OFFICE_TZ = ZoneInfo("America/New_York")

PREVIEW_PATH = "/chat/calendar/availability-preview"
CONFIG_PATH = "/chat/config"

# The exact locked response contracts under test.
NOT_FOUND_DETAIL = "Client not found"
BLANK_KEY_DETAIL = "client_key is required"
LOCKED_DAY_STATES = {"past", "open", "full", "unavailable"}

# The exact public payload shape (test requirement 14): nothing more may
# ever appear at either level.
ALLOWED_TOP_LEVEL_KEYS = {
    "timezone",
    "requested_start_day",
    "requested_end_day",
    "earliest_bookable_day",
    "latest_bookable_day",
    "days",
}
ALLOWED_DAY_KEYS = {"local_date", "weekday", "state"}

# Forbidden material that must never appear anywhere in a serialized
# public preview body (exact JSON key names, checked as substrings of the
# raw body for defense in depth on top of the exact-key-set assertions).
FORBIDDEN_BODY_SUBSTRINGS = [
    "practice_name",
    "booking_enabled",
    "calendar_actions_enabled",
    "calendar_picker_enabled",
    "slot",            # covers slots / slot_id / slot counts
    "hold",            # covers hold ownership
    "held_until",
    "held_by",
    "client_id",
    "conversation",
    "patient",
    "appointment",
    "selectable",      # derived field deliberately excluded from the DTO
    "generated_at",
]


def _now():
    return datetime.now(UTC)


def _today_local():
    return _now().astimezone(OFFICE_TZ).date()


def _day(days_ahead):
    return (_today_local() + timedelta(days=days_ahead)).isoformat()


class _FakeClient:
    """Bare settings holder for the pure (no-database) settings tests."""

    def __init__(self, calendar):
        self.settings = {"timezone": "America/New_York", "calendar": calendar}
        self.timezone = None


# ===========================================================================
# Pure tests — no database required.
# ===========================================================================

def test_picker_flag_defaults_disabled_when_missing():
    """Req 1: a calendar block with no calendar_picker_enabled key loads as
    disabled, and the module default itself is False."""
    assert DEFAULT_CALENDAR_PICKER_ENABLED is False
    settings = load_calendar_settings(_FakeClient({"booking_enabled": True}))
    assert settings.calendar_picker_enabled is False


@pytest.mark.parametrize(
    "malformed",
    ["true", "True", "false", "yes", "no", "1", "0", 1, 0, 1.0, None,
     [], [True], {}, {"enabled": True}],
    ids=["str_true", "str_True", "str_false", "str_yes", "str_no",
         "str_1", "str_0", "int_1", "int_0", "float_1", "null",
         "empty_list", "list_true", "empty_dict", "dict_true"],
)
def test_picker_flag_rejects_every_non_boolean(malformed):
    """Req 2: strict JSON booleans only — inherited truthiness (bool(1),
    bool("true")) must never enable the public preview."""
    settings = load_calendar_settings(
        _FakeClient({"calendar_picker_enabled": malformed})
    )
    assert settings.calendar_picker_enabled is False


def test_picker_flag_accepts_only_real_booleans():
    """Req 2 (positive side): exactly True enables; exactly False disables."""
    assert load_calendar_settings(
        _FakeClient({"calendar_picker_enabled": True})
    ).calendar_picker_enabled is True
    assert load_calendar_settings(
        _FakeClient({"calendar_picker_enabled": False})
    ).calendar_picker_enabled is False


def test_public_day_state_literal_matches_owner_vocabulary():
    """Req 12 (sync half): the public DTO's Literal is pinned to the B1
    owner's ALL_DAY_STATES — the same pattern as the admin PreviewDay sync
    test, so the vocabularies can never drift apart silently."""
    from app.routes.chat import WidgetPreviewDay

    literal = WidgetPreviewDay.model_fields["state"].annotation
    assert set(typing.get_args(literal)) == ALL_DAY_STATES == LOCKED_DAY_STATES


def test_route_delegates_to_existing_owner_not_a_copy():
    """Req 19 (static half): the route module binds the REAL B1 builder —
    the exact function object from availability_preview_service — so no
    availability computation was duplicated or forked into chat.py."""
    from app.routes import chat as chat_routes
    from app.services import availability_preview_service as owner

    assert chat_routes.build_availability_preview is owner.build_availability_preview


# ===========================================================================
# Database-backed HTTP tests.
# ===========================================================================

PICKER_ON = {
    "booking_enabled": True,
    "calendar_actions_enabled": True,
    "calendar_picker_enabled": True,
    "hold_minutes": 5,
    "minimum_notice_minutes": 60,
    "max_offered_slots": 3,
    "max_booking_days": 30,
    "require_staff_confirmation": True,
}


def _make_office(db, *, practice_name="Picker Dental", calendar=None,
                 active=True, timezone_name="America/New_York"):
    """One office row with an explicit calendar settings block."""
    from app.models import Client

    client = Client(
        id=uuid.uuid4(),
        practice_name=practice_name,
        api_key=f"key-{uuid.uuid4()}",
        active=active,
        settings={
            "timezone": timezone_name,
            "calendar": dict(calendar) if calendar is not None else dict(PICKER_ON),
        },
        notification_email=None,
        notification_phone=None,
    )
    db.add(client)
    db.commit()
    return client


@pytest.fixture()
def http(db):
    """A real FastAPI app containing the CHAT router, driven over HTTP.
    Only get_db is overridden (to the shared test session); tenant lookup
    and feature gating run FOR REAL on every request."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import chat as chat_routes

    app = FastAPI()
    app.include_router(chat_routes.router)
    app.dependency_overrides[chat_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def preview_spy(monkeypatch):
    """Wrap the CHAT-route binding of the preview builder: records every
    invocation, then delegates to the real B1 owner — so spy-using tests
    still exercise the genuine availability pathway end to end."""
    from app.routes import chat as chat_routes
    from app.services.availability_preview_service import (
        build_availability_preview as real_build,
    )

    calls = []

    def spy(db_arg, client_arg, request_arg, now_arg):
        calls.append(request_arg)
        return real_build(db_arg, client_arg, request_arg, now_arg)

    monkeypatch.setattr(chat_routes, "build_availability_preview", spy)
    return calls


@pytest.fixture()
def fixed_clock(monkeypatch):
    """Deterministic route clock (V2 corrections 3-4). Returns set_now():
    pin the exact aware-UTC instant the route's datetime.now(tz) reports.

    Implemented as a datetime SUBCLASS so every other datetime behavior
    (arithmetic, astimezone, strftime) stays real — only now() is pinned,
    and only on the chat route module's imported name. DST tests therefore
    exercise real ZoneInfo transition math against a frozen 'now' and can
    never skip, drift, or flake as wall-clock time advances."""
    from datetime import datetime as real_datetime

    from app.routes import chat as chat_routes

    state = {"now": None}

    class FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            assert state["now"] is not None, "set_now() not called"
            pinned = state["now"]
            return pinned.astimezone(tz) if tz else pinned.replace(tzinfo=None)

    def set_now(instant):
        assert instant.tzinfo is not None, "pin an AWARE instant"
        state["now"] = instant.astimezone(UTC)

    monkeypatch.setattr(chat_routes, "datetime", FrozenDatetime)
    return set_now


def _preview(http, key, start, end, extra_params=None):
    params = {}
    if start is not None:
        params["start_day"] = start
    if end is not None:
        params["end_day"] = end
    if key is not None:
        params["client_key"] = key
    if extra_params:
        params.update(extra_params)
    return http.get(PREVIEW_PATH, params=params)


def _publish_slot(db, client, *, days_ahead=3, hour=10, minute=0,
                  local_day=None):
    """One AVAILABLE published slot at an exact office-local wall time.
    Returns (slot, local_day)."""
    from app.repositories.appointment_repository import create_slot

    if local_day is None:
        local_day = _today_local() + timedelta(days=days_ahead)
    start_utc = datetime(
        local_day.year, local_day.month, local_day.day, hour, minute,
        tzinfo=OFFICE_TZ,
    ).astimezone(UTC)
    slot = create_slot(
        db, client.id, start_utc, start_utc + timedelta(minutes=45),
    )
    db.commit()
    return slot, local_day


def _slot_snapshot(slot):
    """Every persisted field of one slot row, for byte-unchanged proofs."""
    return {
        "id": str(slot.id),
        "client_id": str(slot.client_id),
        "start_datetime": slot.start_datetime,
        "end_datetime": slot.end_datetime,
        "provider_name": slot.provider_name,
        "service_key": slot.service_key,
        "status": slot.status,
        "held_until": slot.held_until,
        "held_by_conversation_id": slot.held_by_conversation_id,
    }


def _table_counts(db):
    from sqlalchemy import func
    from app.calendar_models import Appointment, AppointmentSlot
    from app.models import Conversation, Message

    counts = {
        "appointment_slots": db.query(func.count(AppointmentSlot.id)).scalar(),
        "appointments": db.query(func.count(Appointment.id)).scalar(),
        "conversations": db.query(func.count(Conversation.id)).scalar(),
        "messages": db.query(func.count(Message.id)).scalar(),
    }
    try:
        from app.calendar_models import NotificationAttempt
        counts["notification_attempts"] = db.query(
            func.count(NotificationAttempt.id)
        ).scalar()
    except ImportError:
        pass
    return counts


# --- Gating and indistinguishability -------------------------------------

@requires_db
def test_missing_picker_flag_is_disabled_over_http(http, db):
    """Req 1 (transport half) + Req 20: booking + actions on, picker key
    ABSENT -> the preview 404s while /chat/config for the same tenant
    still answers normally (existing behavior unchanged)."""
    calendar = dict(PICKER_ON)
    del calendar["calendar_picker_enabled"]
    office = _make_office(db, calendar=calendar)

    r = _preview(http, office.api_key, _day(1), _day(7))
    assert r.status_code == 404
    assert r.json() == {"detail": NOT_FOUND_DETAIL}

    config = http.get(CONFIG_PATH, params={"client_key": office.api_key})
    assert config.status_code == 200
    assert config.json()["practice_name"] == office.practice_name


@requires_db
@pytest.mark.parametrize(
    "malformed",
    ["true", 1, None, [], {}],
    ids=["str_true", "int_1", "null", "list", "dict"],
)
def test_malformed_picker_flag_is_disabled_over_http(http, db, malformed):
    """Req 2 (transport half): non-boolean picker values behave as
    disabled at the route, not merely in the settings loader."""
    calendar = dict(PICKER_ON)
    calendar["calendar_picker_enabled"] = malformed
    office = _make_office(db, calendar=calendar)
    r = _preview(http, office.api_key, _day(1), _day(7))
    assert r.status_code == 404
    assert r.json() == {"detail": NOT_FOUND_DETAIL}


@requires_db
@pytest.mark.parametrize(
    "booking,actions,picker",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, False),
        (True, False, True),
        (False, True, True),
    ],
    ids=["none", "booking_only", "actions_only", "picker_only",
         "booking_actions", "booking_picker", "actions_picker"],
)
def test_every_incomplete_flag_combination_is_disabled(
    http, db, booking, actions, picker
):
    """Req 3: all seven non-(true,true,true) combinations are disabled and
    indistinguishable from one another."""
    calendar = dict(PICKER_ON)
    calendar["booking_enabled"] = booking
    calendar["calendar_actions_enabled"] = actions
    calendar["calendar_picker_enabled"] = picker
    office = _make_office(db, calendar=calendar)
    r = _preview(http, office.api_key, _day(1), _day(7))
    assert r.status_code == 404
    assert r.json() == {"detail": NOT_FOUND_DETAIL}


@requires_db
def test_unknown_key_disabled_tenant_and_inactive_tenant_identical(http, db):
    """Req 4: unknown client_key, a disabled tenant, and an INACTIVE fully
    enabled tenant produce byte-identical status and body."""
    disabled = _make_office(
        db, calendar={**PICKER_ON, "calendar_picker_enabled": False}
    )
    inactive = _make_office(db, active=False)

    unknown = _preview(http, f"key-{uuid.uuid4()}", _day(1), _day(7))
    off = _preview(http, disabled.api_key, _day(1), _day(7))
    gone = _preview(http, inactive.api_key, _day(1), _day(7))

    assert unknown.status_code == off.status_code == gone.status_code == 404
    assert unknown.content == off.content == gone.content


@requires_db
def test_blank_client_key_uses_config_convention(http, db):
    """Blank key follows the existing /chat/config convention (400) —
    presence is checked before tenant resolution."""
    r = _preview(http, "   ", _day(1), _day(7))
    assert r.status_code == 400
    assert r.json() == {"detail": BLANK_KEY_DETAIL}


@requires_db
def test_availability_owner_never_invoked_for_rejected_tenants(
    http, db, preview_spy
):
    """Req 5 + gate-ordering proof: unknown key and disabled tenant — even
    with MALFORMED dates — 404 without the availability owner ever being
    invoked. Date semantics are revealed only past the gates."""
    disabled = _make_office(
        db, calendar={**PICKER_ON, "calendar_picker_enabled": False}
    )

    assert _preview(http, f"key-{uuid.uuid4()}", _day(1), _day(7)).status_code == 404
    assert _preview(http, f"key-{uuid.uuid4()}", "not-a-date", "2026").status_code == 404
    assert _preview(http, disabled.api_key, "not-a-date", "2026").status_code == 404
    # V2 correction 2: OMITTED dates reveal no date semantics either — an
    # unknown or disabled tenant with missing start_day/end_day still gets
    # the indistinguishable gated 404, never a framework or contract 422.
    assert _preview(http, f"key-{uuid.uuid4()}", None, None).status_code == 404
    assert _preview(http, disabled.api_key, None, None).status_code == 404
    assert _preview(http, disabled.api_key, _day(1), None).status_code == 404
    assert preview_spy == []


@requires_db
def test_missing_dates_get_established_422_only_when_enabled(
    http, db, preview_spy
):
    """V2 correction 2: an ENABLED tenant omitting start_day, end_day, or
    both receives the established compact 422 from the existing B1 model
    (which owns the missing-value rejection itself) — never a framework
    422 emitted before the route body, and never a service invocation."""
    office = _make_office(db)

    r_start = _preview(http, office.api_key, None, _day(7))
    assert r_start.status_code == 422
    assert isinstance(r_start.json()["detail"], str)
    assert "start_day" in r_start.json()["detail"]

    r_end = _preview(http, office.api_key, _day(1), None)
    assert r_end.status_code == 422
    assert "end_day" in r_end.json()["detail"]

    r_both = _preview(http, office.api_key, None, None)
    assert r_both.status_code == 422
    assert "start_day" in r_both.json()["detail"]
    assert "end_day" in r_both.json()["detail"]

    assert preview_spy == []


@requires_db
def test_omitted_client_key_keeps_config_convention(http, db):
    """client_key stays a REQUIRED framework parameter — the exact
    /chat/config convention (locked by the V2 correction order)."""
    r = http.get(PREVIEW_PATH, params={"start_day": _day(1), "end_day": _day(7)})
    assert r.status_code == 422


# --- Date validation on an enabled tenant ---------------------------------

@requires_db
def test_malformed_dates_get_established_422_contract(http, db):
    """Req 6: enabled tenant + malformed date -> the established compact
    'field: message' 422 detail (the existing single formatter)."""
    office = _make_office(db)
    r = _preview(http, office.api_key, "not-a-date", _day(7))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert "start_day" in detail


@requires_db
def test_start_after_end_rejected(http, db):
    """Req 7."""
    office = _make_office(db)
    r = _preview(http, office.api_key, _day(7), _day(1))
    assert r.status_code == 422
    assert "end_day must not be before start_day" in r.json()["detail"]


@requires_db
def test_thirty_two_inclusive_days_rejected(http, db):
    """Req 8: 32 inclusive days exceeds the 31-day cap."""
    office = _make_office(db)
    r = _preview(http, office.api_key, _day(0), _day(31))
    assert r.status_code == 422
    assert "31" in r.json()["detail"]


@requires_db
def test_exactly_thirty_one_inclusive_days_accepted(http, db):
    """Req 9: exactly 31 inclusive days is the maximum accepted range."""
    office = _make_office(db)
    r = _preview(http, office.api_key, _day(0), _day(30))
    assert r.status_code == 200
    assert len(r.json()["days"]) == 31


# --- Timezone, DST, vocabulary, bounds, headers, shape --------------------

@requires_db
def test_tenant_timezone_owns_date_boundaries(http, db):
    """Req 10: a slot at office-local 9:00 AM (stored as UTC) appears on
    ITS office-local date, and past local dates classify as past."""
    office = _make_office(db)
    _slot, local_day = _publish_slot(db, office, days_ahead=3, hour=9)

    r = _preview(http, office.api_key, _day(-1), _day(7))
    assert r.status_code == 200
    body = r.json()
    assert body["timezone"] == "America/New_York"
    by_date = {d["local_date"]: d for d in body["days"]}
    assert by_date[local_day.isoformat()]["state"] == "open"
    assert by_date[_day(-1)]["state"] == "past"
    assert by_date[local_day.isoformat()]["weekday"] == local_day.strftime("%A")


@requires_db
def test_dst_fall_back_day_window_correct(http, db, fixed_clock):
    """Req 11 (V2 revision — correction 4, PERMANENT): America/New_York
    2026-11-01 is a 25-hour local day (fall back). The route clock is
    PINNED to 2026-10-20 15:00 UTC, so this test exercises real ZoneInfo
    transition math forever — it can never skip or drift as wall-clock
    time advances. Three slots bracket the transition: 00:30 EDT (before),
    09:00 EST (after), and 23:30 EST (an instant whose UTC value falls on
    Nov 2 — the exact case a naive start+24h window misplaces). All three
    must bucket to 2026-11-01; Nov 2 (no rows) stays unavailable."""
    from datetime import date as date_cls, datetime as dt_cls

    fixed_clock(dt_cls(2026, 10, 20, 15, 0, tzinfo=UTC))
    office = _make_office(db, calendar={**PICKER_ON, "max_booking_days": 365})

    dst_day = date_cls(2026, 11, 1)
    _publish_slot(db, office, local_day=dst_day, hour=0, minute=30)   # EDT
    _publish_slot(db, office, local_day=dst_day, hour=9)              # EST
    _publish_slot(db, office, local_day=dst_day, hour=23, minute=30)  # EST,
    # stored UTC instant 2026-11-02T04:30Z — local date still Nov 1.

    r = _preview(http, office.api_key, "2026-10-27", "2026-11-05")
    assert r.status_code == 200
    by_date = {d["local_date"]: d for d in r.json()["days"]}
    assert by_date["2026-11-01"]["state"] == "open"
    assert by_date["2026-11-02"]["state"] == "unavailable"
    assert by_date["2026-10-31"]["state"] in (LOCKED_DAY_STATES - {"open"})
    # The pinned clock also fixes both policy bounds deterministically.
    body = r.json()
    assert body["earliest_bookable_day"] == "2026-10-20"  # 60-min notice
    assert body["latest_bookable_day"] == "2027-10-20"    # today + 365


@requires_db
def test_dst_spring_forward_day_window_correct(http, db, fixed_clock):
    """V2 correction 4 companion: America/New_York 2027-03-14 is a
    23-hour local day (spring forward). Clock pinned to 2027-03-01
    15:00 UTC; a 09:00 EDT slot on the transition day must classify its
    own local date open, permanently."""
    from datetime import date as date_cls, datetime as dt_cls

    fixed_clock(dt_cls(2027, 3, 1, 15, 0, tzinfo=UTC))
    office = _make_office(db, calendar={**PICKER_ON, "max_booking_days": 365})
    _publish_slot(db, office, local_day=date_cls(2027, 3, 14), hour=9)

    r = _preview(http, office.api_key, "2027-03-10", "2027-03-20")
    assert r.status_code == 200
    by_date = {d["local_date"]: d for d in r.json()["days"]}
    assert by_date["2027-03-14"]["state"] == "open"
    assert by_date["2027-03-15"]["state"] == "unavailable"


@requires_db
def test_response_vocabulary_constrained_to_owner_states(http, db):
    """Req 12 (transport half): every emitted state over a mixed window is
    inside ALL_DAY_STATES."""
    office = _make_office(db)
    _publish_slot(db, office, days_ahead=2, hour=10)
    r = _preview(http, office.api_key, _day(-2), _day(20))
    assert r.status_code == 200
    states = {d["state"] for d in r.json()["days"]}
    assert states <= ALL_DAY_STATES
    assert "past" in states and "open" in states


@requires_db
def test_bounds_are_authoritative_booking_window(http, db, fixed_clock):
    """Req 13 (V2 revision — correction 3): earliest = office-local date
    of (now + minimum_notice_minutes), the too_soon boundary; latest =
    office-local today + max_booking_days, the beyond_horizon boundary.
    Pinned to a fixed mid-day clock so the assertion is deterministic at
    any wall-clock time. NO slot is published in this test: the bounds
    are pure policy and must be identical with an empty calendar."""
    from datetime import date as date_cls, datetime as dt_cls

    # 2026-08-05 15:00 EDT — notice 60 min stays inside the local day.
    fixed_clock(dt_cls(2026, 8, 5, 15, 0, tzinfo=OFFICE_TZ))

    office = _make_office(
        db, calendar={**PICKER_ON, "minimum_notice_minutes": 60,
                      "max_booking_days": 12}
    )
    r = _preview(http, office.api_key, "2026-08-05", "2026-08-08")
    assert r.status_code == 200
    body = r.json()
    assert body["earliest_bookable_day"] == "2026-08-05"
    assert body["latest_bookable_day"] == (
        date_cls(2026, 8, 5) + timedelta(days=12)
    ).isoformat()

    today_only = _make_office(
        db, calendar={**PICKER_ON, "minimum_notice_minutes": 60,
                      "max_booking_days": 0}
    )
    r2 = _preview(http, today_only.api_key, "2026-08-05", "2026-08-08")
    assert r2.json()["earliest_bookable_day"] == "2026-08-05"
    assert r2.json()["latest_bookable_day"] == "2026-08-05"


@requires_db
def test_earliest_bound_zero_minimum_notice(http, db, fixed_clock):
    """V2 correction 3: zero notice — earliest is exactly office-local
    today (the cutoff IS now)."""
    from datetime import datetime as dt_cls

    fixed_clock(dt_cls(2026, 8, 5, 15, 0, tzinfo=OFFICE_TZ))
    office = _make_office(
        db, calendar={**PICKER_ON, "minimum_notice_minutes": 0}
    )
    r = _preview(http, office.api_key, "2026-08-05", "2026-08-08")
    assert r.status_code == 200
    assert r.json()["earliest_bookable_day"] == "2026-08-05"


@requires_db
def test_earliest_bound_crosses_local_midnight(http, db, fixed_clock):
    """V2 correction 3: 23:30 EDT + 60-minute notice puts the cutoff at
    00:30 the NEXT office-local day — earliest advances past today while
    the latest bound stays anchored to today's local date."""
    from datetime import datetime as dt_cls

    fixed_clock(dt_cls(2026, 8, 5, 23, 30, tzinfo=OFFICE_TZ))
    office = _make_office(
        db, calendar={**PICKER_ON, "minimum_notice_minutes": 60,
                      "max_booking_days": 12}
    )
    r = _preview(http, office.api_key, "2026-08-05", "2026-08-08")
    assert r.status_code == 200
    body = r.json()
    assert body["earliest_bookable_day"] == "2026-08-06"
    assert body["latest_bookable_day"] == "2026-08-17"  # today(8-05) + 12


@requires_db
def test_earliest_bound_may_exceed_latest_bound(http, db, fixed_clock):
    """V2 correction 3: a 3-day notice with max_booking_days=0 leaves NO
    currently bookable date window — the bounds report that truthfully
    (earliest later than latest) instead of inventing a window. Pure
    policy: computed with an empty calendar, no slot rows consulted."""
    from datetime import datetime as dt_cls

    fixed_clock(dt_cls(2026, 8, 5, 15, 0, tzinfo=OFFICE_TZ))
    office = _make_office(
        db, calendar={**PICKER_ON, "minimum_notice_minutes": 3 * 24 * 60,
                      "max_booking_days": 0}
    )
    r = _preview(http, office.api_key, "2026-08-05", "2026-08-08")
    assert r.status_code == 200
    body = r.json()
    assert body["earliest_bookable_day"] == "2026-08-08"
    assert body["latest_bookable_day"] == "2026-08-05"
    assert body["earliest_bookable_day"] > body["latest_bookable_day"]


@requires_db
def test_response_shape_contains_no_forbidden_fields(http, db):
    """Req 14: exact key sets at both levels, plus a defense-in-depth
    substring sweep of the raw body for every forbidden concept."""
    office = _make_office(db)
    _publish_slot(db, office, days_ahead=2, hour=10)
    r = _preview(http, office.api_key, _day(0), _day(7))
    assert r.status_code == 200
    body = r.json()

    assert set(body.keys()) == ALLOWED_TOP_LEVEL_KEYS
    assert body["days"], "window must contain days"
    for day in body["days"]:
        assert set(day.keys()) == ALLOWED_DAY_KEYS

    raw = r.text.lower()
    for forbidden in FORBIDDEN_BODY_SUBSTRINGS:
        assert forbidden not in raw, f"forbidden concept leaked: {forbidden}"


@requires_db
def test_undeclared_contract_parameters_are_ignored(http, db):
    """selected_day / service_key / slot concepts are OUTSIDE the C2-A.1
    contract: supplying them changes nothing and never emits slots."""
    office = _make_office(db)
    _publish_slot(db, office, days_ahead=2, hour=10)
    plain = _preview(http, office.api_key, _day(0), _day(7))
    decorated = _preview(
        http, office.api_key, _day(0), _day(7),
        extra_params={
            "selected_day": _day(2),
            "service_key": "cleaning_checkup",
            "slot_id": str(uuid.uuid4()),
        },
    )
    assert decorated.status_code == 200
    assert decorated.json() == plain.json()
    assert set(decorated.json().keys()) == ALLOWED_TOP_LEVEL_KEYS


@requires_db
def test_cache_control_no_store(http, db):
    """Req 15: the success response carries Cache-Control: no-store."""
    office = _make_office(db)
    r = _preview(http, office.api_key, _day(0), _day(7))
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"


@requires_db
def test_cache_control_no_store_on_every_intentional_outcome(http, db):
    """V2 correction 1: EVERY response this endpoint intentionally
    produces — blank-key 400, gated 404, contract 422, and the 200 —
    carries the exact Cache-Control: no-store header, so a cached error
    can never mask a later state change."""
    office = _make_office(db)
    disabled = _make_office(
        db, calendar={**PICKER_ON, "calendar_picker_enabled": False}
    )

    r400 = _preview(http, "   ", _day(0), _day(7))
    r404_unknown = _preview(http, f"key-{uuid.uuid4()}", _day(0), _day(7))
    r404_disabled = _preview(http, disabled.api_key, _day(0), _day(7))
    r422_bad = _preview(http, office.api_key, "not-a-date", _day(7))
    r422_missing = _preview(http, office.api_key, None, None)
    r200 = _preview(http, office.api_key, _day(0), _day(7))

    assert r400.status_code == 400
    assert r404_unknown.status_code == 404
    assert r404_disabled.status_code == 404
    assert r422_bad.status_code == 422
    assert r422_missing.status_code == 422
    assert r200.status_code == 200

    for outcome in (r400, r404_unknown, r404_disabled,
                    r422_bad, r422_missing, r200):
        assert outcome.headers.get("cache-control") == "no-store", (
            f"missing no-store on HTTP {outcome.status_code}"
        )


# --- Read-only proofs ------------------------------------------------------

@requires_db
def test_active_hold_interpreted_without_mutation(http, db):
    """Req 16: the only slot of a day actively held by another conversation
    -> 'full' per the existing interpretation, with the row byte-unchanged
    and zero table-count drift."""
    from app.calendar_models import SlotStatus

    office = _make_office(db)
    slot, local_day = _publish_slot(db, office, days_ahead=3, hour=10)
    slot.status = SlotStatus.HELD
    slot.held_until = _now() + timedelta(minutes=5)
    slot.held_by_conversation_id = uuid.uuid4()
    db.add(slot)
    db.commit()

    before = _slot_snapshot(slot)
    counts_before = _table_counts(db)

    r = _preview(http, office.api_key, _day(0), _day(7))
    assert r.status_code == 200
    by_date = {d["local_date"]: d for d in r.json()["days"]}
    assert by_date[local_day.isoformat()]["state"] == "full"

    db.expire_all()
    assert _slot_snapshot(slot) == before
    assert _table_counts(db) == counts_before


@requires_db
def test_expired_hold_interpreted_without_mutation(http, db):
    """Req 17: an EXPIRED hold is interpreted as available (day 'open')
    while held_until / held_by / status stay byte-untouched — read-only
    interpretation, never lazy-reclaim writes."""
    from app.calendar_models import SlotStatus

    office = _make_office(db)
    slot, local_day = _publish_slot(db, office, days_ahead=3, hour=10)
    slot.status = SlotStatus.HELD
    slot.held_until = _now() - timedelta(minutes=10)
    slot.held_by_conversation_id = uuid.uuid4()
    db.add(slot)
    db.commit()

    before = _slot_snapshot(slot)

    r = _preview(http, office.api_key, _day(0), _day(7))
    assert r.status_code == 200
    by_date = {d["local_date"]: d for d in r.json()["days"]}
    assert by_date[local_day.isoformat()]["state"] == "open"

    db.expire_all()
    after = _slot_snapshot(slot)
    assert after == before
    assert after["status"] == SlotStatus.HELD
    assert after["held_until"] is not None


@requires_db
def test_no_write_of_any_kind_occurs(http, db, preview_spy):
    """Req 18 + 19 (runtime half): a successful preview leaves every table
    count unchanged and the session clean, and the spy proves the request
    flowed through the real B1 owner exactly once with the locked-out
    fields forced to None."""
    office = _make_office(db)
    _publish_slot(db, office, days_ahead=2, hour=10)
    counts_before = _table_counts(db)

    r = _preview(http, office.api_key, _day(0), _day(7))
    assert r.status_code == 200

    assert _table_counts(db) == counts_before
    assert not db.dirty and not db.new and not db.deleted

    assert len(preview_spy) == 1
    validated = preview_spy[0]
    assert validated.selected_day is None
    assert validated.service_key is None
