# calendar_tests/test_explicit_date_time_window.py
#
# Checkpoint A: explicit calendar dates in the intake time window.
#
# Staging defect these pin (2026-07-27):
#     User: July 27 morning
#     Mia:  "What day/time works best for you? For example: Mon morning or
#            Tue afternoon."   ... forever
#
# detect_time_window() had no month/day vocabulary, so "July 27 morning"
# collapsed to "morning" (the date silently discarded) and
# time_window_is_complete() could never return True.
#
# Canonical internal storage is now:
#     Mon 2026-07-27
#     Mon 2026-07-27 morning
#     Mon 2026-07-27 9am
#
# The leading weekday token keeps every existing consumer working; the ISO
# token carries the real date so nothing has to re-derive it from the weekday
# (which is wrong for anything more than a week out). The ISO token must NEVER
# reach a patient or the office.
#
# Checkpoint A does NOT touch the early intake routing guard or multi-turn
# merging. Those are Checkpoint B.

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from app.routes import chat as chat_module
    HAVE_CHAT = True
except ModuleNotFoundError:  # pragma: no cover - environment guard
    HAVE_CHAT = False

pytestmark = pytest.mark.skipif(not HAVE_CHAT, reason="app.routes.chat requires SQLAlchemy/FastAPI")

ISO_IN_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")


def detect(text):
    """No client: the parser falls back to datetime.now() for the year rule."""
    return chat_module.detect_time_window(text, None)


def today():
    return datetime.now().date()


def a_future_date(days_ahead):
    return today() + timedelta(days=days_ahead)


def a_future_weekday(days_ahead):
    """A weekday (Mon-Fri) at least `days_ahead` days out.

    CHECKPOINT B: the early intake guard now delegates to the one capture
    owner, handle_time_window_capture(), whose weekend validation (Sunday
    nudge / weekday-only rejection for offices without weekend hours) is
    live on this path too. Day-only fixture dates that could land on a
    weekend would make those endpoint tests flaky by run date."""
    d = today() + timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def phrase(d, suffix=""):
    """'July 27' / 'July 27 morning' for a real date, month name spelled out."""
    base = d.strftime("%B %d").replace(" 0", " ")
    return f"{base} {suffix}".strip()


def canonical(d, detail=""):
    base = f"{d.strftime('%a')} {d.isoformat()}"
    return f"{base} {detail}".strip()


# ---------------------------------------------------------------------------
# 1-6, 10: the supported input forms
# ---------------------------------------------------------------------------

def test_month_name_and_day_parses_to_weekday_plus_iso():
    d = a_future_date(3)
    assert detect(phrase(d)) == canonical(d)


def test_abbreviated_month_parses():
    d = a_future_date(3)
    assert detect(d.strftime("%b %d").replace(" 0", " ")) == canonical(d)


@pytest.mark.parametrize("suffix", ["st", "nd", "rd", "th"])
def test_ordinal_day_suffixes_parse(suffix):
    """Patients write 27th / 1st / 2nd / 3rd. All are accepted; the suffix is
    not validated against the number, which is deliberate leniency."""
    d = a_future_date(3)
    label = d.strftime("%B %d").replace(" 0", " ")
    assert detect(f"{label}{suffix}") == canonical(d)


def test_explicit_year_parses():
    d = a_future_date(3)
    assert detect(d.strftime("%B %d, %Y").replace(" 0", " ")) == canonical(d)


def test_numeric_date_uses_us_month_day_order():
    """7/27 is July 27, never the 7th of an imagined 27th month."""
    d = date(today().year, 7, 27)
    if d < today():
        d = date(today().year + 1, 7, 27)
    assert detect("7/27") == canonical(d)


def test_numeric_date_with_year_parses():
    d = a_future_date(5)
    assert detect(d.strftime("%m/%d/%Y").lstrip("0")) == canonical(d)


def test_part_of_day_and_exact_time_details_are_retained():
    d = a_future_date(3)
    assert detect(phrase(d, "morning")) == canonical(d, "morning")
    assert detect(phrase(d, "afternoon")) == canonical(d, "afternoon")
    assert detect(phrase(d, "at 9am")) == canonical(d, "9am")


def test_explicit_date_with_detail_is_complete():
    """The staging loop: this had to become True."""
    d = a_future_date(3)
    assert chat_module.time_window_is_complete(detect(phrase(d, "morning"))) is True


def test_explicit_date_alone_is_day_only_not_complete():
    d = a_future_date(3)
    value = detect(phrase(d))
    assert chat_module.time_window_has_specific_day(value) is True
    assert chat_module.time_window_has_detail(value) is False
    assert chat_module.time_window_is_complete(value) is False


# ---------------------------------------------------------------------------
# 7-9: year resolution and impossible dates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["February 30", "February 30 morning", "13/45", "0/0"])
def test_impossible_dates_return_none_rather_than_guessing(text):
    assert chat_module.parse_explicit_calendar_date(text, datetime.now()) is None


def test_yearless_past_date_rolls_to_next_year():
    yesterday = today() - timedelta(days=1)
    parsed = chat_module.parse_explicit_calendar_date(
        yesterday.strftime("%B %d").replace(" 0", " "), datetime.now()
    )
    assert parsed is not None
    assert parsed.year == yesterday.year + 1
    assert (parsed.month, parsed.day) == (yesterday.month, yesterday.day)


def test_yearless_future_date_uses_current_year():
    d = a_future_date(2)
    parsed = chat_module.parse_explicit_calendar_date(
        d.strftime("%B %d").replace(" 0", " "), datetime.now()
    )
    assert parsed == d


def test_yearless_today_uses_current_year():
    parsed = chat_module.parse_explicit_calendar_date(
        today().strftime("%B %d").replace(" 0", " "), datetime.now()
    )
    assert parsed == today()


# ---------------------------------------------------------------------------
# 11, 20: every existing phrasing is untouched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("tomorrow morning", (datetime.now() + timedelta(days=1)).strftime("%a") + " morning"),
        ("today afternoon", datetime.now().strftime("%a") + " afternoon"),
        ("Monday morning", "Mon morning"),
        ("monday", "Mon"),
        ("Wed 9", "Wed"),
        ("Tue 3pm", "Tue 3pm"),
        ("weekday morning", "Weekday morning"),
        ("morning", "morning"),
        ("I want an appointment", None),
    ],
)
def test_existing_forms_are_unchanged(text, expected):
    assert detect(text) == expected


def test_bare_part_of_day_remains_incomplete():
    assert chat_module.time_window_is_complete("morning") is False


# ---------------------------------------------------------------------------
# 12-14: rendering. The ISO token must never be shown.
# ---------------------------------------------------------------------------

def test_pretty_renders_explicit_date_exactly_once():
    d = a_future_date(3)
    rendered = chat_module.pretty_time_window(canonical(d, "morning"))
    month_day = d.strftime("%b %d").replace(" 0", " ")
    assert rendered.count(month_day) == 1
    assert rendered.endswith("morning")


