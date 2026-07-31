"""
Service-policy mapping owner.

Purpose:
- Single runtime owner (Rule 3) of the master-service-key -> Calendar-policy
  translation that previously lived inside app/routes/chat.py as the private
  SERVICE_LIBRARY_TO_LEGACY_REASON dictionary.
- Lets the existing /chat flow and the future B2 service route share ONE
  mapping without a route importing another route (Rule 6) and without
  duplicating the dictionary (Rule 3).

Vocabularies bridged (both pre-existing; neither is defined here):
- INPUT:  master service library keys from app/services/mia_service_library.py
          (e.g. "cleaning_checkup", "tooth_pain").
- OUTPUT: existing Calendar-policy / legacy lead-reason values compared by raw
          equality elsewhere (e.g. "cleaning/checkup", "tooth pain").

Deliberately NOT here (locked design):
- No fallback. Unknown keys answer None; the generic "appointment request"
  fallback is chat-owned so a future B2 route can REJECT unmapped keys
  instead of silently showing generic appointment slots.
- No import of mia_service_library at runtime. The invariant "mapping keys ==
  all non-admin_other master keys" is enforced by the focused regression test
  (calendar_tests/test_service_policy_mapping.py), not at import time, so a
  future vocabulary drift degrades gracefully through chat's fallback instead
  of crashing application startup.
- No chat, intake, booking, hold, notification, HTTP, database, logging, or
  route behavior of any kind. Pure stdlib; pure lookup.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

# Private backing dictionary. Content is byte-for-byte the 37 pairs extracted
# from app/routes/chat.py at HEAD 8c08376 (SERVICE_LIBRARY_TO_LEGACY_REASON).
# Closed vocabulary (Rule 16): no key or value may be added, removed, renamed,
# or reclassified without its own approved patch. The four admin_other master
# keys (insurance_question, payment_financing, prescription_question,
# records_request) are intentionally absent: admin/office questions must never
# become appointment leads, so lookups for them answer None.
_MASTER_SERVICE_TO_CALENDAR_POLICY: dict[str, str] = {
    # General
    "dental_consultation": "appointment request",
    "new_patient_exam": "cleaning/checkup",
    "follow_up": "appointment request",

    # Preventive / diagnostic
    "cleaning_checkup": "cleaning/checkup",
    "deep_cleaning": "cleaning/checkup",
    "x_rays": "appointment request",
    "fluoride": "cleaning/checkup",
    "sealants": "cleaning/checkup",

    # Urgent / symptoms
    "tooth_pain": "tooth pain",
    "broken_tooth": "broken tooth/filling",
    "swelling_abscess": "tooth pain",
    "lost_crown_filling": "broken tooth/filling",

    # Restorative
    "fillings": "broken tooth/filling",
    "crowns": "crown",
    "bridges": "crown",
    "bonding": "cosmetic/whitening",

    # Endodontic
    "root_canal": "appointment request",

    # Oral surgery
    "tooth_extraction": "extraction/implant",
    "wisdom_tooth": "extraction/implant",
    "bone_graft": "extraction/implant",

    # Cosmetic
    "teeth_whitening": "cosmetic/whitening",
    "veneers": "cosmetic/whitening",
    "smile_makeover": "cosmetic/whitening",

    # Orthodontic
    "braces": "orthodontics",
    "invisalign": "orthodontics",
    "retainers": "orthodontics",

    # Implants / dentures
    "implants": "extraction/implant",
    "dentures": "appointment request",

    # Periodontic
    "gum_disease": "appointment request",
    "gum_grafting": "appointment request",

    # Pediatric
    "child_cleaning": "cleaning/checkup",
    "child_cavity": "broken tooth/filling",
    "space_maintainer": "appointment request",

    # TMJ / oral medicine
    "tmj": "tooth pain",
    "night_guard": "appointment request",
    "oral_cancer_screening": "appointment request",

    # Sleep
    "sleep_apnea_appliance": "appointment request",
}

# Public read-only view (Rule 4: no hidden mutation pathway). Item assignment
# on this object raises TypeError, so no caller can silently drift the
# vocabulary at runtime.
MASTER_SERVICE_TO_CALENDAR_POLICY: Mapping[str, str] = MappingProxyType(
    _MASTER_SERVICE_TO_CALENDAR_POLICY
)


def calendar_policy_value_for_master_service(service_key: object) -> str | None:
    """
    Translate ONE master-library service key into the exact existing
    Calendar-policy vocabulary value.

    Inputs:
        service_key: expected to be a master service key such as "tooth_pain".
            Annotated as object on purpose: ANY input is tolerated and any
            non-string input is answered with None rather than an exception.

    Returns:
        The mapped Calendar-policy value for a known key, or None for blank,
        whitespace-only, unknown, non-string, admin_other, case-mismatched,
        or otherwise unsupported input.

    Possible failures:
        None raised. Unknowns are reported as None; each caller decides what
        that means (chat applies its own generic fallback; future B2 rejects).

    Database effects: none.
    External effects: none (no logging, no HTTP, no mutation of any state).

    Deliberate deviation from raw dict.get semantics (locked lookup contract):
    surrounding whitespace is trimmed BEFORE the case-sensitive lookup. This
    is invisible to chat today -- matched_service.key always comes from the
    frozen DentalService dataclass and is canonical -- and exists for the
    future B2 route, where the key arrives from an HTTP request and may carry
    accidental padding. No other normalization is performed.
    """
    if not isinstance(service_key, str):
        return None

    trimmed = service_key.strip()
    if not trimmed:
        return None

    return MASTER_SERVICE_TO_CALENDAR_POLICY.get(trimmed)
