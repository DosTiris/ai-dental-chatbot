# app/services/portal_notification_settings_service.py
#
# OWNER OF: Office Portal NOTIFICATION DESTINATION management rules (P6-A).
#
# This module is the SINGLE owner (Rule 3) of every rule that decides whether
# an authenticated office may VIEW or REPLACE its own two notification
# destinations and HOW that write is performed safely:
#
#   - office_admin role enforcement                    (require_office_admin)
#   - destination normalization + validation           (normalize_email/phone)
#   - the empty-destination safety invariant           (set_notification_settings)
#   - the atomic compare-and-set write + token minting  (set_notification_settings,
#                                                        _advance_token)
#   - the authoritative post-write re-read              (set_notification_settings)
#
# app/routes/portal_notification_settings.py contains ONLY transport wiring
# and response shaping - it repeats none of the rules here (the portal.py /
# portal_auth.py split, and the portal_leads.py / portal_leads_service.py
# split before it).
#
# SCOPE (owner decision D1, contract v1.1): DESTINATION management only. The
# source of truth stays the existing first-class columns
# clients.notification_email and clients.notification_phone (D2). This module
# introduces NO channel enable/disable flag, NO notification-type preference,
# NO patient notification, and NO provider configuration, and it NEVER changes
# how or whether the two existing send owners (notification_service.py,
# chat.py) send - it only changes WHERE the destinations are stored.
#
# TENANT ISOLATION (Rule 15 - non-negotiable):
#   * Every rule here consumes the verified Client row that
#     app/services/portal_auth.py resolved from the token (PortalIdentity).
#   * Nothing here reads a tenant identifier from request input.
#   * The compare-and-set WHERE clause is scoped to that verified client id,
#     so no request can ever touch another office's row.
#
# CONCURRENCY (owner decision D3/D8, contract v1.1): the write is a single
# NULL-safe compare-and-set on clients.notification_settings_updated_at
# (migration 009), reusing the P3-B2 optimistic-concurrency semantics. There
# is deliberately NO select-compare-then-update sequence. The server-owned
# token STRICTLY advances on every accepted write (see _advance_token),
# mirroring portal_leads_service._advance_token - a deliberate local
# equivalent, not a shared-helper refactor (contract lock C6).

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import update as sql_update  # P6-A: the CAS conditional UPDATE
from sqlalchemy.orm import Session

from app.models import Client
from app.portal_models import OfficeUserRole
from app.services.portal_auth import PortalIdentity

# --- Named limits (Rule 4: no magic values). These MATCH the existing
# output-boundary field limits the notification senders already use
# (notification_service.FIELD_LIMIT_EMAIL / FIELD_LIMIT_PHONE), so a value the
# portal accepts is a value the senders can carry unchanged. ------------------
EMAIL_MAX_LENGTH = 254   # RFC address maximum; == FIELD_LIMIT_EMAIL
PHONE_MAX_LENGTH = 32    # == FIELD_LIMIT_PHONE

# Phone acceptance is deliberately PERMISSIVE (contract D7): E.164 is NOT
# required, because legitimate stored destinations already use both the
# "+15550001111" and the dashed "516-555-7777" forms. The allowed set is the
# characters those forms need; a non-empty phone must also carry at least one
# digit. Internal content is never reformatted - only surrounding whitespace
# is trimmed.
_PHONE_ALLOWED = set("0123456789 +-().")

# --- Controlled failure details (Rule 16: honest, no secret leakage) ---------
# Semantic validation uses 422 (owner decisions D6/D11; contract lock C6) -
# deliberately different from the leads writer's 400, per owner authority.
FORBIDDEN_DETAIL = "This action requires an office administrator."
INVALID_EMAIL_DETAIL = (
    "notification_email must be a valid email address (max 254 characters), "
    "or null to clear it."
)
INVALID_PHONE_DETAIL = (
    "notification_phone must be a valid phone number (max 32 characters), "
    "or null to clear it."
)
BOTH_EMPTY_DETAIL = (
    "At least one notification destination (email or phone) must remain "
    "configured."
)
STALE_TOKEN_DETAIL = (
    "Notification settings were updated elsewhere. Refresh to load the latest "
    "state."
)
# The authenticated client row is guaranteed to exist for an authenticated
# identity; if it has vanished concurrently we fail closed with a generic
# server-class error - NEVER a tenant-resource 404 (there is no user-supplied
# resource id here) and never any database detail (contract C3).
SETTINGS_UNAVAILABLE_DETAIL = "Notification settings are temporarily unavailable."