@pytest.mark.parametrize("detail", ["", "morning", "9am"])
def test_raw_iso_never_appears_in_staff_facing_wording(detail):
    d = a_future_date(10)
    rendered = chat_module.pretty_time_window(canonical(d, detail))
    assert not ISO_IN_TEXT.search(rendered), f"ISO date leaked: {rendered!r}"


def test_date_more_than_seven_days_away_renders_its_true_date():
    """The rejected shape re-derived the NEXT occurrence of the weekday and
    produced two contradictory dates. It must render the real one."""
    d = a_future_date(21)
    rendered = chat_module.pretty_time_window(canonical(d, "morning"))
    assert d.strftime("%b %d").replace(" 0", " ") in rendered
    wrong = (today() + timedelta(days=(d.weekday() - today().weekday()) % 7 or 7))
    if wrong != d:
        assert wrong.strftime("%b %d").replace(" 0", " ") not in rendered


def test_today_and_tomorrow_labels_use_the_explicit_date():
    assert chat_module.pretty_time_window(canonical(today(), "morning")).startswith("Today (")
    assert chat_module.pretty_time_window(
        canonical(today() + timedelta(days=1), "morning")
    ).startswith("Tomorrow (")


@pytest.mark.parametrize("legacy", ["Mon morning", "Tue 3pm", "Wed", "Weekday morning", "Sat 9am"])
def test_legacy_weekday_only_rendering_is_preserved(legacy):
    """Values with no ISO date keep the original code path verbatim."""
    rendered = chat_module.pretty_time_window(legacy)
    assert rendered
    assert not ISO_IN_TEXT.search(rendered)


# ---------------------------------------------------------------------------
# 15-19: downstream consumers
# ---------------------------------------------------------------------------

def test_explicit_saturday_is_still_recognized_as_saturday():
    """Weekend and closed-day rules key off the day token, which survives."""
    saturday = today() + timedelta(days=(5 - today().weekday()) % 7 or 7)
    value = canonical(saturday, "morning")
    assert chat_module._extract_day_token(value) == "Sat"
    assert chat_module._get_day_key_from_time_window(value) == "sat"


def test_day_only_weekday_token_replaces_exact_string_membership():
    """The old `detected_tw in {"Mon",...}` checks silently stopped matching
    once a value could carry a date. The token owner keeps the ORIGINAL
    day-only meaning: 'Mon morning' must still not qualify."""
    d = a_future_date(3)
    assert chat_module.time_window_day_only_weekday_token(canonical(d)) == d.strftime("%a")
    assert chat_module.time_window_day_only_weekday_token(canonical(d, "morning")) is None
    assert chat_module.time_window_day_only_weekday_token("Mon") == "Mon"
    assert chat_module.time_window_day_only_weekday_token("Mon morning") is None


def test_same_weekday_but_future_date_is_not_treated_as_today():
    """A Monday three weeks out shares today's weekday when today is Monday.
    Comparing formatted weekday strings called it 'today'."""
    same_weekday_future = today() + timedelta(days=21)
    value = canonical(same_weekday_future, "morning")
    assert chat_module.time_window_is_client_today(value, datetime.now()) is False


def test_explicit_date_equal_to_today_is_detected_as_today():
    value = canonical(today(), "morning")
    assert chat_module.time_window_is_client_today(value, datetime.now()) is True


def test_legacy_weekday_only_today_detection_is_unchanged():
    """Without an ISO date the weekday-token fallback still applies."""
    assert chat_module.time_window_is_client_today(
        f"{datetime.now().strftime('%a')} morning", datetime.now()
    ) is True
    other = today() + timedelta(days=2)
    assert chat_module.time_window_is_client_today(
        f"{other.strftime('%a')} morning", datetime.now()
    ) is False


def test_specificity_score_matches_the_legacy_equivalent():
    """Merge ordering must not shift because a value now carries a date."""
    d = a_future_date(3)
    score = chat_module._time_window_specificity_score
    assert score(canonical(d)) == score("Mon")
    assert score(canonical(d, "morning")) == score("Mon morning")


def test_exact_time_extraction_matches_the_legacy_equivalent():
    """The ISO digits must never be misread as a clock time."""
    d = a_future_date(3)
    extract = chat_module._extract_exact_time_minutes_from_tw
    assert extract(canonical(d)) is None
    assert extract(canonical(d, "morning")) == extract("Mon morning")
    assert extract(canonical(d, "9am")) == extract("Mon 9am")


# ===========================================================================
# Real consumer tests.
#
# The helper tests above prove the owners behave; these prove the CONSUMERS
# that were actually wrong now call them. A helper-only test is not evidence
# that build_time_window_issue_reply() or the staff renderers were fixed.
#
# get_client_now / get_office_hours_struct are patched so the office timezone
# and hours are deterministic and independent of the machine clock.
# ===========================================================================

from types import SimpleNamespace

from calendar_tests.conftest import make_conversation as _base_make_conversation, requires_db
from calendar_tests.test_chat_integration import OPEN_ALL_WEEK_HOURS, fakes, make_client, send  # noqa: F401


def make_conversation(db, client, **overrides):
    """Test-local adapter around the shared two-argument fixture helper.

    The shared helper creates and commits the baseline Conversation. These
    Checkpoint A endpoint tests need targeted field values, so apply only the
    requested overrides and persist them before driving the real endpoint.
    """
    conversation = _base_make_conversation(db, client)

    for field_name, field_value in overrides.items():
        setattr(conversation, field_name, field_value)

    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation
CLIENT = SimpleNamespace(id="fake-client")

# Mon 2026-07-27, 3:00 PM office-local. Mid-afternoon so "already passed
# today" is reachable, and a Monday so the same-weekday confusion is live.
CLIENT_NOW = datetime(2026, 7, 27, 15, 0)

OPEN_WEEK = {d: {"open": True, "start": "09:00", "end": "17:00"}
             for d in ("mon", "tue", "wed", "thu", "fri")}


def _hours(**overrides):
    hours = {d: dict(v) for d, v in OPEN_WEEK.items()}
    hours["sat"] = {"open": False, "start": None, "end": None}
    hours["sun"] = {"open": False, "start": None, "end": None}
    hours.update(overrides)
    return hours


@pytest.fixture()
def office(monkeypatch):
    """A deterministic office: Mon-Fri 9-5, closed weekends, now = Mon 3 PM."""
    state = {"now": CLIENT_NOW, "hours": _hours()}
    monkeypatch.setattr(chat_module, "get_client_now", lambda c: state["now"])
    monkeypatch.setattr(chat_module, "get_office_hours_struct", lambda c: state["hours"])
    return state


def _issue(tw):
    return chat_module.build_time_window_issue_reply(CLIENT, tw)


# --- 1-3: build_time_window_issue_reply is explicit-date aware -------------

