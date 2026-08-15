# calendar_tests/test_portal_recurring_schedule.py
#
# P4-B: the Office Portal RECURRING SCHEDULE management paths (GET config, PUT
# save-only, POST preview, POST apply) proven at the REAL HTTP layer (real
# portal routers, real P2 JWT authentication; only the session dependency is
# overridden - the test_portal_notification_settings.py pattern). The role
# guard is proven directly against require_office_admin (office_admin is the
# only role portal_auth accepts, so the negative branch is unit-exercised).
#
# Proven here (Contract v1.1 s13 + A1 + A2 + Option A/G1-G4):
#   - GET returns the approved config slice (weekly hours + slot_minutes +
#     closures + opaque token); a legacy empty client reads default-safe;
#   - office_admin is required (403 for any other role);
#   - PUT saves config ONLY (never materializes slots): it writes office_hours
#     and settings.calendar.recurring, advances the CAS token from NULL, and a
#     stale token is 409 with NO write (atomic compare-and-set);
#   - A1: a non-UTC-'Z', offset, date-only, or junk expected token is 422 with
#     ZERO write, BEFORE any SQL;
#   - G1: an open weekday window whose span is not a positive exact multiple of
#     slot_minutes (09:00-17:10 @30), or that would exceed MAX_GENERATED_SLOTS,
#     is 422 with no write; an exact multiple and the exactly-MAX window save;
#   - Preview body is EXACTLY {} (extra keys 422); Preview and Apply share the
#     identical horizon (today_local .. +max_booking_days);
#   - G2: pre-first-Save Preview validates the same geometry (bad legacy config
#     -> 422) while GET still surfaces the stored config;
#   - F3: Apply before the first Save is 409 CONFIG_NOT_SAVED;
#   - A2/G3: once the token is non-null, malformed stored config (bad recurring
#     block or bad geometry) is 422 MALFORMED_STORED_CONFIG on GET/Preview/Apply
#     with ZERO mutation (Apply creates no slots);
#   - Apply materializes the horizon (per-day commits), is idempotent on rerun
#     (existing inventory skipped), never blocks a weekly-closed day, and never
#     modifies a BOOKED slot on a closure date;
#   - the outcome vocabulary stays within the approved set (G4: dst_invalid /
#     dst_skipped only - no invalid_skipped / would_invalid);
#   - tenant isolation: office A's save touches only office A's row.
#
# Owner-local PG17 is the sole pass/fail authority (Rule 19). Without
# TEST_DATABASE_URL every test SKIPS visibly (requires_db).

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402

import jwt as pyjwt  # noqa: E402

pytestmark = requires_db

TEST_SECRET = "portal-test-secret-0123456789abcdef0123456789"
TEST_ISSUER = "https://p4b-test-project.supabase.co/auth/v1"
AUDIENCE = "authenticated"

RECURRING_PATH = "/portal/schedule/recurring"
PREVIEW_PATH = "/portal/schedule/recurring/preview"
APPLY_PATH = "/portal/schedule/recurring/apply"

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _token(sub, *, secret=TEST_SECRET, aud=AUDIENCE, exp_delta=300,
           email="office@example.test"):
    import time
    claims = {"sub": str(sub), "aud": aud, "exp": int(time.time()) + exp_delta,
              "email": email, "role": "authenticated", "iss": TEST_ISSUER}
    return pyjwt.encode(claims, secret, algorithm="HS256")


def _closed_week():
    return {wd: {"open": False, "start": None, "end": None} for wd in WEEKDAYS}


def _week_one_open(wd, start, end):
    week = _closed_week()
    week[wd] = {"open": True, "start": start, "end": end}
    return week


@pytest.fixture()
def second_client(db):
    from app.models import Client
    client = Client(id=uuid.uuid4(), practice_name="Other Dental",
                    api_key=f"key-{uuid.uuid4()}", active=True, settings={})
    db.add(client)
    db.commit()
    return client


@pytest.fixture(scope="module")
def office_users_table(engine):
    """Run the REAL migration 007 (sole creation authority for office_users)
    up before this module and down after it."""
    import sqlalchemy
    from calendar_tests.conftest import TEST_DB_URL
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    raw = sqlalchemy.create_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
    with raw.connect() as connection:
        connection.exec_driver_sql(
            (migrations / "007_office_users_up.sql").read_text())
    yield
    with raw.connect() as connection:
        connection.exec_driver_sql(
            (migrations / "007_office_users_down.sql").read_text())


