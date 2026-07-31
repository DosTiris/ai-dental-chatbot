# calendar_tests/test_client_enabled_services.py
#
# Prototype B B2 — commit candidate 1: enabled-service owner extraction.
#
# Proves that get_client_enabled_service_keys, moved BYTE-FOR-BYTE from
# app/routes/chat.py into app/services/mia_service_library.py, preserves
# every documented behavior:
#   - explicit settings.enabled_services list (order, trimming, coercion,
#     blank-item dropping);
#   - specialty-preset fallback (including the normalization pathway);
#   - the general default (returned as the SAME module constant — identity,
#     not just equality — matching the pre-move return semantics);
#   - malformed / missing settings behavior;
# and that ownership is truly single (Rule 3):
#   - chat.py binds the shared owner and defines NO duplicate (AST-proven);
#   - the B2 calendar route binds the SAME object;
#   - existing widget service-button choices are behaviorally unchanged.
#
# Run: pytest calendar_tests/test_client_enabled_services.py -v
# Pure fixtures only: no database rows, no HTTP, no network. (Importing the
# route modules requires the same environment the existing suite already
# uses — calendar_tests/conftest.py supplies the harmless env defaults.)

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.routes.calendar as calendar_module  # noqa: E402
import app.routes.chat as chat_module  # noqa: E402
from app.services.mia_service_library import (  # noqa: E402
    DEFAULT_ENABLED_SERVICE_KEYS,
    MASTER_DENTAL_SERVICES,
    SPECIALTY_PRESETS,
    get_client_enabled_service_keys,
)


def stub_client(settings=None):
    """The helper reads ONLY getattr(client, 'settings', None), so a plain
    namespace is a faithful stand-in for the ORM Client row."""
    return SimpleNamespace(settings=settings)


# ---------------------------------------------------------------------------
# 1. Explicit enabled-service list preserved
# ---------------------------------------------------------------------------

def test_explicit_enabled_service_list_is_preserved():
    # Order kept; surrounding whitespace trimmed; blank/None items dropped;
    # non-string items pass through str() — exactly the pre-move cleaning.
    result = get_client_enabled_service_keys(stub_client({
        "enabled_services": ["braces", "  invisalign  ", "", None, 7],
    }))
    assert result == ["braces", "invisalign", "7"]


def test_explicit_list_wins_over_specialty_preset():
    result = get_client_enabled_service_keys(stub_client({
        "enabled_services": ["braces"],
        "practice_specialty": "pediatric",
    }))
    assert result == ["braces"]


def test_explicit_list_of_only_blanks_falls_through_to_specialty():
    result = get_client_enabled_service_keys(stub_client({
        "enabled_services": ["", "   ", None],
        "practice_specialty": "pediatric",
    }))
    assert result is SPECIALTY_PRESETS["pediatric"]


# ---------------------------------------------------------------------------
# 2. Specialty-preset fallback preserved
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("stored_specialty", "preset_key"),
    [
        ("pediatric", "pediatric"),
        ("Pediatric", "pediatric"),          # lowercased by normalization
        ("  pediatric  ", "pediatric"),      # trimmed by normalization
        ("Oral-Surgery", "oral_surgery"),    # '-' -> ' ' -> '_' pathway
        ("general", "general"),
    ],
)
def test_specialty_preset_fallback_preserved(stored_specialty, preset_key):
    result = get_client_enabled_service_keys(stub_client({
        "practice_specialty": stored_specialty,
    }))
    # IDENTITY, not equality: the pre-move helper returned the preset list
    # object itself, and the moved owner must keep doing so.
    assert result is SPECIALTY_PRESETS[preset_key]


def test_unknown_specialty_returns_general_default():
    result = get_client_enabled_service_keys(stub_client({
        "practice_specialty": "cardiology",
    }))
    assert result is DEFAULT_ENABLED_SERVICE_KEYS