def test_future_explicit_date_sharing_todays_weekday_is_not_a_past_time(office):
    """Today is Monday 3 PM. Monday three weeks out at 9 AM is in the FUTURE.
    Comparing weekday keys called it today and rejected it."""
    reply = _issue("Mon 2026-08-17 9am")
    assert reply is None, f"future date wrongly rejected: {reply!r}"


def test_explicit_date_equal_to_today_with_earlier_time_still_passed(office):
    """The existing protection must survive: 9 AM today, at 3 PM today."""
    assert _issue("Mon 2026-07-27 9am") == (
        "That time has already passed today. "
        "What later time today or another day works better for you?"
    )


def test_closed_future_date_sharing_todays_weekday_names_the_day(office):
    office["hours"]["mon"] = {"open": False, "start": None, "end": None}
    reply = _issue("Mon 2026-08-17 9am")
    assert reply is not None
    assert "closed today" not in reply
    assert "closed on Monday" in reply


def test_legacy_weekday_only_today_behavior_is_unchanged(office):
    """No ISO date: the weekday fallback still treats Mon as today."""
    assert _issue("Mon 9am") == (
        "That time has already passed today. "
        "What later time today or another day works better for you?"
    )


# --- 4-6: closed / open explicit Saturday through the real paths -----------

def test_closed_explicit_saturday_with_detail_is_rejected(office):
    reply = _issue("Sat 2026-08-01 morning")
    assert reply is not None
    assert "closed on Saturday" in reply


def test_closed_explicit_saturday_day_only_is_rejected_by_the_capture_owner(
        office, monkeypatch):
    """Day-only weekend values are caught by handle_time_window_capture.
    The closed-day server revalidation now routes this rejection through the
    single office-hours owner (is_day_open, via build_time_window_issue_reply):
    the office fixture configures Saturday closed, so the truthful closed-day
    correction is returned instead of the older weekend-only "choose a weekday"
    wording, and nothing is persisted."""
    monkeypatch.setattr(chat_module, "is_saturday_open", lambda c: False)
    monkeypatch.setattr(chat_module, "is_sunday_closed", lambda c: True)
    monkeypatch.setattr(chat_module, "build_time_window_examples",
                        lambda c, prefer_weekdays=True: "")
    conversation = SimpleNamespace(lead_time_window="", lead_name="Kyle",
                                   lead_phone="516-555-5555", lead_is_priority=False)

    reply, saved = chat_module.handle_time_window_capture(
        CLIENT, conversation, "August 1", ""
    )

    assert saved is False
    reply_l = (reply or "").lower()
    assert "closed" in reply_l and "saturday" in reply_l
    assert (conversation.lead_time_window or "") == ""


def test_open_explicit_saturday_follows_existing_accepted_behavior(office, monkeypatch):
    office["hours"]["sat"] = {"open": True, "start": "09:00", "end": "13:00"}
    monkeypatch.setattr(chat_module, "is_saturday_open", lambda c: True)
    monkeypatch.setattr(chat_module, "is_sunday_closed", lambda c: True)
    monkeypatch.setattr(chat_module, "build_time_window_examples",
                        lambda c, prefer_weekdays=True: "")
    conversation = SimpleNamespace(lead_time_window="", lead_name="Kyle",
                                   lead_phone="516-555-5555", lead_is_priority=False)

    chat_module.handle_time_window_capture(CLIENT, conversation, "August 1", "")

    assert conversation.lead_time_window == "Sat 2026-08-01"
    assert _issue("Sat 2026-08-01 9am") is None


# --- 9: outside-hours wording never leaks ISO ------------------------------

@pytest.mark.parametrize("tw", ["Sat 2026-08-01 morning", "Mon 2026-08-17 9am",
                                "Mon 2026-08-17", "Mon morning"])
def test_check_outside_hours_wording_never_leaks_iso(office, tw):
    _, note = chat_module.check_outside_hours(CLIENT, tw)
    if note:
        assert not ISO_IN_TEXT.search(note), f"ISO leaked into staff note: {note!r}"


def test_check_outside_hours_note_uses_the_client_local_date(office):
    office["hours"]["mon"] = {"open": False, "start": None, "end": None}
    is_outside, note = chat_module.check_outside_hours(CLIENT, "Mon 2026-08-17 9am")
    assert is_outside is True
    assert "Aug 17" in note
    assert not ISO_IN_TEXT.search(note)


# --- 10: client timezone boundary -----------------------------------------

def test_staff_rendering_uses_client_local_today_not_server_local(office, monkeypatch):
    """Server and office sit on opposite sides of midnight. The office's own
    date must decide the Today / Tomorrow label."""
    monkeypatch.setattr(chat_module, "lead_is_same_day_without_explicit_urgency",
                        lambda conv: False)
    conversation = SimpleNamespace(lead_time_window="Tue 2026-07-28 morning")

    # Office-local date is Mon Jul 27, so Jul 28 is Tomorrow.
    office["now"] = datetime(2026, 7, 27, 21, 0)
    assert chat_module.pretty_staff_time_window(conversation, CLIENT).startswith("Tomorrow (")

    # Office-local date has rolled to Jul 28, so the same value is Today.
    office["now"] = datetime(2026, 7, 28, 1, 0)
    assert chat_module.pretty_staff_time_window(conversation, CLIENT).startswith("Today (")


def test_pretty_time_window_reference_date_defaults_preserve_legacy(office):
    """Omitting reference_date keeps the previous server-local behavior, so
    every legacy caller is unchanged."""
    server_today = datetime.now().date()
    assert chat_module.pretty_time_window(
        f"{server_today.strftime('%a')} {server_today.isoformat()} morning"
    ).startswith("Today (")


# --- 7-8: staff email and SMS summaries -----------------------------------


# ===========================================================================
# Blocker 3: an explicitly stated PAST date is a mistake, not a booking.
# ===========================================================================

def test_explicit_past_year_is_rejected(office):
    """July 27, 2025 when the office clock says July 27, 2026."""
    assert chat_module.parse_explicit_calendar_date("July 27, 2025", CLIENT_NOW) is None
    assert chat_module.detect_time_window("July 27, 2025 morning", CLIENT) is None


def test_explicit_past_numeric_date_is_rejected(office):
    assert chat_module.parse_explicit_calendar_date("7/27/2025", CLIENT_NOW) is None


def test_explicit_today_is_accepted(office):
    assert chat_module.parse_explicit_calendar_date("July 27, 2026", CLIENT_NOW) == date(2026, 7, 27)


def test_explicit_future_date_is_accepted(office):
    assert chat_module.parse_explicit_calendar_date("August 17, 2026", CLIENT_NOW) == date(2026, 8, 17)
    assert chat_module.parse_explicit_calendar_date("8/17/2026", CLIENT_NOW) == date(2026, 8, 17)


def test_yearless_past_month_day_still_rolls_forward(office):
    """The rollover rule applies ONLY when no year was stated."""
    assert chat_module.parse_explicit_calendar_date("January 5", CLIENT_NOW) == date(2027, 1, 5)


