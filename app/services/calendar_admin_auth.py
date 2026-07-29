# app/services/calendar_admin_auth.py
#
# OWNER OF: Calendar admin authorization (Patch 5 — Senior Audit Critical #2).
#
# This module is the SINGLE owner (Rule 3) of every rule that decides whether
# a Calendar-admin request is allowed to act, and for WHICH office:
#
#   - raw-key format validation        (RAW_KEY_PATTERN)
#   - SHA-256 hashing                  (hash_calendar_admin_key)
#   - credential lookup                (authenticate_calendar_admin)
#   - credential active/revoked checks (authenticate_calendar_admin)
#   - Client.active check              (authenticate_calendar_admin)
#   - returning the authenticated tenant identity (the Client row)
#
# app/routes/calendar.py contains ONLY transport wiring (Header binding and
# session injection) plus the per-request client_id comparison; it repeats
# none of the logic above.
#
# CREDENTIAL MODEL (approved):
#   - One or more per-office credentials live in calendar_admin_credentials
#     (app/calendar_models.py CalendarAdminCredential / migration 005).
#   - A raw key is "mia_cal_" + secrets.token_urlsafe(32) and is shown to the
#     operator exactly once at provisioning time. It is NEVER stored: only
#     its SHA-256 lowercase-hex digest is persisted (key_hash).
#   - The global ADMIN_API_KEY has NO access to Calendar routes. This module
#     deliberately never imports it, and there is no fallback of any kind:
#     an infrastructure failure during lookup propagates as a server error
#     (fail closed — Rule 16), never as a 401 and never as an open door.
#
# WHY SHA-256 WITHOUT SALT/KDF: the raw key is a machine-generated token with
# 256 bits of entropy, not a human password. Offline brute force of the
# digest is infeasible, and the unique index on key_hash gives an O(1)
# equality lookup. Timing side channels are moot for the same reason: an
# attacker cannot meaningfully iterate the keyspace.

import hashlib
import re
import secrets
from typing import Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.calendar_models import CalendarAdminCredential
from app.models import Client

# The prefix makes a raw Calendar admin key recognizable to operators (and to
# secret scanners) and unmistakably NOT the global ADMIN_API_KEY.
RAW_KEY_PREFIX = "mia_cal_"

# secrets.token_urlsafe(32) always yields 43 base64url characters
# (32 bytes -> 44 base64 chars with padding, minus the stripped '=').
RAW_KEY_TOKEN_LENGTH = 43

# The COMPLETE shape of a valid raw key. Anything else is rejected before a
# database round-trip (cheap, and reveals nothing: the format is public).
# If a future rotation changes the token length, this ONE constant (and its
# tests) is the only thing to update — single owner, Rule 3.
RAW_KEY_PATTERN = re.compile(
    r"^" + RAW_KEY_PREFIX + r"[A-Za-z0-9_-]{" + str(RAW_KEY_TOKEN_LENGTH) + r"}$"
)

# Every credential failure — missing, empty, malformed, unknown, revoked,
# inactive client — returns EXACTLY this status and detail, so none of them
# can be told apart (no enumeration oracle). The wording is byte-identical
# to what Calendar routes returned before Patch 5.
INVALID_KEY_DETAIL = "Invalid admin key."


def _invalid_key() -> HTTPException:
    """One constructor for the single indistinguishable credential failure."""
    return HTTPException(status_code=401, detail=INVALID_KEY_DETAIL)


def hash_calendar_admin_key(raw_key: str) -> str:
    """
    Purpose: Turn a raw Calendar admin key into its stored form.
    Inputs:  raw_key — the full raw key including the mia_cal_ prefix.
    Returns: the SHA-256 digest as 64 lowercase hexadecimal characters —
             the ONLY form ever persisted (calendar_admin_credentials.key_hash).
    Database effects: none.
    Possible failures: none for str input.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_calendar_admin_key() -> Tuple[str, str]:
    """
    Purpose: Create one new per-office Calendar admin credential pair for the
             operator-only provisioning procedure (docs/INTEGRATION.md §8).
    Inputs:  none — deliberately takes NO database session.
    Returns: (raw_key, key_hash). The caller must show raw_key ONCE for
             secure placement in the intended tool and must persist ONLY
             key_hash. This function itself persists NOTHING and must never
             be given the ability to (no session parameter, on purpose).
    Database effects: none.
    External effects: none (secrets.token_urlsafe uses the OS CSPRNG).
    Possible failures: none.
    """
    raw_key = RAW_KEY_PREFIX + secrets.token_urlsafe(32)
    return raw_key, hash_calendar_admin_key(raw_key)


def authenticate_calendar_admin(db: Session, raw_key: Optional[str]) -> Client:
    """
    Purpose: Decide whether a presented X-Admin-Key value identifies exactly
             one active per-office Calendar admin credential, and return the
             office it belongs to. This is the ONLY authorization decision
             point for every Calendar-admin route.
    Inputs:
        db:      the request's database session (injected by the route's
                 transport wrapper).
        raw_key: the X-Admin-Key header value; None when the header is absent
                 (the header is optional at the FastAPI validation layer so a
                 missing header reaches THIS function instead of a 422).
    Returns: the authenticated Client row — the tenant identity. Routes must
             compare the request's client_id against .id and then use ONLY
             this row.
    Database effects: one SELECT on calendar_admin_credentials (by the unique
        key_hash index) joined to clients. No writes.
    Possible failures:
        - HTTPException 401 "Invalid admin key." for EVERY credential
          failure: missing, empty, malformed, unknown, revoked
          (active=false OR revoked_at set — both are checked in the
          application even though the database CHECK forbids the
          inconsistent combination, failing closed against schema drift or
          manually corrupted rows), and inactive client.
        - Any database error: the session is rolled back and the ORIGINAL
          exception propagates as a server failure. An infrastructure
          failure is NEVER converted into a 401 and NEVER falls back to the
          global ADMIN_API_KEY (which this module does not know exists).
    """
    candidate = (raw_key or "").strip()

    # Format gate: missing, empty, and malformed keys are rejected here,
    # before any database work. The old global admin key also dies here —
    # it does not have the mia_cal_ prefix.
    if RAW_KEY_PATTERN.fullmatch(candidate) is None:
        raise _invalid_key()

    key_hash = hash_calendar_admin_key(candidate)

    try:
        row = (
            db.query(CalendarAdminCredential, Client)
            .join(Client, CalendarAdminCredential.client_id == Client.id)
            .filter(CalendarAdminCredential.key_hash == key_hash)
            .first()
        )
    except Exception:
        # Fail closed and fail VISIBLY (Rule 16): roll the session back and
        # let the real infrastructure error propagate. Returning 401 here
        # would falsely blame the caller's credential; continuing would
        # authenticate nobody-knows-whom.
        db.rollback()
        raise

    if row is None:
        raise _invalid_key()

    credential, client = row

    # Explicit fail-closed checks. The database CHECK constraint already
    # forbids active=true with revoked_at set, but the application refuses
    # BOTH conditions independently so a corrupted or drifted row can never
    # authenticate (approved final condition 4).
    if credential.active is not True:
        raise _invalid_key()
    if credential.revoked_at is not None:
        raise _invalid_key()

    # An office that has been deactivated loses ALL Calendar admin access,
    # regardless of its credentials' state.
    if client.active is not True:
        raise _invalid_key()

    return client