@pytest.fixture()
def office_user_a(db, client_row, office_users_table):
    from app.portal_models import OfficeUser
    row = OfficeUser(auth_user_id=uuid.uuid4(), client_id=client_row.id)
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def portal_http(db, office_user_a, monkeypatch):
    """Real app with the portal auth router AND the P4-B recurring router over
    HTTP; real JWT auth; only the session dependency overridden."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import portal as portal_routes
    from app.routes import portal_recurring_schedule as recurring_routes
    from app.services import portal_auth

    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, TEST_SECRET)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, TEST_ISSUER)
    portal_auth._jwks_clients.clear()

    app = FastAPI()
    app.include_router(portal_routes.router)
    app.include_router(recurring_routes.router)
    app.dependency_overrides[portal_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def _headers(token):
    return {"Authorization": f"Bearer {token}"} if token is not None else {}


def _get(http, token):
    return http.get(RECURRING_PATH, headers=_headers(token))


def _put(http, weekly, slot_minutes, closures, expected, token):
    body = {"weekly_hours": weekly, "slot_minutes": slot_minutes,
            "closures": closures,
            "expected_schedule_config_updated_at": expected}
    return http.put(RECURRING_PATH, json=body, headers=_headers(token))


def _preview(http, token):
    return http.post(PREVIEW_PATH, json={}, headers=_headers(token))


def _apply(http, expected, token):
    return http.post(APPLY_PATH,
                     json={"expected_schedule_config_updated_at": expected},
                     headers=_headers(token))


def _reload(db, client):
    db.expire_all()
    from app.models import Client
    return db.query(Client).filter(Client.id == client.id).one()


# ---------------------------------------------------------------------------
# Role guard (unit): office_admin required
# ---------------------------------------------------------------------------

class _FakeOfficeUser:
    def __init__(self, role):
        self.role = role


class _FakeIdentity:
    """Minimal identity for the guard: require_office_admin reads
    identity.office_user.role (the actual attribute), nothing else."""
    def __init__(self, role, client=None):
        self.office_user = _FakeOfficeUser(role)
        self.client = client


def test_require_office_admin_rejects_non_admin():
    from fastapi import HTTPException
    from app.services import portal_recurring_schedule_service as svc
    # Any role other than office_admin is rejected (V1 has a single role, so a
    # sentinel non-admin string exercises the != OFFICE_ADMIN branch).
    with pytest.raises(HTTPException) as exc:
        svc.require_office_admin(_FakeIdentity("not-admin"))
    assert exc.value.status_code == 403


def test_require_office_admin_allows_admin():
    from app.portal_models import OfficeUserRole
    from app.services import portal_recurring_schedule_service as svc
    svc.require_office_admin(_FakeIdentity(OfficeUserRole.OFFICE_ADMIN))  # no raise


def test_all_four_service_entrypoints_require_office_admin():
    """The guard runs FIRST on every endpoint: a non-admin identity is 403 on
    get/put/preview/apply before any DB work (db=None proves the guard fires
    before the session is touched)."""
    from fastapi import HTTPException
    from app.services import portal_recurring_schedule_service as svc
    ident = _FakeIdentity("not-admin")
    for call in (
        lambda: svc.get_recurring_config(ident),
        lambda: svc.put_recurring_config(None, ident, {}, 30, [], None),
        lambda: svc.preview_recurring_config(None, ident),
        lambda: svc.apply_recurring_config(None, ident, None),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# GET: default-safe read
# ---------------------------------------------------------------------------

def test_get_returns_default_config_for_empty_client(
        portal_http, db, client_row, office_user_a):
    """A legacy client with empty office_hours/settings reads a default-safe
    config: 7 closed weekdays, the default slot length, no closures, null token."""
    response = _get(portal_http, _token(office_user_a.auth_user_id))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {
        "weekly_hours", "slot_minutes", "closures", "schedule_config_updated_at"}
    assert set(payload["weekly_hours"].keys()) == set(WEEKDAYS)
    assert payload["closures"] == []
    assert payload["schedule_config_updated_at"] is None
    for forbidden in ("client_id", "id", "api_key", "settings"):
        assert forbidden not in payload


# ---------------------------------------------------------------------------
# PUT: save-only + CAS token + tenant isolation
# ---------------------------------------------------------------------------

def test_put_saves_config_only_and_advances_token(
        portal_http, db, client_row, office_user_a):
    token = _token(office_user_a.auth_user_id)
    week = _week_one_open("mon", "09:00", "17:00")
    response = _put(portal_http, week, 30, [{"date": "2026-12-25"}], None, token)
    assert response.status_code == 200
    payload = response.json()
    assert payload["schedule_config_updated_at"] is not None   # token advanced
    assert payload["slot_minutes"] == 30
    # The save wrote the EXISTING JSONB columns (bookability authority is the
    # appointment_slots table, untouched here - save never materializes slots).
    reloaded = _reload(db, client_row)
    assert reloaded.office_hours["mon"]["open"] is True
    assert reloaded.settings["calendar"]["recurring"]["slot_minutes"] == 30
    assert reloaded.settings["calendar"]["recurring"]["closures"] == [
        {"date": "2026-12-25"}]
    assert reloaded.schedule_config_updated_at is not None


def test_put_stale_token_is_conflict_with_no_write(
        portal_http, db, client_row, office_user_a):
    from app.services.portal_recurring_schedule_service import STALE_CONFIG_DETAIL
    token = _token(office_user_a.auth_user_id)
    first = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30, [],
                 None, token)
    assert first.status_code == 200
    saved_token = first.json()["schedule_config_updated_at"]
    # A stale (null) expected token now loses the compare-and-set.
    stale = _put(portal_http, _week_one_open("tue", "09:00", "17:00"), 60, [],
                 None, token)
    assert stale.status_code == 409
    assert stale.json()["detail"] == STALE_CONFIG_DETAIL
    # No write: the saved token is unchanged and Monday is still the open day.
    reloaded = _reload(db, client_row)
    assert reloaded.office_hours["mon"]["open"] is True
    assert reloaded.office_hours["tue"]["open"] is False
    # A fresh retry with the correct token then succeeds.
    ok = _put(portal_http, _week_one_open("tue", "09:00", "17:00"), 60, [],
              saved_token, token)
    assert ok.status_code == 200


def test_put_is_tenant_isolated(
        portal_http, db, client_row, second_client, office_user_a):
    before = _reload(db, second_client)
    assert before.schedule_config_updated_at is None
    resp = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30, [],
                None, _token(office_user_a.auth_user_id))
    assert resp.status_code == 200
    after = _reload(db, second_client)
    assert after.schedule_config_updated_at is None        # office B untouched
    assert after.office_hours is None or "mon" not in (after.office_hours or {})


# ---------------------------------------------------------------------------
# A1: strict wire-token validation before SQL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_token", [
    "2026-08-14T12:00:00+00:00",     # offset form, not the 'Z' designator
    "2026-08-14T12:00:00",           # missing designator
    "2026-08-14",                    # date only
    "2026-13-14T12:00:00Z",          # impossible month
    "garbage",                       # junk
])
def test_put_rejects_malformed_expected_token_before_write(
        portal_http, db, client_row, office_user_a, bad_token):
    resp = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30, [],
                bad_token, _token(office_user_a.auth_user_id))
    assert resp.status_code == 422                          # A1 INVALID_CONFIG
    reloaded = _reload(db, client_row)
    assert reloaded.schedule_config_updated_at is None      # zero write


# ---------------------------------------------------------------------------
# G1: open-weekday geometry validation at PUT time
# ---------------------------------------------------------------------------

def test_put_rejects_non_multiple_window(
        portal_http, db, client_row, office_user_a):
    """09:00-17:10 @30 -> span 490 not divisible by 30 -> 422, zero write."""
    resp = _put(portal_http, _week_one_open("mon", "09:00", "17:10"), 30, [],
                None, _token(office_user_a.auth_user_id))
    assert resp.status_code == 422
    assert _reload(db, client_row).schedule_config_updated_at is None


def test_put_accepts_exact_multiple_window(
        portal_http, db, client_row, office_user_a):
    resp = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30, [],
                None, _token(office_user_a.auth_user_id))
    assert resp.status_code == 200                          # 480/30 = 16 slots


def test_put_boundary_max_generated_slots(
        portal_http, db, client_row, office_user_a):
    """Exactly MAX_GENERATED_SLOTS saves; MAX+1 is 422. slot_minutes=10 so
    MAX slots span MAX*10 minutes from 06:00 (within a single local day)."""
    from app.services.portal_recurring_schedule_service import MAX_GENERATED_SLOTS
    token = _token(office_user_a.auth_user_id)
    total_min = MAX_GENERATED_SLOTS * 10
    end_max = (datetime(2026, 1, 1, 6, 0) + timedelta(minutes=total_min)).strftime("%H:%M")
    end_over = (datetime(2026, 1, 1, 6, 0) + timedelta(minutes=total_min + 10)).strftime("%H:%M")

    ok = _put(portal_http, _week_one_open("mon", "06:00", end_max), 10, [],
              None, token)
    assert ok.status_code == 200                            # exactly MAX
    saved = ok.json()["schedule_config_updated_at"]

    over = _put(portal_http, _week_one_open("mon", "06:00", end_over), 10, [],
                saved, token)
    assert over.status_code == 422                          # MAX + 1


def test_put_rejects_bool_slot_minutes(
        portal_http, db, client_row, office_user_a):
    body = {"weekly_hours": _week_one_open("mon", "09:00", "17:00"),
            "slot_minutes": True, "closures": [],
            "expected_schedule_config_updated_at": None}
    resp = portal_http.put(RECURRING_PATH, json=body,
                           headers=_headers(_token(office_user_a.auth_user_id)))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Preview: body {} + horizon parity + G2
# ---------------------------------------------------------------------------

def test_preview_body_must_be_empty(portal_http, office_user_a):
    resp = portal_http.post(PREVIEW_PATH, json={"unexpected": 1},
                            headers=_headers(_token(office_user_a.auth_user_id)))
    assert resp.status_code == 422                          # extra="forbid"


def test_preview_and_apply_share_the_same_horizon(
        portal_http, db, client_row, office_user_a):
    token = _token(office_user_a.auth_user_id)
    save = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30, [],
                None, token)
    saved = save.json()["schedule_config_updated_at"]
    preview = _preview(portal_http, token).json()
    apply = _apply(portal_http, saved, token).json()
    assert preview["start_day"] == apply["start_day"]
    assert preview["end_day"] == apply["end_day"]


def test_preview_pre_first_save_validates_geometry_but_get_surfaces(
        portal_http, db, client_row, office_user_a):
    """G2: with NO prior Save (token null) a legacy bad-geometry office_hours
    makes Preview 422, yet GET still surfaces the stored config (read path is
    tolerant; only expansion is gated)."""
    client_row.office_hours = {**_closed_week(),
                               "mon": {"open": True, "start": "09:00", "end": "17:10"}}
    client_row.settings = {"calendar": {"recurring": {"slot_minutes": 30,
                                                      "closures": []}}}
    db.commit()
    token = _token(office_user_a.auth_user_id)
    assert _get(portal_http, token).status_code == 200      # GET tolerant
    assert _preview(portal_http, token).status_code == 422  # G2 gate


# ---------------------------------------------------------------------------
# F3: first Save required before Apply
# ---------------------------------------------------------------------------

def test_apply_before_first_save_is_config_not_saved(
        portal_http, db, client_row, office_user_a):
    from app.services.portal_recurring_schedule_service import CONFIG_NOT_SAVED_DETAIL
    resp = _apply(portal_http, None, _token(office_user_a.auth_user_id))
    assert resp.status_code == 409
    assert resp.json()["detail"] == CONFIG_NOT_SAVED_DETAIL


# ---------------------------------------------------------------------------
# A2/G3: malformed stored config fail-closed once the token is non-null
# ---------------------------------------------------------------------------

def _count_slots(db, client_id):
    from app.calendar_models import AppointmentSlot
    return db.query(AppointmentSlot).filter(
        AppointmentSlot.client_id == client_id).count()


def _canonical_week(open_day="mon", start="09:00", end="17:00"):
    """The canonical DB form the writer produces: open -> {open,start,end};
    closed -> EXACTLY {open:false}."""
    week = {wd: {"open": False} for wd in WEEKDAYS}
    week[open_day] = {"open": True, "start": start, "end": end}
    return week


def _apply_corruption(fresh, corrupt):
    """Mutate exactly ONE aspect of the canonical stored state; the untouched
    aspect keeps the canonical form the first Save wrote."""
    if corrupt == "recurring_missing":
        fresh.settings = {"calendar": {}}
    elif corrupt == "slot_minutes_missing":
        fresh.settings = {"calendar": {"recurring": {"closures": []}}}
    elif corrupt == "slot_minutes_invalid":
        fresh.settings = {"calendar": {"recurring": {"slot_minutes": 7, "closures": []}}}
    elif corrupt == "closures_missing":
        fresh.settings = {"calendar": {"recurring": {"slot_minutes": 30}}}
    elif corrupt == "closures_malformed":
        fresh.settings = {"calendar": {"recurring": {"slot_minutes": 30, "closures": "nope"}}}
    elif corrupt == "recurring_extra_key":
        # T1: an EXTRA key alongside the two required ones is malformed too.
        fresh.settings = {"calendar": {"recurring": {"slot_minutes": 30,
                                                     "closures": [],
                                                     "unexpected": 1}}}
    elif corrupt == "office_hours_missing":
        fresh.office_hours = None
    elif corrupt == "missing_weekday":
        wk = _canonical_week(); del wk["sun"]; fresh.office_hours = wk
    elif corrupt == "malformed_open_weekday":
        wk = _canonical_week(); wk["mon"] = {"open": True, "start": "9am", "end": "17:00"}
        fresh.office_hours = wk
    elif corrupt == "malformed_closed_weekday":
        wk = _canonical_week(); wk["sun"] = {"open": False, "start": None}
        fresh.office_hours = wk
    else:
        raise AssertionError("unknown corruption: " + corrupt)


@pytest.mark.parametrize("corrupt", [
    "recurring_missing", "slot_minutes_missing", "slot_minutes_invalid",
    "closures_missing", "closures_malformed", "recurring_extra_key",
    "office_hours_missing", "missing_weekday", "malformed_open_weekday",
    "malformed_closed_weekday",
])
def test_malformed_stored_config_fails_closed_after_first_save(
        portal_http, db, client_row, office_user_a, corrupt):
    """F1: once the token is non-NULL, EVERY corruption of application-owned
    state is 422 MALFORMED_STORED_CONFIG on GET/Preview/Apply, and Apply
    mutates ZERO slots - the reader never defaults or normalizes it away."""
    from app.services.portal_recurring_schedule_service import (
        MALFORMED_STORED_CONFIG_DETAIL)
    token = _token(office_user_a.auth_user_id)
    save = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30, [],
                None, token)
    saved = save.json()["schedule_config_updated_at"]
    assert saved is not None
    slots_before = _count_slots(db, client_row.id)
    fresh = _reload(db, client_row)
    _apply_corruption(fresh, corrupt)
    db.commit()
    for response in (_get(portal_http, token),
                     _preview(portal_http, token),
                     _apply(portal_http, saved, token)):
        assert response.status_code == 422, corrupt
        assert response.json()["detail"] == MALFORMED_STORED_CONFIG_DETAIL
    assert _count_slots(db, client_row.id) == slots_before   # zero mutation


# ---------------------------------------------------------------------------
# Apply: materialize, idempotent, weekly-closed, closures, G4 vocabulary
# ---------------------------------------------------------------------------

def test_apply_materializes_and_is_idempotent(
        portal_http, db, client_row, office_user_a):
    token = _token(office_user_a.auth_user_id)
    save = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30, [],
                None, token)
    saved = save.json()["schedule_config_updated_at"]
    first = _apply(portal_http, saved, token)
    assert first.status_code == 200
    created = _count_slots(db, client_row.id)
    assert created > 0                                      # slots materialized
    # Rerun over the same horizon skips days that already have inventory.
    second = _apply(portal_http, saved, token)
    assert second.status_code == 200
    assert second.json()["totals"]["existing_inventory_skipped_days"] >= 1
    assert _count_slots(db, client_row.id) == created      # idempotent: no dups


def test_apply_never_blocks_a_weekly_closed_day(
        portal_http, db, client_row, office_user_a):
    """A weekly-closed weekday yields the weekly_closed outcome and never a
    block/publish - closing the week is not the same as a closure."""
    token = _token(office_user_a.auth_user_id)
    save = _put(portal_http, _closed_week(), 30, [], None, token)
    saved = save.json()["schedule_config_updated_at"]
    result = _apply(portal_http, saved, token).json()
    outcomes = {d["outcome"] for d in result["days"]}
    assert outcomes <= {"weekly_closed"}
    assert result["totals"]["published_days"] == 0
    assert result["totals"]["closure_blocked_days"] == 0


def test_apply_outcomes_stay_in_approved_vocabulary(
        portal_http, db, client_row, office_user_a):
    """G4: every reported day outcome is in the approved set; no new
    invalid_skipped / would_invalid values are ever emitted."""
    approved = {"published", "existing_inventory_skipped", "dst_skipped",
                "closure_blocked", "closure_empty", "weekly_closed"}
    token = _token(office_user_a.auth_user_id)
    save = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30,
                [{"date": "2026-12-25"}], None, token)
    saved = save.json()["schedule_config_updated_at"]
    result = _apply(portal_http, saved, token).json()
    for day in result["days"]:
        assert day["outcome"] in approved


def test_apply_stale_token_is_conflict(
        portal_http, db, client_row, office_user_a):
    from app.services.portal_recurring_schedule_service import STALE_CONFIG_DETAIL
    token = _token(office_user_a.auth_user_id)
    save = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30, [],
                None, token)
    saved = save.json()["schedule_config_updated_at"]
    # Save again to advance the token, making `saved` stale.
    _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 60, [], saved, token)
    resp = _apply(portal_http, saved, token)
    assert resp.status_code == 409
    assert resp.json()["detail"] == STALE_CONFIG_DETAIL


# ---------------------------------------------------------------------------
# CAS token strictness, settings preservation, C1 inventory, closure removal
# ---------------------------------------------------------------------------

def _parse_wire_instant(wire):
    """Parse the A1 wire token (UTC 'Z' designator) into an aware UTC
    instant. Compare parsed INSTANTS, never lexical strings - the token
    omits trailing-zero fractional digits, so a string compare would be
    wrong even when the instant strictly advanced."""
    return datetime.fromisoformat(
        wire.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_token_strictly_advances_under_frozen_clock(
        portal_http, db, client_row, office_user_a, monkeypatch):
    """The minted token is STRICTLY newer than the one it replaces even when the
    clock does not advance (frozen/equal). Compares parsed INSTANTS, not wire
    strings (the fractional-seconds omission would break a string compare)."""
    from datetime import datetime as _dt, timezone as _tz
    from app.services import portal_recurring_schedule_service as svc
    frozen = _dt(2026, 8, 14, 12, 0, 0, tzinfo=_tz.utc)
    monkeypatch.setattr(svc, "_now_utc", lambda: frozen)
    token = _token(office_user_a.auth_user_id)
    s1 = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30, [],
              None, token).json()["schedule_config_updated_at"]
    s2 = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 60, [],
              s1, token).json()["schedule_config_updated_at"]
    assert _parse_wire_instant(s2) > _parse_wire_instant(s1)


def test_put_preserves_unrelated_settings_semantically(
        portal_http, db, client_row, office_user_a):
    """PUT rewrites ONLY settings.calendar.recurring; every other settings and
    settings.calendar value survives BY VALUE (F7)."""
    client_row.settings = {"booking_enabled": True, "timezone": "America/New_York",
                           "calendar": {"max_booking_days": 30, "keep_me": "yes"}}
    db.commit()
    token = _token(office_user_a.auth_user_id)
    resp = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30, [],
                None, token)
    assert resp.status_code == 200
    reloaded = _reload(db, client_row)
    assert reloaded.settings["booking_enabled"] is True
    assert reloaded.settings["timezone"] == "America/New_York"
    assert reloaded.settings["calendar"]["max_booking_days"] == 30
    assert reloaded.settings["calendar"]["keep_me"] == "yes"
    assert reloaded.settings["calendar"]["recurring"]["slot_minutes"] == 30


def test_weekly_closed_apply_leaves_existing_inventory_untouched(
        portal_http, db, client_row, office_user_a):
    """C1: making a weekday weekly-closed does NOT block/remove inventory a prior
    Apply created there; the day is reported weekly_closed and the slots remain."""
    token = _token(office_user_a.auth_user_id)
    s1 = _put(portal_http, _week_one_open("mon", "09:00", "17:00"), 30, [],
              None, token).json()["schedule_config_updated_at"]
    _apply(portal_http, s1, token)
    created = _count_slots(db, client_row.id)
    assert created > 0
    s2 = _put(portal_http, _closed_week(), 30, [], s1, token).json()[
        "schedule_config_updated_at"]
    result = _apply(portal_http, s2, token).json()
    assert result["totals"]["published_days"] == 0
    assert result["totals"]["closure_blocked_days"] == 0
    assert _count_slots(db, client_row.id) == created   # untouched


def test_closure_removal_does_not_auto_unblock(
        portal_http, db, client_row, office_user_a):
    """Closure removal only edits config: slots a previous Apply blocked for the
    closed dates STAY blocked (a later Apply never unblocks)."""
    from app.calendar_models import AppointmentSlot, SlotStatus
    token = _token(office_user_a.auth_user_id)
    open_week = {wd: {"open": True, "start": "09:00", "end": "17:00"}
                 for wd in WEEKDAYS}
    s1 = _put(portal_http, open_week, 30, [], None, token).json()[
        "schedule_config_updated_at"]
    _apply(portal_http, s1, token)

    def blocked_count():
        db.expire_all()
        return db.query(AppointmentSlot).filter(
            AppointmentSlot.client_id == client_row.id,
            AppointmentSlot.status == SlotStatus.BLOCKED).count()

    assert blocked_count() == 0
    # Close the entire horizon (span = max_booking_days) and Apply -> blocks.
    horizon = _preview(portal_http, token).json()
    closure = [{"start": horizon["start_day"], "end": horizon["end_day"]}]
    s2 = _put(portal_http, open_week, 30, closure, s1, token).json()[
        "schedule_config_updated_at"]
    _apply(portal_http, s2, token)
    after_block = blocked_count()
    assert after_block > 0

    # Remove the closure and Apply again -> the blocked slots stay blocked.
    s3 = _put(portal_http, open_week, 30, [], s2, token).json()[
        "schedule_config_updated_at"]
    _apply(portal_http, s3, token)
    assert blocked_count() >= after_block


# ===========================================================================
# R3 - Contract v1.1 PG17 concurrency / safety matrix (owner-local PG17 is the
# sole authority; these SKIP without TEST_DATABASE_URL). These are REAL tests
# (real primitives, real second connections), not parametrized case inflation.
# ===========================================================================

def _client_tz(client):
    from app.services import calendar_settings_service
    return calendar_settings_service.load_calendar_settings(client).timezone_name


def _all_open_week():
    return {wd: {"open": True, "start": "09:00", "end": "17:00"} for wd in WEEKDAYS}


def _horizon_bounds(portal_http, token):
    from datetime import date as _date
    hz = _preview(portal_http, token).json()
    return _date.fromisoformat(hz["start_day"]), _date.fromisoformat(hz["end_day"])


def _mid_horizon_date(portal_http, token, offset):
    start, end = _horizon_bounds(portal_http, token)
    day = start + timedelta(days=offset)
    assert day <= end, "test offset fell outside the horizon"
    return day


def _window_utc(day, start_hhmm, end_hhmm, tz):
    from app.services import portal_schedule_service as ss
    exp = ss.expand_publish_slots(day, start_hhmm, end_hhmm, 30, tz)
    assert not isinstance(exp, ss.PublishResult), "expected a valid seeding window"
    return exp[0]


def _seed_slot(db, client_id, start_utc, end_utc, status, held_until=None):
    import uuid as _uuid
    from app.calendar_models import AppointmentSlot
    slot = AppointmentSlot(id=_uuid.uuid4(), client_id=client_id,
                           start_datetime=start_utc, end_datetime=end_utc,
                           status=status, held_until=held_until)
    db.add(slot)
    db.commit()
    return slot


def _seed_slot_on(db, client, day, start_hhmm, end_hhmm, status, tz, held_until=None):
    s_utc, e_utc = _window_utc(day, start_hhmm, end_hhmm, tz)
    return _seed_slot(db, client.id, s_utc, e_utc, status, held_until)


def _day_outcome(apply_or_preview_json, day):
    iso = day.isoformat()
    for d in apply_or_preview_json["days"]:
        if d["day"] == iso:
            return d
    return None


def _second_session():
    from app.database import SessionLocal
    return SessionLocal()


def _lock_is_free(session, client_id, day):
    """A non-blocking probe from a SEPARATE connection: True means the per-
    (tenant, local-day) advisory lock is currently free (released)."""
    from sqlalchemy import text as _sql
    from app.services import portal_schedule_service as ss
    material = ss.build_schedule_lock_material(client_id, day)
    got = session.execute(
        _sql("SELECT pg_try_advisory_xact_lock(hashtextextended(:m, 0))"),
        {"m": material}).scalar()
    session.rollback()   # release anything acquired; end the probe transaction
    return bool(got)


# --- Closure / booking safety --------------------------------------------

def test_closure_blocks_available_slot(portal_http, db, client_row, office_user_a):
    from app.calendar_models import AppointmentSlot, SlotStatus
    token = _token(office_user_a.auth_user_id); tz = _client_tz(client_row)
    s1 = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    day = _mid_horizon_date(portal_http, token, 3)
    slot = _seed_slot_on(db, client_row, day, "09:00", "09:30",
                         SlotStatus.AVAILABLE, tz)
    s2 = _put(portal_http, _all_open_week(), 30, [{"date": day.isoformat()}],
              s1, token).json()["schedule_config_updated_at"]
    _apply(portal_http, s2, token)
    db.expire_all()
    assert db.query(AppointmentSlot).filter(
        AppointmentSlot.id == slot.id).one().status == SlotStatus.BLOCKED


def test_closure_blocks_active_held_slot(portal_http, db, client_row, office_user_a):
    from app.calendar_models import AppointmentSlot, SlotStatus
    token = _token(office_user_a.auth_user_id); tz = _client_tz(client_row)
    s1 = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    day = _mid_horizon_date(portal_http, token, 3)
    held_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    slot = _seed_slot_on(db, client_row, day, "09:00", "09:30",
                         SlotStatus.HELD, tz, held_until=held_until)
    s2 = _put(portal_http, _all_open_week(), 30, [{"date": day.isoformat()}],
              s1, token).json()["schedule_config_updated_at"]
    _apply(portal_http, s2, token)
    db.expire_all()
    # The bulk block outranks the hold (D4): the affected patient's finalize
    # then fails safe as hold_lost on the frozen booking path (owner-local).
    assert db.query(AppointmentSlot).filter(
        AppointmentSlot.id == slot.id).one().status == SlotStatus.BLOCKED


def test_closure_preserves_booked_and_reports_it(portal_http, db, client_row, office_user_a):
    from app.calendar_models import AppointmentSlot, SlotStatus
    token = _token(office_user_a.auth_user_id); tz = _client_tz(client_row)
    s1 = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    day = _mid_horizon_date(portal_http, token, 3)
    slot = _seed_slot_on(db, client_row, day, "09:00", "09:30",
                         SlotStatus.BOOKED, tz)
    s2 = _put(portal_http, _all_open_week(), 30, [{"date": day.isoformat()}],
              s1, token).json()["schedule_config_updated_at"]
    result = _apply(portal_http, s2, token).json()
    day_out = _day_outcome(result, day)
    assert day_out["outcome"] == "closure_blocked"
    assert len(day_out.get("booked_remaining", [])) == 1        # reported
    db.expire_all()
    assert db.query(AppointmentSlot).filter(
        AppointmentSlot.id == slot.id).one().status == SlotStatus.BOOKED   # untouched


def test_repeated_closure_apply_is_idempotent(portal_http, db, client_row, office_user_a):
    from app.calendar_models import AppointmentSlot, SlotStatus
    token = _token(office_user_a.auth_user_id); tz = _client_tz(client_row)
    s1 = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    day = _mid_horizon_date(portal_http, token, 3)
    _seed_slot_on(db, client_row, day, "09:00", "09:30", SlotStatus.AVAILABLE, tz)
    s2 = _put(portal_http, _all_open_week(), 30, [{"date": day.isoformat()}],
              s1, token).json()["schedule_config_updated_at"]

    def blocked_count():
        db.expire_all()
        return db.query(AppointmentSlot).filter(
            AppointmentSlot.client_id == client_row.id,
            AppointmentSlot.status == SlotStatus.BLOCKED).count()

    _apply(portal_http, s2, token)
    after1 = blocked_count()
    assert after1 > 0
    _apply(portal_http, s2, token)     # repeat -> nothing new to block
    assert blocked_count() == after1   # idempotent (blocked_count delta 0)


# --- C2 day-wide inventory (any non-cancelled slot -> whole-day skip) ------

def _c2_skip_case(portal_http, db, client_row, office_user_a, seed_hhmm, status):
    from app.calendar_models import SlotStatus  # noqa: F401
    token = _token(office_user_a.auth_user_id); tz = _client_tz(client_row)
    s1 = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    day = _mid_horizon_date(portal_http, token, 3)
    _seed_slot_on(db, client_row, day, seed_hhmm[0], seed_hhmm[1], status, tz)
    result = _apply(portal_http, s1, token).json()
    assert _day_outcome(result, day)["outcome"] == "existing_inventory_skipped"


def test_open_day_overlapping_available_slot_skips_whole_day(
        portal_http, db, client_row, office_user_a):
    from app.calendar_models import SlotStatus
    _c2_skip_case(portal_http, db, client_row, office_user_a,
                  ("09:00", "09:30"), SlotStatus.AVAILABLE)


def test_open_day_nonoverlapping_outside_window_slot_skips_whole_day(
        portal_http, db, client_row, office_user_a):
    from app.calendar_models import SlotStatus
    # 18:00 is OUTSIDE the 09:00-17:00 recurring window but on the SAME local
    # day: any non-cancelled inventory that day forces the whole-day skip.
    _c2_skip_case(portal_http, db, client_row, office_user_a,
                  ("18:00", "18:30"), SlotStatus.AVAILABLE)


def test_open_day_booked_slot_skips_whole_day(
        portal_http, db, client_row, office_user_a):
    from app.calendar_models import SlotStatus
    _c2_skip_case(portal_http, db, client_row, office_user_a,
                  ("09:00", "09:30"), SlotStatus.BOOKED)


def test_open_day_blocked_slot_skips_whole_day(
        portal_http, db, client_row, office_user_a):
    from app.calendar_models import SlotStatus
    _c2_skip_case(portal_http, db, client_row, office_user_a,
                  ("09:00", "09:30"), SlotStatus.BLOCKED)


# --- C2 concurrency: advisory-lock serialization, single materialization --

def test_advisory_lock_serializes_same_tenant_day(db, client_row, office_user_a):
    """A second connection CANNOT take the per-(tenant, day) lock while the
    first transaction holds it, and CAN once it is released - the serialization
    the P4-A publish path relies on (no threads, no hang: a non-blocking probe)."""
    from datetime import date as _date
    from app.services import portal_schedule_service as ss
    day = _date.today()
    ss.acquire_schedule_day_lock(db, client_row.id, day)   # held in db's txn
    other = _second_session()
    try:
        assert _lock_is_free(other, client_row.id, day) is False   # serialized
    finally:
        other.close()
    db.rollback()   # release the first holder
    other2 = _second_session()
    try:
        assert _lock_is_free(other2, client_row.id, day) is True    # now free
    finally:
        other2.close()


def test_apply_then_manual_publish_same_day_no_duplicates(
        portal_http, db, client_row, office_user_a):
    """A P4-A manual publish on a day P4-B Apply already materialized sees the
    existing inventory -> PUBLISH_OVERLAP, zero inserts: exactly one
    materialization, no duplicate/gap-fill/extension."""
    from app.services import portal_schedule_service as ss
    from app.services import calendar_settings_service
    token = _token(office_user_a.auth_user_id)
    s1 = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    _apply(portal_http, s1, token)
    day = _mid_horizon_date(portal_http, token, 3)
    after_apply = _count_slots(db, client_row.id)
    settings = calendar_settings_service.load_calendar_settings(client_row)
    result = ss.publish_day_slots(db, client_row.id, settings, day,
                                  "09:00", "17:00", 30)
    assert result.reason == ss.PUBLISH_OVERLAP
    assert _count_slots(db, client_row.id) == after_apply     # no duplicates


# --- F5 lock termination: the lock is released after each outcome ----------

def test_apply_releases_day_lock_after_publish_and_after_skip(
        portal_http, db, client_row, office_user_a):
    from app.calendar_models import SlotStatus
    token = _token(office_user_a.auth_user_id); tz = _client_tz(client_row)
    s1 = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    skip_day = _mid_horizon_date(portal_http, token, 3)
    publish_day = _mid_horizon_date(portal_http, token, 5)
    _seed_slot_on(db, client_row, skip_day, "09:00", "09:30",
                  SlotStatus.AVAILABLE, tz)
    result = _apply(portal_http, s1, token).json()
    assert _day_outcome(result, skip_day)["outcome"] == "existing_inventory_skipped"
    assert _day_outcome(result, publish_day)["outcome"] == "published"
    other = _second_session()
    try:
        assert _lock_is_free(other, client_row.id, skip_day) is True     # released
        assert _lock_is_free(other, client_row.id, publish_day) is True  # released
    finally:
        other.close()


def test_apply_open_day_dst_invalid_releases_lock_and_reports_dst_skipped(
        portal_http, db, client_row, office_user_a):
    """Directly exercise _apply_open_day on a spring-forward window: the P4-A
    expander refuses (PUBLISH_INVALID), the day is dst_skipped, and the advisory
    lock is released (a second connection can take it)."""
    import types
    from datetime import date as _date
    from app.services import portal_recurring_schedule_service as svc
    from app.services import calendar_settings_service
    settings = calendar_settings_service.load_calendar_settings(client_row)  # tz NY
    snap = types.SimpleNamespace(settings=settings, slot_minutes=30)
    day = _date(2026, 3, 8)   # America/New_York spring-forward (02:00->03:00 gap)
    outcome = svc._apply_open_day(db, client_row.id, snap, day,
                                  {"open": True, "start": "02:00", "end": "02:30"})
    assert outcome["outcome"] == "dst_skipped"
    other = _second_session()
    try:
        assert _lock_is_free(other, client_row.id, day) is True
    finally:
        other.close()


# --- C3/F6 linearization: stale previewed token -> 409, zero mutation ------

def test_preview_token_then_put_then_apply_is_conflict_zero_mutation(
        portal_http, db, client_row, office_user_a):
    token = _token(office_user_a.auth_user_id)
    s1 = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    previewed = _preview(portal_http, token).json()["schedule_config_updated_at"]
    assert previewed == s1
    s2 = _put(portal_http, _all_open_week(), 60, [], s1, token).json()[
        "schedule_config_updated_at"]
    assert s2 != s1
    before = _count_slots(db, client_row.id)
    resp = _apply(portal_http, previewed, token)    # Apply A after PUT B
    assert resp.status_code == 409
    assert _count_slots(db, client_row.id) == before   # zero mutation


# --- C5 interruption: per-day commits survive; rerun converges ------------

def test_interrupted_apply_keeps_committed_days_and_reruns_without_duplicates(
        portal_http, db, client_row, office_user_a, monkeypatch):
    from app.services import portal_recurring_schedule_service as svc
    token = _token(office_user_a.auth_user_id)
    s1 = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    real = svc._apply_open_day
    state = {"n": 0}

    def flaky(dbs, cid, snap, cur, row):
        state["n"] += 1
        if state["n"] == 2:     # fail AFTER the first open day has committed
            raise RuntimeError("simulated interruption mid-horizon")
        return real(dbs, cid, snap, cur, row)

    monkeypatch.setattr(svc, "_apply_open_day", flaky)
    with pytest.raises(RuntimeError):
        _apply(portal_http, s1, token)
    committed = _count_slots(db, client_row.id)
    assert committed > 0        # the first day's slots are durably committed
    # TARGETED restoration of ONLY svc._apply_open_day. This test's
    # `monkeypatch` is the SAME instance the portal_http fixture used for its
    # auth / get_db overrides, so a broad monkeypatch.undo() here would tear
    # those down and the rerun would 503. Re-setting the attribute restores
    # the real function without disturbing the fixture patches.
    monkeypatch.setattr(svc, "_apply_open_day", real)
    resp = _apply(portal_http, s1, token)   # rerun on the same saved config
    assert resp.status_code == 200
    assert _count_slots(db, client_row.id) >= committed   # converges, no loss
    # C5: EXPLICIT uniqueness - no two slots share the same (start,end)
    # interval for this tenant (a rerun must not duplicate a materialized day).
    from app.calendar_models import AppointmentSlot as _Slot
    intervals = [(s.start_datetime, s.end_datetime) for s in db.query(_Slot).filter(
        _Slot.client_id == client_row.id).all()]
    assert len(intervals) == len(set(intervals)), "duplicate slot intervals after rerun"


# --- DST: spring-forward nonexistent + fall-back ambiguous ----------------

def _dst_apply_open_day(db, client, day, start_hhmm, end_hhmm):
    import types
    from app.services import portal_recurring_schedule_service as svc
    from app.services import calendar_settings_service
    settings = calendar_settings_service.load_calendar_settings(client)
    snap = types.SimpleNamespace(settings=settings, slot_minutes=30)
    return svc._apply_open_day(db, client.id, snap, day,
                               {"open": True, "start": start_hhmm, "end": end_hhmm})


def test_dst_spring_forward_window_is_dst_skipped(db, client_row):
    from datetime import date as _date
    outcome = _dst_apply_open_day(db, client_row, _date(2026, 3, 8), "02:00", "02:30")
    assert outcome["outcome"] == "dst_skipped"     # nonexistent gap; batch continues


def test_dst_fall_back_window_is_dst_skipped(db, client_row):
    from datetime import date as _date
    outcome = _dst_apply_open_day(db, client_row, _date(2026, 11, 1), "01:00", "02:00")
    assert outcome["outcome"] == "dst_skipped"     # ambiguous repeated interval


def test_dst_classifier_uses_only_the_approved_vocabulary(client_row):
    """The P4-A classifier the service relies on emits only nonexistent /
    ambiguous / valid - the service maps invalid geometry to dst_skipped only
    (G4: no new invalid_skipped / would_invalid values enter the vocabulary)."""
    from datetime import datetime as _dt
    from app.services import portal_schedule_service as ss
    tz = _client_tz(client_row)
    gap = ss.classify_local_wall_time(_dt(2026, 3, 8, 2, 30), tz)
    amb = ss.classify_local_wall_time(_dt(2026, 11, 1, 1, 30), tz)
    assert gap.status == ss.WALL_NONEXISTENT
    assert amb.status == ss.WALL_AMBIGUOUS


# --- Tenant isolation across all four operations --------------------------

def test_tenant_isolation_operations_are_credential_derived(
        portal_http, db, client_row, second_client, office_user_a):
    from app.calendar_models import AppointmentSlot
    token = _token(office_user_a.auth_user_id)
    s1 = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    _apply(portal_http, s1, token)
    # Office A's GET/PUT/Preview/Apply used its OWN credential-derived client;
    # office B's row is entirely untouched - no token, no config, no slots.
    b = _reload(db, second_client)
    assert b.schedule_config_updated_at is None
    assert db.query(AppointmentSlot).filter(
        AppointmentSlot.client_id == second_client.id).count() == 0
    assert _count_slots(db, client_row.id) > 0    # A did materialize (isolated)


# ===========================================================================
# T2 - the harder PG17 bites: real finalize hold_lost, genuine concurrent
# overlap, F5 defensive-overlap + F5 exception, F6 snapshot linearization,
# DST batch continuation. Owner-local PG17 is the sole authority (SKIP w/o DB).
# ===========================================================================

def test_closure_blocked_held_slot_finalizes_as_hold_lost(
        portal_http, db, client_row, office_user_a):
    """Closure blocks an ACTIVE held slot; the affected conversation's frozen
    booking-finalize path then returns hold_lost (D4) - the slot is no longer
    HELD for it."""
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz
    from app.calendar_models import AppointmentSlot, SlotStatus
    from app.services import booking_service
    from app.services import calendar_settings_service
    token = _token(office_user_a.auth_user_id); tz = _client_tz(client_row)
    s1 = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    day = _mid_horizon_date(portal_http, token, 3)
    conv_id = _uuid.uuid4()
    held_until = _dt.now(_tz.utc) + timedelta(minutes=10)
    slot = _seed_slot_on(db, client_row, day, "09:00", "09:30",
                         SlotStatus.HELD, tz, held_until=held_until)
    slot.held_by_conversation_id = conv_id
    db.commit()
    s2 = _put(portal_http, _all_open_week(), 30, [{"date": day.isoformat()}],
              s1, token).json()["schedule_config_updated_at"]
    _apply(portal_http, s2, token)              # closure blocks the held slot
    settings = calendar_settings_service.load_calendar_settings(client_row)
    result = booking_service.finalize_booking(
        db, client_row.id, slot.id, conv_id,
        settings=settings, now_utc=_dt.now(_tz.utc), time_preference="any",
        service_key=None, patient_name="Test Patient", patient_phone="+15550000000",
        patient_email=None, new_or_returning=None, reason=None, urgency="routine")
    # Frozen BookingResult is (success: bool, reason: str, appointment, detail):
    # there is no .ok. Assert the real hold_lost outcome and that finalize
    # created NO booking (the substantive bite is preserved, not weakened).
    assert result.success is False
    assert result.reason == "hold_lost"
    assert result.appointment is None


def test_concurrent_manual_publish_and_apply_open_day_single_materialization(
        db, client_row, office_user_a):
    """Genuine overlap: one thread runs the P4-B open-day path and another runs a
    P4-A manual publish on the SAME tenant/local day, each on its OWN connection.
    The per-(tenant,day) advisory lock serializes them: exactly ONE materializes;
    the loser sees the inventory (existing_inventory_skipped / PUBLISH_OVERLAP).
    No duplicate/gap-fill/extension."""
    import threading
    import types
    from datetime import date as _date
    from app.database import SessionLocal
    from app.services import portal_schedule_service as ss
    from app.services import portal_recurring_schedule_service as svc
    from app.services import calendar_settings_service
    from app.calendar_models import AppointmentSlot

    day = _date.today() + timedelta(days=2)
    settings = calendar_settings_service.load_calendar_settings(client_row)
    snap = types.SimpleNamespace(settings=settings, slot_minutes=30)
    start = threading.Barrier(2)
    out = {}

    def run_apply():
        s = SessionLocal()
        try:
            start.wait()
            out["apply"] = svc._apply_open_day(
                s, client_row.id, snap, day,
                {"open": True, "start": "09:00", "end": "17:00"})
        except Exception as exc:      # pragma: no cover - surfaced via assert
            out["apply_exc"] = repr(exc)
        finally:
            s.close()

    def run_publish():
        s = SessionLocal()
        try:
            start.wait()
            r = ss.publish_day_slots(
                s, client_row.id, settings, day, "09:00", "17:00", 30)
            out["publish"] = r.reason
        except Exception as exc:      # pragma: no cover
            out["publish_exc"] = repr(exc)
        finally:
            s.close()

    ta = threading.Thread(target=run_apply); tb = threading.Thread(target=run_publish)
    ta.start(); tb.start(); ta.join(); tb.join()
    assert "apply_exc" not in out and "publish_exc" not in out, out
    # Exactly one materialization: the day holds one 09:00-17:00 sweep (16 slots).
    count = db.query(AppointmentSlot).filter(
        AppointmentSlot.client_id == client_row.id).count()
    assert count == 16, (count, out)
    # The two operations serialized into one winner + one no-op loser.
    apply_outcome = out.get("apply", {}).get("outcome")
    publish_reason = out.get("publish")
    winners = {
        ("published", ss.PUBLISH_OVERLAP),                 # apply won, publish saw it
        ("existing_inventory_skipped", ss.PUBLISH_OK),     # publish won, apply saw it
    }
    assert (apply_outcome, publish_reason) in winners, out


def test_f5_defensive_publish_overlap_releases_lock(
        portal_http, db, client_row, office_user_a):
    """Forcing PUBLISH_OVERLAP (pre-seeded overlapping inventory) rolls back and
    RELEASES the advisory lock: a second connection can immediately acquire the
    same day lock."""
    from app.calendar_models import SlotStatus
    from app.services import portal_schedule_service as ss
    from app.services import calendar_settings_service
    token = _token(office_user_a.auth_user_id); tz = _client_tz(client_row)
    _put(portal_http, _all_open_week(), 30, [], None, token)
    day = _mid_horizon_date(portal_http, token, 3)
    _seed_slot_on(db, client_row, day, "09:00", "09:30", SlotStatus.AVAILABLE, tz)
    settings = calendar_settings_service.load_calendar_settings(client_row)
    result = ss.publish_day_slots(db, client_row.id, settings, day,
                                  "09:00", "17:00", 30)
    assert result.reason == ss.PUBLISH_OVERLAP
    other = _second_session()
    try:
        assert _lock_is_free(other, client_row.id, day) is True
    finally:
        other.close()


def test_f5_unexpected_exception_rolls_back_and_releases_lock(
        db, client_row, office_user_a, monkeypatch):
    """An unexpected error AFTER the day lock is acquired is rolled back (the
    _apply_open_day except-branch), releasing the lock for a second connection."""
    import types
    from datetime import date as _date
    from app.services import portal_recurring_schedule_service as svc
    from app.repositories import appointment_repository
    from app.services import calendar_settings_service
    day = _date.today() + timedelta(days=2)
    settings = calendar_settings_service.load_calendar_settings(client_row)
    snap = types.SimpleNamespace(settings=settings, slot_minutes=30)

    def boom(*a, **k):
        raise RuntimeError("injected failure after lock acquire")

    monkeypatch.setattr(appointment_repository, "list_slots_between", boom)
    with pytest.raises(RuntimeError):
        svc._apply_open_day(db, client_row.id, snap, day,
                            {"open": True, "start": "09:00", "end": "17:00"})
    monkeypatch.undo()
    other = _second_session()
    try:
        assert _lock_is_free(other, client_row.id, day) is True
    finally:
        other.close()


def test_apply_snapshot_linearization_ignores_concurrent_put_b(
        portal_http, db, client_row, office_user_a, monkeypatch):
    """F6: Apply captures config A (30-min). A concurrent config PUT B (60-min)
    commits AFTER the snapshot; the running Apply keeps using A, so published
    days show A's 30-min geometry (16 slots/8h), not B's (8)."""
    from datetime import datetime as _dt, timezone as _tz
    from app.services import portal_recurring_schedule_service as svc
    from app.database import SessionLocal
    from app.models import Client as _Client
    token = _token(office_user_a.auth_user_id)
    sA = _put(portal_http, _all_open_week(), 30, [], None, token).json()[
        "schedule_config_updated_at"]
    real = svc._apply_open_day
    fired = {"done": False}

    def hook(dbs, cid, snap, cur, row):
        if not fired["done"]:
            fired["done"] = True
            other = SessionLocal()
            try:
                c = other.query(_Client).filter(_Client.id == client_row.id).one()
                s = dict(c.settings or {}); cal = dict(s.get("calendar") or {})
                cal["recurring"] = {"slot_minutes": 60, "closures": []}
                s["calendar"] = cal; c.settings = s
                c.schedule_config_updated_at = _dt.now(_tz.utc) + timedelta(hours=1)
                other.commit()                 # PUT B commits after snapshot A
            finally:
                other.close()
        return real(dbs, cid, snap, cur, row)

    monkeypatch.setattr(svc, "_apply_open_day", hook)
    result = _apply(portal_http, sA, token).json()
    published = [d for d in result["days"] if d["outcome"] == "published"]
    assert published, "at least one day published"
    # 09:00-17:00 at 30 min = 16 slots (A); at 60 min it would be 8 (B).
    assert published[0].get("published_count") == 16


def test_dst_apply_batch_continues_past_invalid_day(
        portal_http, db, client_row, office_user_a, monkeypatch):
    """A full Apply whose horizon holds a DST-invalid day AND a later valid open
    day: the invalid day reports dst_skipped and the batch continues - the valid
    day still materializes."""
    from datetime import date as _date
    from app.services import portal_recurring_schedule_service as svc
    token = _token(office_user_a.auth_user_id)
    weekly = {wd: {"open": False, "start": None, "end": None} for wd in WEEKDAYS}
    weekly["sun"] = {"open": True, "start": "02:00", "end": "02:30"}   # spring-forward gap
    weekly["mon"] = {"open": True, "start": "09:00", "end": "17:00"}
    s1 = _put(portal_http, weekly, 30, [], None, token).json()[
        "schedule_config_updated_at"]
    # Horizon = 2026-03-08 (Sun, NY spring-forward) .. 2026-03-09 (Mon).
    monkeypatch.setattr(svc, "_horizon",
                        lambda settings: (_date(2026, 3, 8), _date(2026, 3, 9)))
    result = _apply(portal_http, s1, token).json()
    sun = _day_outcome(result, _date(2026, 3, 8))
    mon = _day_outcome(result, _date(2026, 3, 9))
    assert sun["outcome"] == "dst_skipped"          # invalid day skipped
    assert mon["outcome"] == "published"            # batch continued
    assert mon.get("published_count") == 16


# ===========================================================================
# V2 - execute the P4-B _apply_open_day DEFENSIVE PUBLISH_OVERLAP branch. The
# normal under-lock emptiness check prevents reaching it, so two narrow seams
# reproduce the race WITHOUT touching frozen P4-A code: the emptiness check sees
# no inventory, and the delegated publisher behaves as the real P4-A overlap
# outcome (rolls back -> releases the lock -> returns PUBLISH_OVERLAP).
# ===========================================================================

def test_apply_open_day_defensive_overlap_branch_releases_lock(
        db, client_row, office_user_a, monkeypatch):
    import types
    from datetime import date as _date
    from app.services import portal_recurring_schedule_service as svc
    from app.services import portal_schedule_service as ss
    from app.repositories import appointment_repository
    from app.services import calendar_settings_service

    day = _date.today() + timedelta(days=2)
    settings = calendar_settings_service.load_calendar_settings(client_row)
    snap = types.SimpleNamespace(settings=settings, slot_minutes=30)

    # Seam 1: the P4-B under-lock emptiness check sees NO inventory, so
    # _apply_open_day proceeds to publish (rather than short-circuiting to
    # existing_inventory_skipped before publish).
    monkeypatch.setattr(appointment_repository, "list_slots_between",
                        lambda *a, **k: [])

    seen = {"lock_held_during_publish": None}

    # Seam 2: the delegated publisher reproduces the real P4-A overlap outcome.
    # First it PROVES P4-B already holds this day's advisory lock (a second
    # connection cannot take it), then rolls back (which releases that lock,
    # exactly as the frozen P4-A overlap path does) and returns PUBLISH_OVERLAP.
    def overlap_publish(dbs, cid, s, d, o, c, m):
        probe = _second_session()
        try:
            seen["lock_held_during_publish"] = (
                _lock_is_free(probe, client_row.id, day) is False)
        finally:
            probe.close()
        dbs.rollback()   # real P4-A overlap rolls back -> releases the advisory lock
        return ss.PublishResult(False, ss.PUBLISH_OVERLAP, detail="overlap")

    monkeypatch.setattr(ss, "publish_day_slots", overlap_publish)

    outcome = svc._apply_open_day(
        db, client_row.id, snap, day,
        {"open": True, "start": "09:00", "end": "17:00"})

    assert seen["lock_held_during_publish"] is True     # P4-B acquired the day lock
    assert outcome["outcome"] == "existing_inventory_skipped"  # defensive branch
    assert not db.in_transaction()                      # no P4-B transaction remains open
    monkeypatch.undo()
    other = _second_session()
    try:
        assert _lock_is_free(other, client_row.id, day) is True  # lock released
    finally:
        other.close()