def test_impossible_dates_remain_rejected_with_explicit_year(office):
    assert chat_module.parse_explicit_calendar_date("February 30, 2026", CLIENT_NOW) is None


# ===========================================================================
# Output boundaries: the canonical ISO value is INTERNAL. It must never reach
# the browser, the model, the office, or the patient.
# ===========================================================================

FUTURE_TW = "Mon 2026-08-17 9am"


def test_lead_context_renders_the_time_window_and_leaks_no_iso(office):
    conversation = SimpleNamespace(
        lead_name="Kyle", lead_phone="516-555-5555", lead_email="",
        lead_reason="tooth pain", lead_is_new_patient=True,
        lead_time_window=FUTURE_TW, lead_email_opt_out=False,
    )

    context = chat_module.build_lead_context(conversation, CLIENT)

    assert context is not None
    body = context["content"]
    assert not ISO_IN_TEXT.search(body), f"raw ISO reached the model context: {body!r}"
    assert "Aug 17" in body
    assert "9am" in body


def test_lead_context_without_client_still_renders_and_leaks_no_iso():
    """Legacy compatibility: the client argument is optional."""
    conversation = SimpleNamespace(
        lead_name="Kyle", lead_phone="", lead_email="", lead_reason="",
        lead_is_new_patient=None, lead_time_window=FUTURE_TW, lead_email_opt_out=False,
    )
    body = chat_module.build_lead_context(conversation)["content"]
    assert not ISO_IN_TEXT.search(body)


def test_rendering_owner_is_shared_by_every_boundary(office):
    """One client-aware renderer, so the boundaries cannot drift apart."""
    rendered = chat_module._render_time_window_for_client(CLIENT, FUTURE_TW)
    assert rendered == chat_module.pretty_time_window(FUTURE_TW, CLIENT_NOW.date())
    assert not ISO_IN_TEXT.search(rendered)
    assert chat_module._render_time_window_for_client(CLIENT, None) == ""


# --- the real staff builders, not a helper ---------------------------------

@requires_db
@pytest.mark.parametrize("tw,expect_label,expect_detail", [
    ("Mon 2026-08-17 9am", "Aug 17", "9am"),
    ("Mon 2026-07-27 morning", "Jul 27", "morning"),
])
def test_staff_email_summary_renders_date_once_without_iso(
        db, client_row, monkeypatch, tw, expect_label, expect_detail):
    monkeypatch.setattr(chat_module, "get_client_now", lambda c: CLIENT_NOW)
    conversation = make_conversation(db, client_row)
    conversation.lead_time_window = tw
    db.add(conversation)
    db.commit()

    body = chat_module.build_staff_lead_summary(client_row, conversation)

    assert not ISO_IN_TEXT.search(body), f"raw ISO reached the staff email: {body!r}"
    assert body.count(expect_label) == 1
    assert expect_detail in body


@requires_db
@pytest.mark.parametrize("tw,expect_label,expect_detail", [
    ("Mon 2026-08-17 9am", "Aug 17", "9am"),
    ("Mon 2026-07-27 morning", "Jul 27", "morning"),
])
def test_staff_sms_renders_date_once_without_iso(
        db, client_row, monkeypatch, tw, expect_label, expect_detail):
    monkeypatch.setattr(chat_module, "get_client_now", lambda c: CLIENT_NOW)
    conversation = make_conversation(db, client_row)
    conversation.lead_time_window = tw
    db.add(conversation)
    db.commit()

    body = chat_module.build_staff_lead_sms(client_row, conversation)

    assert not ISO_IN_TEXT.search(body), f"raw ISO reached the staff SMS: {body!r}"
    assert body.count(expect_label) == 1
    assert expect_detail in body


@requires_db
def test_staff_builders_use_the_client_local_today_label(db, client_row, monkeypatch):
    monkeypatch.setattr(chat_module, "get_client_now", lambda c: CLIENT_NOW)
    conversation = make_conversation(db, client_row)
    conversation.lead_time_window = "Mon 2026-07-27 morning"
    db.add(conversation)
    db.commit()

    summary = chat_module.build_staff_lead_summary(client_row, conversation)
    sms = chat_module.build_staff_lead_sms(client_row, conversation)

    assert "Today (Jul 27)" in summary
    assert "Today (Jul 27)" in sms


# --- the real ChatResponse boundary ----------------------------------------

@requires_db
def test_chat_response_meta_never_carries_a_raw_iso_date(db, monkeypatch):
    """Single-turn explicit-date intake capture, driven through the real
    endpoint. The stored value keeps its ISO date; the response must not."""
    import json

    client = make_client(db, office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )

    target = datetime.now().date() + timedelta(days=10)
    resp = send(db, client, conversation, f"{target.strftime('%B %d').replace(' 0', ' ')} morning")

    assert not ISO_IN_TEXT.search(json.dumps(resp.meta, default=str)), (
        f"raw ISO reached the browser: {resp.meta!r}"
    )
    assert not ISO_IN_TEXT.search(resp.reply), f"raw ISO reached the patient: {resp.reply!r}"

    # The canonical value is still stored internally, ISO intact.
    db.refresh(conversation)
    assert ISO_IN_TEXT.search(conversation.lead_time_window or ""), (
        f"canonical storage lost its date: {conversation.lead_time_window!r}"
    )
    assert chat_module.time_window_is_complete(conversation.lead_time_window) is True


@requires_db
def test_chat_response_meta_still_reports_the_saved_window_readably(db):
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )
    target = datetime.now().date() + timedelta(days=10)
    resp = send(db, client, conversation, f"{target.strftime('%B %d').replace(' 0', ' ')} morning")

    saved = resp.meta.get("saved_time_window")
    if saved is not None:
        assert not ISO_IN_TEXT.search(saved)
        assert target.strftime("%b %d").replace(" 0", " ") in saved


# ===========================================================================
# A rejected date must NOT fall through to the legacy part-of-day parser.
#
# find_explicit_calendar_date() reports three states. Collapsing
# INVALID_OR_REJECTED_DATE_MATCH into NO_DATE_MATCH made
# "February 30 morning" parse as plain "morning": the date vanished
# silently and the never-complete time-window loop came straight back.
# ===========================================================================

REJECTED_DATE_PHRASES = [
    "July 27, 2025 morning",
    "7/27/2025 morning",
    "February 30 morning",
    "13/45 morning",
    "February 30 at 9am",
    "July 27, 2025",
    "13/45",
]


@pytest.mark.parametrize("text", REJECTED_DATE_PHRASES)
def test_rejected_date_never_degrades_to_a_detail_only_value(office, text):
    result = chat_module.detect_time_window(text, CLIENT)
    assert result is None, (
        f"{text!r} degraded to {result!r}; the rejected date was silently "
        "dropped and an incomplete value would be stored"
    )


