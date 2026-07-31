# calendar_tests/test_service_policy_mapping.py
#
# Focused regression tests for the service-policy mapping-owner extraction
# (pre-B2 prerequisite). Proves, against the LOCKED implementation spec:
#
#   - the runtime mapping is exactly the frozen 37-entry vocabulary,
#   - the public mapping is read-only,
#   - mapping keys equal all non-admin_other master library keys (this test
#     file -- not the runtime module -- is the vocabulary-drift gate, so a
#     future drift fails tests loudly instead of crashing app startup),
#   - the lookup contract (trim, case-sensitive, None for everything else),
#   - the new module's import purity (no routes, no DB/HTTP/booking deps),
#   - chat.py no longer owns the dictionary but keeps its generic fallback,
#   - representative recognized chat flows are byte-identical to before,
#   - chat_rebuild.py (dead tracked legacy duplicate, [DEV-MAP-OWNER-001])
#     is neither imported nor touched.
#
# The EXPECTED_MAPPING literal below intentionally duplicates the runtime
# dictionary. That duplication is allowed ONLY here, as a regression oracle
# (locked spec); it must never be imported by runtime code and must not
# become a second runtime owner.

import ast
import sys
from pathlib import Path

import pytest

import app.routes.chat as chat_module
from app.services import service_policy_mapping as mapping_module
from app.services.mia_service_library import MASTER_DENTAL_SERVICES
from app.services.service_policy_mapping import (
    MASTER_SERVICE_TO_CALENDAR_POLICY,
    calendar_policy_value_for_master_service,
)

# ---------------------------------------------------------------------------
# Frozen oracle: the exact 37 pairs extracted from chat.py at HEAD 8c08376.
# ---------------------------------------------------------------------------
EXPECTED_MAPPING = {
    "dental_consultation": "appointment request",
    "new_patient_exam": "cleaning/checkup",
    "follow_up": "appointment request",
    "cleaning_checkup": "cleaning/checkup",
    "deep_cleaning": "cleaning/checkup",
    "x_rays": "appointment request",
    "fluoride": "cleaning/checkup",
    "sealants": "cleaning/checkup",
    "tooth_pain": "tooth pain",
    "broken_tooth": "broken tooth/filling",
    "swelling_abscess": "tooth pain",
    "lost_crown_filling": "broken tooth/filling",
    "fillings": "broken tooth/filling",
    "crowns": "crown",
    "bridges": "crown",
    "bonding": "cosmetic/whitening",
    "root_canal": "appointment request",
    "tooth_extraction": "extraction/implant",
    "wisdom_tooth": "extraction/implant",
    "bone_graft": "extraction/implant",
    "teeth_whitening": "cosmetic/whitening",
    "veneers": "cosmetic/whitening",
    "smile_makeover": "cosmetic/whitening",
    "braces": "orthodontics",
    "invisalign": "orthodontics",
    "retainers": "orthodontics",
    "implants": "extraction/implant",
    "dentures": "appointment request",
    "gum_disease": "appointment request",
    "gum_grafting": "appointment request",
    "child_cleaning": "cleaning/checkup",
    "child_cavity": "broken tooth/filling",
    "space_maintainer": "appointment request",
    "tmj": "tooth pain",
    "night_guard": "appointment request",
    "oral_cancer_screening": "appointment request",
    "sleep_apnea_appliance": "appointment request",
}

ADMIN_OTHER_KEYS = [
    "insurance_question",
    "payment_financing",
    "prescription_question",
    "records_request",
]


def _mapping_module_ast() -> ast.Module:
    """Parse the runtime mapping module's real on-disk source for import
    inspection. Reading the file (rather than trusting sys.modules) proves
    the SHIPPED source is pure, not merely the loaded interpreter state."""
    source_path = Path(mapping_module.__file__)
    return ast.parse(source_path.read_text(encoding="utf-8"))