@dataclass(frozen=True)
class NotificationSettings:
    """The exact three-field slice both endpoints return - destinations plus
    the server-owned concurrency token, and nothing else. No client_id, no
    practice name, no provider identifier, no secret."""
    notification_email: Optional[str]
    notification_phone: Optional[str]
    notification_settings_updated_at: Optional[datetime]


def _now_utc() -> datetime:
    """One clock source for token minting in this module."""
    return datetime.now(timezone.utc)


def _forbidden() -> HTTPException:
    return HTTPException(status_code=403, detail=FORBIDDEN_DETAIL)


def _unprocessable(detail: str) -> HTTPException:
    """One constructor for every semantic-validation refusal (422)."""
    return HTTPException(status_code=422, detail=detail)


def _stale_conflict() -> HTTPException:
    return HTTPException(status_code=409, detail=STALE_TOKEN_DETAIL)


def require_office_admin(identity: PortalIdentity) -> None:
    """
    Purpose: Pin notification-destination management to the office_admin role
        (owner decision D9/C5). In V1 this is INERT - office_admin is the only
        role portal_auth accepts (portal_auth.py rejects anything outside
        OfficeUserRole.ALL) - but asserting it explicitly on BOTH endpoints
        prevents a future role addition from silently gaining destination-edit
        rights. This reads the existing constant only; it never modifies the
        auth owner or the role vocabulary.
    Inputs:  the authenticated PortalIdentity.
    Failures: HTTPException 403 when the role is not office_admin.
    Database effects: none.
    """
    if identity.office_user.role != OfficeUserRole.OFFICE_ADMIN:
        raise _forbidden()


def _advance_token(expected_token: Optional[datetime]) -> datetime:
    """
    Purpose: Mint the server concurrency token for one ACCEPTED write so it is
        STRICTLY newer than the token it replaces - even when the wall clock
        reads the same instant again or has moved backward (coarse resolution,
        VM steps, NTP corrections). Identical semantics to
        portal_leads_service._advance_token (a deliberate LOCAL equivalent, not
        a shared-helper refactor - contract lock C6).
    Inputs:  the expected token the caller supplied. Under compare-and-set an
        ACCEPTED write means the persisted token IS NOT DISTINCT FROM this
        value, so advancing past the expected token advances past the
        persisted one.
    Returns: an aware UTC datetime, strictly greater than expected_token when
        one exists; the plain current clock for the first-ever write
        (expected None).
    Database effects: none (pure).
    """
    now = _now_utc()
    if expected_token is None:
        return now
    expected = (expected_token if expected_token.tzinfo is not None
                else expected_token.replace(tzinfo=timezone.utc))
    if now > expected:
        return now
    # Clock equal to or behind the token being replaced: force strict
    # advancement by the smallest step both PostgreSQL timestamptz and Python
    # datetimes represent exactly (1 microsecond).
    return expected + timedelta(microseconds=1)


def normalize_email(raw: Optional[str]) -> Optional[str]:
    """
    Purpose: Trim and validate one notification email destination.
    Inputs:  the raw request value (may be None).
    Returns: None when blank/None (the existing nullable representation -
        clearing that channel), otherwise the trimmed address.
    Validation (deliberately PERMISSIVE, contract D7 - no TLD allowlist, no
        MX/DNS lookup, no provider call): after trimming, a non-empty value
        must contain no internal whitespace, no control characters, exactly one
        "@" with a non-empty local part and a non-empty domain part, and be at
        most EMAIL_MAX_LENGTH characters. The value is never reformatted.
    Failures: HTTPException 422 INVALID_EMAIL_DETAIL. Database effects: none.
    """
    if raw is None:
        return None
    value = raw.strip()
    if value == "":
        return None
    if len(value) > EMAIL_MAX_LENGTH:
        raise _unprocessable(INVALID_EMAIL_DETAIL)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise _unprocessable(INVALID_EMAIL_DETAIL)
    if any(ch.isspace() for ch in value):
        raise _unprocessable(INVALID_EMAIL_DETAIL)
    if value.count("@") != 1:
        raise _unprocessable(INVALID_EMAIL_DETAIL)
    local, _, domain = value.partition("@")
    if local == "" or domain == "":
        raise _unprocessable(INVALID_EMAIL_DETAIL)
    return value


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """
    Purpose: Trim and validate one notification phone destination.
    Inputs:  the raw request value (may be None).
    Returns: None when blank/None (clearing that channel), otherwise the
        trimmed number, NEVER reformatted (contract D7).
    Validation (PERMISSIVE - E.164 is NOT required): after trimming, a
        non-empty value must be at most PHONE_MAX_LENGTH characters, contain
        only characters in _PHONE_ALLOWED (digits, space, + - ( ) .), and
        carry at least one digit. Both "+15550001111" and "516-555-7777" pass.
    Failures: HTTPException 422 INVALID_PHONE_DETAIL. Database effects: none.
    """
    if raw is None:
        return None
    value = raw.strip()
    if value == "":
        return None
    if len(value) > PHONE_MAX_LENGTH:
        raise _unprocessable(INVALID_PHONE_DETAIL)
    if any(ch not in _PHONE_ALLOWED for ch in value):
        raise _unprocessable(INVALID_PHONE_DETAIL)
    if not any(ch.isdigit() for ch in value):
        raise _unprocessable(INVALID_PHONE_DETAIL)
    return value