@pytest.mark.parametrize("text", REJECTED_DATE_PHRASES)
def test_rejected_date_is_not_storable(office, text):
    """canonicalize_time_window_for_storage is what the intake guard calls."""
    assert chat_module.canonicalize_time_window_for_storage(CLIENT, text) is None


def test_three_states_are_distinguishable(office):
    """The whole repair rests on telling 'no date' from 'bad date'."""
    find = chat_module.find_explicit_calendar_date

    assert find("morning", CLIENT_NOW) is None                      # NO_DATE_MATCH
    valid = find("August 17 morning", CLIENT_NOW)                   # VALID
    assert valid is not None and valid[0] == date(2026, 8, 17)
    rejected = find("February 30 morning", CLIENT_NOW)              # REJECTED
    assert rejected is not None and rejected[0] is None


def test_detail_only_answers_still_work(office):
    assert chat_module.detect_time_window("morning", CLIENT) == "morning"
    assert chat_module.detect_time_window("afternoon", CLIENT) == "afternoon"


@pytest.mark.parametrize("text,expected", [
    ("Monday morning", "Mon morning"),
    ("Tue 3pm", "Tue 3pm"),
    ("weekday morning", "Weekday morning"),
    ("I want an appointment", None),
])
def test_phrases_without_date_syntax_still_use_the_legacy_parser(office, text, expected):
    assert chat_module.detect_time_window(text, CLIENT) == expected


def test_valid_future_date_with_detail_still_builds_the_canonical_value(office):
    assert chat_module.detect_time_window("August 17 morning", CLIENT) == "Mon 2026-08-17 morning"
    assert chat_module.detect_time_window("August 17 at 9am", CLIENT) == "Mon 2026-08-17 9am"
    assert chat_module.time_window_is_complete("Mon 2026-08-17 morning") is True


@requires_db
@pytest.mark.parametrize("bad_phrase", ["February 30 morning", "13/45 morning"])
def test_intake_does_not_store_a_detail_only_value_for_a_bad_date(db, bad_phrase):
    """The real intake boundary. A rejected date must not overwrite the
    partial value already captured, and must not be saved as 'morning'."""
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_email_opt_out=False, lead_is_new_patient=None,
    )
    # A day-only value already captured, so the guard is still active.
    conversation.lead_time_window = "Mon 2026-08-17"
    db.add(conversation)
    db.commit()

    resp = send(db, client, conversation, bad_phrase)

    db.refresh(conversation)
    assert conversation.lead_time_window == "Mon 2026-08-17", (
        f"the captured day was destroyed: {conversation.lead_time_window!r}"
    )
    assert conversation.lead_time_window != "morning"
    assert not ISO_IN_TEXT.search(resp.reply)
    assert "?" in resp.reply, "Mia must ask again rather than accept nothing"


@requires_db
def test_intake_stores_a_valid_explicit_date_end_to_end(db):
    """The positive control for the test above."""
    client = make_client(db, office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )
    target = datetime.now().date() + timedelta(days=10)

    send(db, client, conversation, f"{target.strftime('%B %d').replace(' 0', ' ')} morning")

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert target.isoformat() in stored, f"canonical date not stored: {stored!r}"
    assert chat_module.time_window_is_complete(stored) is True


# ===========================================================================
# Dental reality: "pain 9/10" is a severity score, not October 9th.
#
# A date-shaped slash token is classified by its surrounding words. When the
# context says rating or ratio, the token is reported as a REJECTED match so
# the whole phrase yields nothing — otherwise the legacy parser would keep
# the nearby "morning" and store an incomplete window all over again.
# ===========================================================================

RATING_PHRASES = [
    "my pain is 7/10",
    "my pain is 7/10 and morning",
    "pain 9/10",
    "pain 9/10 morning",
    "I rate it 8/10 at 9am",
    "severity is 6/10",
    "9/10 people asked",
    "my tooth hurts 8/10 morning",
    "on a scale it is 7/10 morning",
]

VALID_NUMERIC_DATES = {
    "7/27": "Mon 2026-07-27",
    "7/27 morning": "Mon 2026-07-27 morning",
    "on 7/27": "Mon 2026-07-27",
    "7/27 at 9am": "Mon 2026-07-27 9am",
    "appointment on 7/27 morning": "Mon 2026-07-27 morning",
    "7/27/2026": "Mon 2026-07-27",
    "on 7/27/2026 at 9am": "Mon 2026-07-27 9am",
}


@pytest.mark.parametrize("text", RATING_PHRASES)
def test_pain_rating_never_becomes_an_appointment_date(office, text):
    result = chat_module.detect_time_window(text, CLIENT)
    assert result is None, (
        f"{text!r} parsed as {result!r}; a severity score was booked as a date"
    )


@pytest.mark.parametrize("text", RATING_PHRASES)
def test_pain_rating_is_not_storable(office, text):
    assert chat_module.canonicalize_time_window_for_storage(CLIENT, text) is None


@pytest.mark.parametrize("text", RATING_PHRASES)
def test_pain_rating_is_a_rejected_match_not_a_missing_one(office, text):
    """The distinction that stops the fallthrough: a rating must NOT look
    like NO_DATE_MATCH, or 'pain 9/10 morning' degrades to 'morning'."""
    found = chat_module.find_explicit_calendar_date(text, CLIENT_NOW)
    assert found is not None, f"{text!r} reported NO_DATE_MATCH"
    assert found[0] is None, f"{text!r} resolved to a date: {found[0]!r}"


@pytest.mark.parametrize("text,expected", sorted(VALID_NUMERIC_DATES.items()))
def test_valid_numeric_dates_still_parse(office, text, expected):
    assert chat_module.detect_time_window(text, CLIENT) == expected


def test_standalone_ambiguous_slash_remains_a_date(office):
    """Numeric month/day support is an approved requirement. Without rating
    context, 9/10 stays September 10."""
    assert chat_module.detect_time_window("9/10", CLIENT) == "Thu 2026-09-10"


def test_date_preposition_wins_over_nearby_symptom_language(office):
    """A long message must not lose a real date just because it also
    mentions pain."""
    assert chat_module.detect_time_window(
        "severe pain, come in on 7/27 morning", CLIENT
    ) == "Mon 2026-07-27 morning"


def test_rating_classifier_is_bounded(office):
    """The classifier reads a bounded window, not the whole message."""
    classify = chat_module._numeric_slash_is_rating_or_ratio
    text = "pain 9/10"
    start = text.index("9/10")
    assert classify(text, start, start + 4) is True

    far = "my tooth was painful for a very long time and I want 7/27"
    start = far.index("7/27")
    assert classify(far, start, start + 4) is False


@pytest.mark.parametrize("text,expected", [
    ("August 17 morning", "Mon 2026-08-17 morning"),
    ("July 27 morning", "Mon 2026-07-27 morning"),
    ("August 17 at 9am", "Mon 2026-08-17 9am"),
])
def test_month_name_behavior_is_unchanged_by_the_classifier(office, text, expected):
    assert chat_module.detect_time_window(text, CLIENT) == expected