def _imported_module_names(tree: ast.Module) -> set[str]:
    """Collect every module name touched by import statements in the tree."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # node.module is None only for relative "from . import x"; the
            # mapping module must not use relative imports at all.
            assert node.level == 0, "relative import found in mapping module"
            names.add(node.module or "")
    return names


# ---------------------------------------------------------------------------
# 1-2. Exact frozen vocabulary and read-only exposure
# ---------------------------------------------------------------------------

def test_mapping_equals_exact_37_entry_oracle():
    # dict() materializes the read-only proxy for comparison.
    assert dict(MASTER_SERVICE_TO_CALENDAR_POLICY) == EXPECTED_MAPPING
    assert len(MASTER_SERVICE_TO_CALENDAR_POLICY) == 37


def test_public_mapping_is_read_only():
    with pytest.raises(TypeError):
        MASTER_SERVICE_TO_CALENDAR_POLICY["tooth_pain"] = "tampered"  # type: ignore[index]


# ---------------------------------------------------------------------------
# 3. Vocabulary-drift gate: mapping keys == non-admin_other master keys
# ---------------------------------------------------------------------------

def test_mapping_keys_equal_non_admin_master_keys():
    non_admin_master_keys = {
        key
        for key, service in MASTER_DENTAL_SERVICES.items()
        if service.category != "admin_other"
    }
    assert set(MASTER_SERVICE_TO_CALENDAR_POLICY) == non_admin_master_keys


# ---------------------------------------------------------------------------
# 4. admin_other keys must never translate into an appointment lead reason
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("admin_key", ADMIN_OTHER_KEYS)
def test_admin_other_keys_return_none(admin_key):
    assert calendar_policy_value_for_master_service(admin_key) is None


# ---------------------------------------------------------------------------
# 5. Representative exact pairs (locked spec examples)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("master_key", "expected_policy_value"),
    [
        ("cleaning_checkup", "cleaning/checkup"),
        ("tooth_pain", "tooth pain"),
        ("root_canal", "appointment request"),
        ("implants", "extraction/implant"),
        ("dentures", "appointment request"),
    ],
)
def test_representative_pairs_exact(master_key, expected_policy_value):
    assert (
        calendar_policy_value_for_master_service(master_key)
        == expected_policy_value
    )


# ---------------------------------------------------------------------------
# 6. Everything that is not a known trimmed key answers None -- never raises
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_input",
    [
        "",                # blank
        "   ",             # whitespace-only
        "no_such_service", # unknown key
        None,              # non-string
        42,                # non-string
        b"tooth_pain",     # bytes are non-string on purpose
        "TOOTH_PAIN",      # case mismatch (lookup is case-sensitive)
        "Tooth_Pain",      # case mismatch
        "tooth pain",      # a POLICY VALUE is not a master key
    ],
)
def test_invalid_or_unsupported_inputs_return_none(bad_input):
    assert calendar_policy_value_for_master_service(bad_input) is None


# ---------------------------------------------------------------------------
# 7. Trim contract: the ONE documented deviation from raw dict.get
# ---------------------------------------------------------------------------

def test_surrounding_whitespace_is_trimmed_before_lookup():
    # Deliberate deviation, documented in the module docstring: invisible to
    # chat (canonical dataclass keys), relevant to the future B2 route.
    assert calendar_policy_value_for_master_service("  tooth_pain  ") == "tooth pain"
    assert calendar_policy_value_for_master_service("\ttooth_pain\n") == "tooth pain"


# ---------------------------------------------------------------------------
# 8-9. Import purity of the shipped runtime module
# ---------------------------------------------------------------------------

def test_mapping_module_imports_no_route_module():
    imported = _imported_module_names(_mapping_module_ast())
    route_imports = {name for name in imported if name.startswith("app.routes")}
    assert route_imports == set()


def test_mapping_module_is_stdlib_only_and_has_no_forbidden_dependency():
    imported = _imported_module_names(_mapping_module_ast())

    # Strong form: the ONLY imports allowed in the runtime owner.
    assert imported <= {"__future__", "types", "typing"}, imported

    # Belt-and-suspenders forbidden scan required by the locked spec.
    forbidden_prefixes = (
        "sqlalchemy",
        "fastapi",
        "starlette",
        "pydantic",
        "twilio",
        "resend",
        "httpx",
        "requests",
        "psycopg2",
        "app.database",
        "app.models",
        "app.calendar_models",
        "app.repositories",
        "app.routes",
        "app.services.booking_service",
        "app.services.booking_conversation",
        "app.services.appointment_hold_service",
        "app.services.notification_service",
        "app.services.mia_service_library",  # runtime import forbidden by design
    )
    offenders = {
        name
        for name in imported
        if any(name == p or name.startswith(p + ".") for p in forbidden_prefixes)
    }
    assert offenders == set()


# ---------------------------------------------------------------------------
# 10. chat.py no longer owns the dictionary
# ---------------------------------------------------------------------------

def test_chat_no_longer_defines_service_library_to_legacy_reason():
    assert not hasattr(chat_module, "SERVICE_LIBRARY_TO_LEGACY_REASON")


def test_chat_binds_the_lookup_function_by_name():
    # The function-only, by-name import is a locked contract item: it keeps a
    # single access pathway and makes the fallback test below patchable.
    assert (
        chat_module.calendar_policy_value_for_master_service
        is calendar_policy_value_for_master_service
    )
    assert not hasattr(chat_module, "MASTER_SERVICE_TO_CALENDAR_POLICY")


# ---------------------------------------------------------------------------
# 11-12. Chat-owned generic fallback, proven WITHOUT mutating the vocabulary
# ---------------------------------------------------------------------------

def test_chat_falls_back_to_generic_reason_when_mapping_returns_none(monkeypatch):
    # Every non-admin master key is currently mapped, so this branch is
    # unreachable with real data. The LOCKED mechanism is to monkeypatch the
    # chat-bound lookup; adding/removing library entries to reach the branch
    # is forbidden (would mutate a closed vocabulary to satisfy a test).
    master_keys_before = set(MASTER_DENTAL_SERVICES)

    monkeypatch.setattr(
        chat_module,
        "calendar_policy_value_for_master_service",
        lambda service_key: None,
    )

    # "i need a cleaning" genuinely matches cleaning_checkup via the master
    # library aliases, so detection succeeds and only the mapping is simulated.
    assert (
        chat_module.detect_library_service_reason("i need a cleaning")
        == "appointment request"
    )

    # Prove the fallback was reached without vocabulary mutation.
    assert set(MASTER_DENTAL_SERVICES) == master_keys_before


# ---------------------------------------------------------------------------
# 13. Representative recognized chat flows keep their exact prior outcomes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("patient_text", "expected_reason"),
    [
        ("my tooth hurts", "tooth pain"),
        ("i want invisalign", "orthodontics"),
        ("i need a cleaning", "cleaning/checkup"),
        ("do you take my insurance", None),  # admin_other never becomes a lead
    ],
)
def test_recognized_chat_flows_unchanged(patient_text, expected_reason):
    assert (
        chat_module.detect_library_service_reason(patient_text)
        == expected_reason
    )


# ---------------------------------------------------------------------------
# 14. The dead legacy duplicate stays dead ([DEV-MAP-OWNER-001])
# ---------------------------------------------------------------------------

def test_chat_rebuild_is_not_imported_by_this_extraction():
    assert "app.routes.chat_rebuild" not in sys.modules