def _view(client: Client) -> NotificationSettings:
    """Map a Client row to the exact three-field portal slice (explicit
    field-by-field: a new model column can never leak into a portal
    response)."""
    return NotificationSettings(
        notification_email=client.notification_email,
        notification_phone=client.notification_phone,
        notification_settings_updated_at=client.notification_settings_updated_at,
    )


def get_notification_settings(identity: PortalIdentity) -> NotificationSettings:
    """
    Purpose: Read the authenticated office's own notification destinations.
    Inputs:  the authenticated PortalIdentity (its client row was just loaded
        by portal_auth for this request).
    Returns: the three-field NotificationSettings slice.
    Authorization: office_admin required (D9/C5).
    Database effects: none beyond the auth binding SELECT already performed.
    """
    require_office_admin(identity)
    return _view(identity.client)


def set_notification_settings(
    db: Session,
    identity: PortalIdentity,
    raw_email: Optional[str],
    raw_phone: Optional[str],
    expected_token: Optional[datetime],
) -> NotificationSettings:
    """
    Purpose: Replace BOTH notification destinations under optimistic
        concurrency, honoring the empty-destination safety invariant.
    Inputs:  the request session; the authenticated PortalIdentity; the raw
        email and phone (each None-or-string; blank clears that channel); the
        notification_settings_updated_at token the browser last observed.
    Returns: the re-read NotificationSettings after a committed write.
    Authorization: office_admin required (D9/C5).
    Validation: email/phone normalized+validated (422 on malformed); the
        normalized result must leave AT LEAST ONE destination configured -
        both empty is refused 422 with NO database write (owner decision D6).
    Database effects: exactly ONE atomic conditional UPDATE on the
        authenticated clients row (notification_email, notification_phone,
        notification_settings_updated_at), committed only when the persisted
        token IS NOT DISTINCT FROM expected_token; then an authoritative
        db.refresh of the client row. No select-then-write.
    Possible failures: 403 (non-admin); 422 (malformed field or both-empty);
        409 STALE_TOKEN_DETAIL when the row exists but the token no longer
        matches; 500 SETTINGS_UNAVAILABLE_DETAIL if the authenticated client
        row has vanished concurrently (fail closed - never a 404, never any DB
        detail).
    """
    require_office_admin(identity)

    email = normalize_email(raw_email)
    phone = normalize_phone(raw_phone)

    # Empty-destination safety invariant (D6): allowing BOTH to be cleared
    # would functionally disable every office alert (the senders treat a blank
    # recipient as that channel off, and emergency/priority leads use these
    # same destinations). Refuse before any write; the row is untouched.
    if email is None and phone is None:
        raise _unprocessable(BOTH_EMPTY_DETAIL)

    client_id = identity.client.id  # verified tenant - never a request value

    result = db.execute(
        sql_update(Client)
        .where(
            Client.id == client_id,
            # The compare half of compare-and-set, NULL-safe: the write lands
            # only if the persisted token IS NOT DISTINCT FROM the expected one.
            Client.notification_settings_updated_at.is_not_distinct_from(
                expected_token
            ),
        )
        .values(
            notification_email=email,
            notification_phone=phone,
            notification_settings_updated_at=_advance_token(expected_token),
        )
    )

    if result.rowcount == 1:
        db.commit()
        # Authoritative re-read (contract C3): the Core UPDATE did not mutate
        # the ORM instance, so identity.client is stale after commit. Refresh
        # it from the database and build the response from the persisted row -
        # never from the request values.
        db.refresh(identity.client)
        return _view(identity.client)

    # Zero rows: nothing was written. Roll back the no-op transaction and
    # disambiguate. The authenticated client row is guaranteed to exist for an
    # authenticated identity, so a miss can only be a stale token (409). If the
    # row has genuinely vanished (concurrent deletion), fail closed generically
    # - never a tenant-resource 404, never any database detail (contract C3).
    db.rollback()
    still_present = (
        db.query(Client.id).filter(Client.id == client_id).first()
    )
    if still_present is None:
        raise HTTPException(status_code=500, detail=SETTINGS_UNAVAILABLE_DETAIL)
    raise _stale_conflict()