@pytest.mark.parametrize("text", [
    "February 30 morning", "13/45 morning", "July 27, 2025 morning",
    "7/27/2025 morning", "February 30 at 9am",
])
def test_invalid_date_protections_are_unchanged(office, text):
    assert chat_module.detect_time_window(text, CLIENT) is None
    assert chat_module.canonicalize_time_window_for_storage(CLIENT, text) is None


# --- the real intake boundary ---------------------------------------------

@requires_db
@pytest.mark.parametrize("rating_phrase", ["pain 9/10 morning", "my pain is 7/10 and morning"])
def test_intake_never_stores_anything_from_a_pain_rating(db, rating_phrase):
    """A severity answer must not become a time window, must not overwrite
    the partial day already captured, and must not be echoed back as a
    saved value."""
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_email_opt_out=False, lead_is_new_patient=None,
    )
    conversation.lead_time_window = "Mon 2026-08-17"
    db.add(conversation)
    db.commit()

    resp = send(db, client, conversation, rating_phrase)

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert stored == "Mon 2026-08-17", f"partial day was overwritten: {stored!r}"
    assert stored != "morning"
    assert not ISO_IN_TEXT.search(resp.reply)

    saved = resp.meta.get("saved_time_window")
    if saved is not None:
        assert "Sep" not in saved and "Oct" not in saved, (
            f"a rating leaked into the reported window: {saved!r}"
        )
    assert "?" in resp.reply, "Mia must ask for a valid day/time again"


@requires_db
def test_intake_positive_control_numeric_date_still_completes(db):
    """The control for the test above: a real numeric date still works."""
    client = make_client(db, office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )
    target = datetime.now().date() + timedelta(days=14)

    send(db, client, conversation, f"{target.month}/{target.day} morning")

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert target.isoformat() in stored, f"numeric date not stored: {stored!r}"
    assert chat_module.time_window_is_complete(stored) is True


# ===========================================================================
# rev6: "of" is rating/fraction language, not a date preposition.
#
# "severity of 6/10" and "2/3 of my tooth broke" are everyday dental phrases.
# The "of" override let them bypass the rating classifier and become
# appointment dates. "on" and "for" remain date prepositions; "of" does not,
# and a part-of-whole immediately AFTER the token ("2/3 of my tooth") marks
# a fraction regardless of anything before it.
# ===========================================================================

OF_RATING_AND_FRACTION_PHRASES = [
    "severity of 6/10",
    "severity of 6/10 morning",
    "pain score of 8/10",
    "pain score of 8/10 at 9am",
    "rating of 9/10 morning",
    "pain of 7/10",
    "2/3 of my tooth broke",
    "2/3 of my tooth broke and morning",
    "1/2 of my filling came out",
    "3/4 of the crown broke",
]


@pytest.mark.parametrize("text", OF_RATING_AND_FRACTION_PHRASES)
def test_of_rating_and_fractions_never_become_dates(office, text):
    result = chat_module.detect_time_window(text, CLIENT)
    assert result is None, (
        f"{text!r} parsed as {result!r}; rating/fraction language was booked"
    )


@pytest.mark.parametrize("text", OF_RATING_AND_FRACTION_PHRASES)
def test_of_rating_and_fractions_are_not_storable(office, text):
    assert chat_module.canonicalize_time_window_for_storage(CLIENT, text) is None


@pytest.mark.parametrize("text", OF_RATING_AND_FRACTION_PHRASES)
def test_of_rating_and_fractions_are_rejected_matches(office, text):
    found = chat_module.find_explicit_calendar_date(text, CLIENT_NOW)
    assert found is not None, f"{text!r} reported NO_DATE_MATCH"
    assert found[0] is None, f"{text!r} resolved to a date: {found[0]!r}"


@pytest.mark.parametrize("text,expected", [
    ("on 7/27", "Mon 2026-07-27"),
    ("for 7/27", "Mon 2026-07-27"),
    ("appointment for 7/27 morning", "Mon 2026-07-27 morning"),
    ("severe pain, come in on 7/27 morning", "Mon 2026-07-27 morning"),
    ("7/27 morning", "Mon 2026-07-27 morning"),
])
def test_on_and_for_date_prepositions_still_win(office, text, expected):
    assert chat_module.detect_time_window(text, CLIENT) == expected


def test_fraction_follow_rule_beats_a_preceding_date_preposition(office):
    """Part-of-whole after the token outranks any preposition before it."""
    classify = chat_module._numeric_slash_is_rating_or_ratio
    text = "come in for 2/3 of my tooth broke"
    start = text.index("2/3")
    assert classify(text, start, start + 3) is True


# --- the real intake boundary ---------------------------------------------

@requires_db
@pytest.mark.parametrize("phrase,leak_months", [
    ("severity of 6/10 morning", ("Jun",)),
    ("2/3 of my tooth broke and morning", ("Feb",)),
])
def test_intake_never_stores_anything_from_of_ratings_or_fractions(
        db, phrase, leak_months):
    """Neither an 'of' rating nor a dental fraction may store a window,
    overwrite the partial day already captured, or surface a derived value.
    Mia must ask for a valid day/time again."""
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_email_opt_out=False, lead_is_new_patient=None,
    )
    conversation.lead_time_window = "Mon 2026-08-17"
    db.add(conversation)
    db.commit()

    resp = send(db, client, conversation, phrase)

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert stored == "Mon 2026-08-17", f"partial day was overwritten: {stored!r}"
    assert stored != "morning"
    assert not ISO_IN_TEXT.search(resp.reply)

    saved = resp.meta.get("saved_time_window")
    if saved is not None:
        for month in leak_months:
            assert month not in saved, (
                f"a rating/fraction leaked into the reported window: {saved!r}"
            )
    assert "?" in resp.reply, "Mia must ask for a valid day/time again"


@requires_db
def test_intake_positive_control_for_preposition_date_still_completes(db):
    """'appointment for 7/27 morning' still stores and completes."""
    client = make_client(db, office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )
    target = datetime.now().date() + timedelta(days=14)

    send(db, client, conversation,
         f"appointment for {target.month}/{target.day} morning")

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert target.isoformat() in stored, f"date not stored: {stored!r}"
    assert chat_module.time_window_is_complete(stored) is True


# ===========================================================================
# rev7: every explicit-date candidate is examined in textual order.
#
# Stopping at the first slash token starved real dates later in the same
# message: "pain 9/10, appointment on 7/27 morning" returned None. A rejected
# rating/fraction/invalid token is remembered, but a valid date anywhere
# after (or before) it wins. The time detail is tied to the SELECTED date's
# clause, so a rating clause's "9am" cannot leak onto the booked date.
# ===========================================================================