def test_empty_settings_dict_uses_general_preset():
    # No enabled_services and no specialty -> 'general' -> the preset that
    # IS the default constant (documented pre-move equivalence).
    result = get_client_enabled_service_keys(stub_client({}))
    assert result is DEFAULT_ENABLED_SERVICE_KEYS


# ---------------------------------------------------------------------------
# 3. Missing / malformed settings preserved
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "settings_value",
    [None, [], "not-a-dict", 5, ("tuple",)],
)
def test_non_dict_settings_return_general_default(settings_value):
    result = get_client_enabled_service_keys(stub_client(settings_value))
    assert result is DEFAULT_ENABLED_SERVICE_KEYS


def test_object_without_settings_attribute_returns_general_default():
    result = get_client_enabled_service_keys(object())
    assert result is DEFAULT_ENABLED_SERVICE_KEYS


def test_non_list_enabled_services_falls_through():
    # A string (or any non-list) enabled_services is ignored, exactly as
    # before the move: the specialty/default pathway decides instead.
    result = get_client_enabled_service_keys(stub_client({
        "enabled_services": "braces",
    }))
    assert result is DEFAULT_ENABLED_SERVICE_KEYS


# ---------------------------------------------------------------------------
# 4. Single ownership (Rule 3): both routes bind the SAME shared owner
# ---------------------------------------------------------------------------

def test_chat_binds_the_shared_owner_by_name():
    assert (
        chat_module.get_client_enabled_service_keys
        is get_client_enabled_service_keys
    )


def test_calendar_route_binds_the_same_shared_owner():
    assert (
        calendar_module.get_client_enabled_service_keys
        is get_client_enabled_service_keys
    )


def test_chat_source_defines_no_duplicate_helper():
    # AST proof against the REAL on-disk source: no function definition of
    # any nesting depth named get_client_enabled_service_keys remains.
    source = Path(chat_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    duplicate_defs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "get_client_enabled_service_keys"
    ]
    assert duplicate_defs == []


def test_chat_no_longer_imports_the_moved_only_names():
    # DEFAULT_ENABLED_SERVICE_KEYS, SPECIALTY_PRESETS, and
    # normalize_service_text served ONLY the moved helper inside chat.py;
    # the extraction removed those now-dead imports (documented in the
    # implementation report). Their single owner is unchanged.
    assert not hasattr(chat_module, "DEFAULT_ENABLED_SERVICE_KEYS")
    assert not hasattr(chat_module, "SPECIALTY_PRESETS")
    assert not hasattr(chat_module, "normalize_service_text")


# ---------------------------------------------------------------------------
# 5. Existing widget service choices behaviorally unchanged
# ---------------------------------------------------------------------------

def test_widget_service_buttons_behaviorally_unchanged():
    # The widget button builder consumes the shared owner exactly as it
    # consumed the local helper: only enabled services among the visible
    # defaults render, 'other' always renders, and the message wording for
    # each rendered key is the exact pre-move wording.
    client = stub_client({
        "enabled_services": ["cleaning_checkup", "tooth_pain"],
    })
    buttons = chat_module.build_widget_service_buttons(client)

    assert [b["key"] for b in buttons] == [
        "cleaning_checkup", "tooth_pain", "other",
    ]
    cleaning_label = MASTER_DENTAL_SERVICES["cleaning_checkup"].display_name
    assert buttons[0]["label"] == cleaning_label
    assert buttons[0]["message"] == f"I need {cleaning_label.lower()}"
    assert buttons[1]["message"] == "I have tooth pain"
    assert buttons[2] == {"key": "other", "label": "Other", "message": "Other"}


def test_disabled_service_detection_still_uses_the_shared_owner():
    # detect_disabled_library_service_for_client calls the shared owner:
    # implants exists in the master library but is NOT enabled for this
    # client, so it is reported as a disabled-service match — the exact
    # pre-move behavior of the widget's 'not offered here' pathway.
    client = stub_client({"enabled_services": ["cleaning_checkup"]})
    match = chat_module.detect_disabled_library_service_for_client(
        client, "do you do implants?"
    )
    assert match is not None
    assert match.key == "implants"
