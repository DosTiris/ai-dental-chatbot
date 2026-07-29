# calendar_tests/test_maps_action.py
#
# S2 Maps backport regression tests.
#
# Section 1 (no database): validation matrix for the two single-owner Maps
# functions backported from verified production — get_verified_maps_url()
# and build_map_action(). These prove the allowlist, HTTPS enforcement,
# lookalike rejection, fail-safe absence, and that no URL is ever derived
# from the office's written address.
#
# Section 2 (requires_db, same conftest fixtures as test_booking_db.py):
# chat-flow proof that a location question returns the address answer with
# meta["map_action"] attached, that a standalone location question does not
# start intake, and that a location question during an active Calendar
# booking dialog yields, answers with the Maps action, and leaves the
# booking state untouched (the existing interruption/resume owner).
#
# Run (with the rest of the suite):
#   TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/postgres \
#       pytest calendar_tests -v

import uuid
from types import SimpleNamespace

import pytest

from calendar_tests.conftest import requires_db

# Importing the route module exercises the new urlparse import as well.
from app.routes.chat import (
    APPROVED_MAPS_HOSTS,
    MAPS_BUTTON_LABEL,
    build_map_action,
    get_verified_maps_url,
)


def _client_with(settings) -> SimpleNamespace:
    """Minimal client stand-in: get_verified_maps_url reads only
    client.settings (a dict) through get_client_setting()."""
    return SimpleNamespace(settings=settings)


# ---------------------------------------------------------------------------
# Section 1 — validation matrix (no DB)
# ---------------------------------------------------------------------------

def test_approved_share_link_produces_action():
    # Matrix item 1: approved configured URL -> correct backend action.
    url = "https://maps.app.goo.gl/AbCdEf123"
    client = _client_with({"maps_url": url})
    assert get_verified_maps_url(client) == url
    action = build_map_action(client)
    assert action == {
        "type": "external_link",
        "url": url,
        "label": MAPS_BUTTON_LABEL,
        "target": "_blank",
        "rel": "noopener noreferrer",
    }


def test_allowlist_is_exactly_the_verified_production_set():
    # Matrix item 2: only the verified production hosts are approved.
    assert APPROVED_MAPS_HOSTS == {
        "maps.app.goo.gl": True,
        "maps.google.com": True,
        "www.google.com": "/maps",
        "google.com": "/maps",
    }
    # maps.google.com accepts any HTTPS path.
    ok = _client_with({"maps_url": "https://maps.google.com/?cid=123"})
    assert get_verified_maps_url(ok) != ""
    # google.com is approved ONLY for /maps paths.
    good_path = _client_with({"maps_url": "https://www.google.com/maps/place/x"})
    assert get_verified_maps_url(good_path) != ""
    bare = _client_with({"maps_url": "https://www.google.com/"})
    assert get_verified_maps_url(bare) == ""
    lookalike_path = _client_with({"maps_url": "https://www.google.com/mapsearch"})
    assert get_verified_maps_url(lookalike_path) == ""
    # goo.gl (legacy shortener) is intentionally NOT approved.
    legacy = _client_with({"maps_url": "https://goo.gl/maps/abc"})
    assert get_verified_maps_url(legacy) == ""


def test_non_https_rejected():
    # Matrix item 3: every non-HTTPS scheme fails safely.
    for bad in [
        "http://maps.app.goo.gl/AbCdEf123",
        "javascript:alert(1)",
        "data:text/html,x",
        "ftp://maps.google.com/x",
        "//maps.google.com/x",
    ]:
        client = _client_with({"maps_url": bad})
        assert get_verified_maps_url(client) == "", bad
        assert build_map_action(client) is None, bad


def test_lookalike_and_deceptive_hosts_rejected():
    # Matrix item 4: suffix/prefix lookalikes and userinfo tricks fail.
    for bad in [
        "https://maps.google.com.evil.example/x",
        "https://evilmaps.app.goo.gl.attacker.net/x",
        "https://maps-google.com/x",
        "https://maps.google.com@evil.example/x",  # userinfo deception
    ]:
        client = _client_with({"maps_url": bad})
        assert get_verified_maps_url(client) == "", bad


def test_arbitrary_non_google_host_rejected():
    # Matrix item 5.
    for bad in [
        "https://example.com/maps",
        "https://openstreetmap.org/x",
        "https://bing.com/maps",
    ]:
        client = _client_with({"maps_url": bad})
        assert get_verified_maps_url(client) == "", bad


def test_absent_blank_or_malformed_config_produces_no_action():
    # Matrix item 6: fail-safe absence, never an exception.
    for settings in [
        None,                       # client has no settings dict at all
        {},                         # no maps_url key
        {"maps_url": ""},           # blank
        {"maps_url": "   "},        # whitespace
        {"maps_url": 12345},        # wrong type
        {"maps_url": "ht!tp:/bad url"},  # malformed
    ]:
        client = _client_with(settings)
        assert get_verified_maps_url(client) == ""
        assert build_map_action(client) is None