LATER_VALID_DATE_PHRASES = {
    "pain 9/10, appointment on 7/27 morning": "Mon 2026-07-27 morning",
    "my pain is 7/10 and I can come in on 7/27 morning": "Mon 2026-07-27 morning",
    "2/3 of my tooth broke; come in on 7/27 morning": "Mon 2026-07-27 morning",
    "severity of 6/10; appointment for 7/27 at 9am": "Mon 2026-07-27 9am",
    "13/45, use 7/27 morning": "Mon 2026-07-27 morning",
    "February 30, use 7/27 morning": "Mon 2026-07-27 morning",
    "pain 9/10 but July 30 morning works": "Thu 2026-07-30 morning",
}


@pytest.mark.parametrize("text,expected", sorted(LATER_VALID_DATE_PHRASES.items()))
def test_a_valid_date_after_a_rejected_token_wins(office, text, expected):
    assert chat_module.detect_time_window(text, CLIENT) == expected


@pytest.mark.parametrize("text,expected", sorted(LATER_VALID_DATE_PHRASES.items()))
def test_canonicalize_matches_for_later_valid_dates(office, text, expected):
    assert chat_module.canonicalize_time_window_for_storage(CLIENT, text) == expected


def test_detail_is_tied_to_the_selected_dates_clause(office):
    """The 9am belongs to the pain-rating clause. The booked date keeps the
    morning from its own clause."""
    assert chat_module.detect_time_window(
        "pain 9/10 at 9am; appointment on 7/27 morning", CLIENT
    ) == "Mon 2026-07-27 morning"


@pytest.mark.parametrize("text", [
    "pain 9/10 morning",
    "2/3 of my tooth broke and morning",
    "my pain is 7/10 and morning",
])
def test_rejected_tokens_with_no_valid_date_still_yield_nothing(office, text):
    assert chat_module.detect_time_window(text, CLIENT) is None
    assert chat_module.canonicalize_time_window_for_storage(CLIENT, text) is None


def test_multiple_valid_dates_select_the_first_in_textual_order(office):
    assert chat_module.detect_time_window(
        "7/27 morning or 8/3 afternoon", CLIENT
    ) == "Mon 2026-07-27 morning"


def test_a_valid_date_before_a_later_rating_is_unchanged(office):
    assert chat_module.detect_time_window(
        "on 7/27 morning, pain 9/10", CLIENT
    ) == "Mon 2026-07-27 morning"


def test_month_name_and_numeric_dates_obey_textual_order(office):
    """The numeric 7/27 appears first in the text, so it wins over the
    month-name August 3 even though month names used to have priority."""
    assert chat_module.detect_time_window(
        "use 7/27; August 3 also works", CLIENT
    ) == "Mon 2026-07-27"


def test_find_returns_the_chosen_later_dates_span(office):
    text = "pain 9/10, appointment on 7/27 morning"
    found = chat_module.find_explicit_calendar_date(text, CLIENT_NOW)
    assert found is not None
    assert found[0] == date(2026, 7, 27)
    assert "7/27" in text[found[1]:found[2]]
    assert "9/10" not in text[found[1]:found[2]]


def test_find_reports_the_first_rejected_span_when_nothing_is_valid(office):
    text = "pain 9/10 morning"
    found = chat_module.find_explicit_calendar_date(text, CLIENT_NOW)
    assert found is not None and found[0] is None
    assert "9/10" in text[found[1]:found[2]]


# --- the real intake boundary ---------------------------------------------

@requires_db
def test_intake_books_the_valid_date_after_a_pain_rating(db):
    """'my pain is 7/10 and I can come in on 7/27 morning' must store the
    canonical July 27 morning value and complete the field."""
    client = make_client(db, office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )
    target = datetime.now().date() + timedelta(days=14)

    resp = send(db, client, conversation,
                f"my pain is 7/10 and I can come in on {target.month}/{target.day} morning")

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert target.isoformat() in stored, f"valid later date not stored: {stored!r}"
    assert "morning" in stored
    assert chat_module.time_window_is_complete(stored) is True
    assert not ISO_IN_TEXT.search(resp.reply)


@requires_db
def test_intake_books_the_valid_date_after_a_fraction(db):
    """'2/3 of my tooth broke; appointment on 7/27 morning' must store the
    valid date - never February 3."""
    client = make_client(db, office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )
    target = datetime.now().date() + timedelta(days=14)

    resp = send(db, client, conversation,
                f"2/3 of my tooth broke; appointment on {target.month}/{target.day} morning")

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert target.isoformat() in stored, f"valid later date not stored: {stored!r}"
    assert "-02-03" not in stored, f"fraction was booked as February 3: {stored!r}"
    assert chat_module.time_window_is_complete(stored) is True
    assert not ISO_IN_TEXT.search(resp.reply)
    saved = resp.meta.get("saved_time_window")
    if saved is not None:
        assert not ISO_IN_TEXT.search(saved)


@requires_db
def test_intake_rating_only_phrase_still_stores_nothing_and_reasks(db):
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_email_opt_out=False, lead_is_new_patient=None,
    )
    conversation.lead_time_window = "Mon 2026-08-17"
    db.add(conversation)
    db.commit()

    resp = send(db, client, conversation, "pain 9/10 morning")

    db.refresh(conversation)
    assert conversation.lead_time_window == "Mon 2026-08-17"
    assert "?" in resp.reply, "Mia must ask for a valid day/time again"
    assert not ISO_IN_TEXT.search(resp.reply)


# ===========================================================================
# rev8: a time detail is attached to the selected date only when it provably
# belongs to that date's own region.
#
# The region after the selected date ends at the next candidate span (valid,
# invalid, rating, or fraction) or at a hard sentence boundary; an adjacent
# comma is natural punctuation. The pre-date fallback never crosses another
# candidate or the coordination introducing the selected clause. When
# ownership is ambiguous, the date is saved DAY-ONLY and the existing
# morning/afternoon follow-up asks for the time - a manufactured date+time
# combined from different clauses is worse than one extra question.
# ===========================================================================

DETAIL_ASSOCIATION_CASES = {
    "pain 9/10 at 9am and appointment on 7/27": "Mon 2026-07-27",
    "pain 9/10 at 9am appointment on 7/27": "Mon 2026-07-27",
    "pain 9/10 at 9am; appointment on 7/27 morning": "Mon 2026-07-27 morning",
    "appointment on 7/27 and pain 9/10 at 9am": "Mon 2026-07-27",
    "7/27 or 8/3 morning": "Mon 2026-07-27",
    "7/27 and 8/3 at 9am": "Mon 2026-07-27",
    "7/27 morning or 8/3 afternoon": "Mon 2026-07-27 morning",
    "7/27, morning please": "Mon 2026-07-27 morning",
    "appointment on 7/27, morning please": "Mon 2026-07-27 morning",
    "7/27, at 9am": "Mon 2026-07-27 9am",
    "morning on 7/27": "Mon 2026-07-27 morning",
    "9am on 7/27": "Mon 2026-07-27 9am",
}


@pytest.mark.parametrize("text,expected", sorted(DETAIL_ASSOCIATION_CASES.items()))
def test_detail_is_attached_only_within_the_selected_dates_region(office, text, expected):
    assert chat_module.detect_time_window(text, CLIENT) == expected