def test_url_is_never_generated_from_the_practice_address():
    # Matrix item 7: an office with a full written address but no
    # configured maps_url gets NO action — nothing is derived or guessed.
    client = SimpleNamespace(
        settings={"address": "123 Main Street, Lynbrook, NY 11563"},
        address="123 Main Street, Lynbrook, NY 11563",
        practice_name="Test Dental",
    )
    assert get_verified_maps_url(client) == ""
    assert build_map_action(client) is None


# ---------------------------------------------------------------------------
# Section 2 — chat-flow proof (requires_db)
# ---------------------------------------------------------------------------

LOCATION_FAQ_ANSWER = "We are located at 123 Main Street, Lynbrook, NY 11563."
MAPS_URL = "https://maps.app.goo.gl/TestShareLink1"


class _StubRequest:
    """chat() reads only request.client.host."""
    client = SimpleNamespace(host="127.0.0.1")


def _add_location_faq(db, client_row):
    from app.models import ClientFAQ
    faq = ClientFAQ(
        id=uuid.uuid4(),
        client_id=client_row.id,
        question="Where are you located",
        answer=LOCATION_FAQ_ANSWER,
        keywords="address,location,directions,parking",
        enabled=True,
    )
    db.add(faq)
    db.commit()
    return faq


def _configure_maps(db, client_row, url=MAPS_URL):
    # settings is a JSON column: reassign the dict so SQLAlchemy sees it.
    settings = dict(client_row.settings or {})
    settings["maps_url"] = url
    client_row.settings = settings
    db.add(client_row)
    db.commit()


def _chat(db, client_row, text, conversation_id=None):
    from app.routes.chat import chat
    from app.schemas import ChatRequest
    req = ChatRequest(
        message=text,
        client_key=client_row.api_key,
        conversation_id=str(conversation_id) if conversation_id else None,
    )
    return chat(req, _StubRequest(), db)


@requires_db
def test_standalone_location_question_returns_maps_and_does_not_start_intake(db, client_row):
    # Matrix items 8 and 1 (endpoint level).
    from app.calendar_models import BookingState
    from app.models import Conversation
    _add_location_faq(db, client_row)
    _configure_maps(db, client_row)

    resp = _chat(db, client_row, "What is your address?")

    assert LOCATION_FAQ_ANSWER.split(",")[0] in resp.reply
    action = (resp.meta or {}).get("map_action")
    assert action is not None
    assert action["url"] == MAPS_URL
    assert action["label"] == MAPS_BUTTON_LABEL
    assert action["rel"] == "noopener noreferrer"

    conv = (
        db.query(Conversation)
        .filter(Conversation.id == uuid.UUID(resp.conversation_id))
        .first()
    )
    # No intake, no booking dialog started by a pure information question.
    state = getattr(conv, "booking_state", BookingState.NONE) or BookingState.NONE
    assert state == BookingState.NONE
    assert not bool(conv.is_lead)


@requires_db
def test_location_question_without_configured_maps_url_has_no_action(db, client_row):
    # Matrix item 6 (endpoint level): address answer still works, no button.
    _add_location_faq(db, client_row)  # maps_url deliberately NOT configured

    resp = _chat(db, client_row, "Where are you located?")

    assert "123 Main Street" in resp.reply
    assert "map_action" not in (resp.meta or {})


@requires_db
def test_mid_booking_location_interruption_yields_answers_with_maps_and_preserves_state(
    db, client_row, conversation_row
):
    # Matrix item 9, calendar-native: the Calendar dialog yields for a
    # location question (is_information_interruption), the operational
    # answer carries map_action, and booking_state is untouched — the
    # existing single-owner interruption/resume behavior, unmodified.
    from app.calendar_models import BookingState
    from app.models import Conversation
    _add_location_faq(db, client_row)
    _configure_maps(db, client_row)

    # Place the conversation mid-dialog exactly as booking_conversation
    # would leave it between turns (state field only; owner unmodified).
    conversation_row.booking_state = BookingState.WAITING_FOR_DATE
    db.add(conversation_row)
    db.commit()

    resp = _chat(db, client_row, "Actually, what is your address?",
                 conversation_id=conversation_row.id)

    action = (resp.meta or {}).get("map_action")
    assert action is not None and action["url"] == MAPS_URL
    assert "123 Main Street" in resp.reply

    db.refresh(conversation_row)
    refreshed_state = (
        getattr(conversation_row, "booking_state", BookingState.NONE)
        or BookingState.NONE
    )
    # The dialog was not cancelled, advanced, or reset by the interruption.
    assert refreshed_state == BookingState.WAITING_FOR_DATE