@pytest.mark.parametrize("text,expected", sorted(DETAIL_ASSOCIATION_CASES.items()))
def test_canonicalize_matches_detail_association(office, text, expected):
    assert chat_module.canonicalize_time_window_for_storage(CLIENT, text) == expected


def test_selected_span_remains_the_first_valid_date(office):
    text = "pain 9/10 at 9am and appointment on 7/27"
    found = chat_module.find_explicit_calendar_date(text, CLIENT_NOW)
    assert found is not None and found[0] == date(2026, 7, 27)
    assert "7/27" in text[found[1]:found[2]]


# --- the real intake boundary ---------------------------------------------

@requires_db
def test_intake_saves_day_only_when_the_time_belongs_to_the_rating(db):
    """'pain 9/10 at 9am and appointment on <date>' must store the date
    DAY-ONLY - never the rating clause's 9am - and ask for the time."""
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )
    target = a_future_weekday(14)

    resp = send(db, client, conversation,
                f"pain 9/10 at 9am and appointment on {target.month}/{target.day}")

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert target.isoformat() in stored, f"valid date not stored: {stored!r}"
    assert "9am" not in stored, f"the rating clause's time was borrowed: {stored!r}"
    assert chat_module.time_window_is_complete(stored) is False
    assert "morning or afternoon" in resp.reply.lower() or "?" in resp.reply
    assert not ISO_IN_TEXT.search(resp.reply)


@requires_db
def test_intake_does_not_combine_one_dates_day_with_anothers_detail(db):
    """'<date1> or <date2> morning' must not manufacture '<date1> morning'."""
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )
    first = a_future_weekday(14)
    second = datetime.now().date() + timedelta(days=21)

    send(db, client, conversation,
         f"{first.month}/{first.day} or {second.month}/{second.day} morning")

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert first.isoformat() in stored, f"first valid date not stored: {stored!r}"
    assert "morning" not in stored, (
        f"another candidate's detail was combined onto the first date: {stored!r}"
    )
    assert chat_module.time_window_is_complete(stored) is False


@requires_db
def test_intake_comma_phrasing_completes_in_one_turn(db):
    """'<date>, morning please' stores the complete value in one turn."""
    client = make_client(db, office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )
    target = datetime.now().date() + timedelta(days=14)

    resp = send(db, client, conversation,
                f"{target.month}/{target.day}, morning please")

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert target.isoformat() in stored, f"date not stored: {stored!r}"
    assert "morning" in stored
    assert chat_module.time_window_is_complete(stored) is True
    assert not ISO_IN_TEXT.search(resp.reply)
    saved = resp.meta.get("saved_time_window")
    if saved is not None:
        assert not ISO_IN_TEXT.search(saved)


# ===========================================================================
# rev9: a coordination word ends the post-date detail region.
#
# What follows "and" / "but" / "or" is a different clause, and its time
# belongs to that clause: "appointment on 7/27 and my pain started at 9am"
# is symptom narration, not a 9 AM booking. A detail BEFORE the coordination
# still belongs to the date. Nothing beyond the boundary is ever scanned -
# a safe extra morning/afternoon question beats guessing detail ownership.
# ===========================================================================

COORDINATION_BOUNDARY_CASES = {
    "appointment on 7/27 and my pain started at 9am": "Mon 2026-07-27",
    "appointment on 7/27 but my pain gets worse in the morning": "Mon 2026-07-27",
    "7/27 or call me at 9am": "Mon 2026-07-27",
    "7/27 morning and my pain started at 9am": "Mon 2026-07-27 morning",
    "7/27 at 9am but my pain is worse in the morning": "Mon 2026-07-27 9am",
    "appointment on 7/27 and call me in the afternoon": "Mon 2026-07-27",
    "7/27 and morning": "Mon 2026-07-27",
}

REV8_PRESERVATION_CASES = {
    "7/27, morning please": "Mon 2026-07-27 morning",
    "7/27, at 9am": "Mon 2026-07-27 9am",
    "morning on 7/27": "Mon 2026-07-27 morning",
    "9am on 7/27": "Mon 2026-07-27 9am",
    "pain 9/10 at 9am; appointment on 7/27 morning": "Mon 2026-07-27 morning",
    "7/27 or 8/3 morning": "Mon 2026-07-27",
}


@pytest.mark.parametrize("text,expected", sorted(COORDINATION_BOUNDARY_CASES.items()))
def test_coordination_word_ends_the_post_date_detail_region(office, text, expected):
    assert chat_module.detect_time_window(text, CLIENT) == expected


@pytest.mark.parametrize("text,expected", sorted(COORDINATION_BOUNDARY_CASES.items()))
def test_canonicalize_matches_coordination_boundary(office, text, expected):
    assert chat_module.canonicalize_time_window_for_storage(CLIENT, text) == expected


@pytest.mark.parametrize("text,expected", sorted(REV8_PRESERVATION_CASES.items()))
def test_rev8_association_behaviors_are_preserved(office, text, expected):
    assert chat_module.detect_time_window(text, CLIENT) == expected


# --- the real intake boundary ---------------------------------------------

@requires_db
def test_intake_symptom_narration_time_is_never_booked(db):
    """'appointment on <date> and my pain started at 9am' must store the
    date DAY-ONLY - the 9am is when the pain started, not the appointment -
    and the follow-up must ask for the time."""
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )
    target = a_future_weekday(14)

    resp = send(db, client, conversation,
                f"appointment on {target.month}/{target.day} and my pain started at 9am")

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert target.isoformat() in stored, f"date not stored: {stored!r}"
    assert "9am" not in stored, f"symptom narration time was booked: {stored!r}"
    assert chat_module.time_window_is_complete(stored) is False
    assert "morning or afternoon" in resp.reply.lower() or "?" in resp.reply
    assert not ISO_IN_TEXT.search(resp.reply)


@requires_db
def test_intake_detail_before_coordination_still_completes(db):
    """'<date> morning and my pain started at 9am' keeps the morning that
    sits before the coordination and completes in one turn."""
    client = make_client(db, office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain", lead_name="Kyle", lead_phone="516-555-5555",
        lead_time_window=None, lead_email_opt_out=False, lead_is_new_patient=None,
    )
    target = datetime.now().date() + timedelta(days=14)

    resp = send(db, client, conversation,
                f"{target.month}/{target.day} morning and my pain started at 9am")

    db.refresh(conversation)
    stored = conversation.lead_time_window or ""
    assert target.isoformat() in stored, f"date not stored: {stored!r}"
    assert "morning" in stored
    assert "9am" not in stored
    assert chat_module.time_window_is_complete(stored) is True
    assert not ISO_IN_TEXT.search(resp.reply)
    saved = resp.meta.get("saved_time_window")
    if saved is not None:
        assert not ISO_IN_TEXT.search(saved)
