# portal_config_service.py - PUBLIC browser bootstrap configuration for the
# office portal (MIA_P3A_PORTAL_AUTH_UI v1.0.1, audit finding F-P3A-1).
#
# Single owner (Constitution Rule 3): ALL derivation and validation of the
# public portal browser configuration lives here. app/routes/portal.py is
# transport wiring only and simply returns build_portal_public_config().
#
# This module must NEVER return or read SUPABASE_JWT_SECRET, JWKS data,
# ADMIN_API_KEY, service-role credentials, database credentials, or Calendar
# credentials. It reads exactly two environment variables:
#
#   SUPABASE_AUTH_ISSUER      - existing, required by P2 JWT verification;
#                               expected form https://<host>/auth/v1
#   SUPABASE_PUBLISHABLE_KEY  - NEW; the PUBLIC browser key only, and it
#                               MUST be a modern sb_publishable_ key
#
# Failure policy (Constitution Rule 4 / Rule 16): missing or malformed
# configuration fails closed with HTTP 503 and a server-side log line naming
# what failed. No guessed defaults, no hardcoded values, no silent fallback.

import logging
import os
import re

from fastapi import HTTPException

# Environment variable NAMES (named constants; Rule 4: no magic values).
ISSUER_ENV_VAR = "SUPABASE_AUTH_ISSUER"
PUBLISHABLE_KEY_ENV_VAR = "SUPABASE_PUBLISHABLE_KEY"

# The issuer is accepted ONLY in the exact Supabase Auth form
# https://<host>/auth/v1 (one optional trailing slash). Anything else is
# rejected rather than repaired - deriving a browser origin from an
# unexpected issuer shape could point the login form at the wrong host.
ISSUER_PATTERN = re.compile(r"^https://[^/\s]+/auth/v1/?$")

# The stripped suffix, kept as a named constant so the derivation below has
# no inline magic string.
ISSUER_AUTH_SUFFIX = "/auth/v1"

# F-P3A-3: the ONLY accepted form is a modern Supabase publishable key.
# Substring denylists are insufficient because legacy service_role API keys
# are JWTs whose role claim is base64url-ENCODED - the raw token need not
# contain the plaintext "service_role". So this validator is an ALLOWLIST:
# anything that does not carry this exact prefix (sb_secret_ keys, legacy
# anon JWTs, legacy service_role JWTs, arbitrary strings) is refused, and
# no JWT decoding or role inspection is ever attempted here.
REQUIRED_PUBLISHABLE_KEY_PREFIX = "sb_publishable_"

logger = logging.getLogger("portal_config")


def derive_supabase_url(issuer_value):
    """
    Purpose:
        Derive the browser-facing Supabase project URL from the configured
        Auth issuer by removing the /auth/v1 suffix.

    Inputs:
        issuer_value: raw string from SUPABASE_AUTH_ISSUER (may be None).

    Returns:
        The https project URL without a trailing slash, e.g.
        https://abc.supabase.co - or None when the issuer is missing or not
        in the expected https://<host>/auth/v1 form.

    Possible failures:
        Returns None instead of raising; the caller converts None to 503.

    Database effects: none.  External effects: none.
    """
    if not isinstance(issuer_value, str):
        return None
    issuer = issuer_value.strip()
    if not ISSUER_PATTERN.match(issuer):
        return None
    # Remove one optional trailing slash, then the /auth/v1 suffix. The
    # regex above guarantees the suffix is present exactly at the end.
    if issuer.endswith("/"):
        issuer = issuer[:-1]
    derived = issuer[: -len(ISSUER_AUTH_SUFFIX)]
    # The regex guarantees a non-empty https://<host> remainder; assert the
    # invariant loudly rather than trusting it silently (Rule 4).
    if not derived.startswith("https://") or derived == "https://":
        return None
    return derived


def validate_publishable_key(key_value):
    """
    Purpose:
        Validate the PUBLIC browser key from SUPABASE_PUBLISHABLE_KEY.

    Inputs:
        key_value: raw string from the environment (may be None).

    Returns:
        The stripped key ONLY when it is a modern Supabase publishable key
        (exact prefix sb_publishable_). None for everything else: missing
        or empty values, sb_secret_ keys, JWT-shaped values (legacy anon
        AND legacy service_role tokens), and arbitrary strings.

    Possible failures:
        Returns None instead of raising; the caller converts None to 503.
        The allowlist is deliberate (F-P3A-3): a legacy service_role JWT
        encodes its role claim in base64url, so no substring check can
        recognize it - only the modern publishable form is safe to hand to
        every anonymous browser. The supplied value is never logged.

    Database effects: none.  External effects: none.
    """
    if not isinstance(key_value, str):
        return None
    key = key_value.strip()
    if not key.startswith(REQUIRED_PUBLISHABLE_KEY_PREFIX):
        return None
    if len(key) == len(REQUIRED_PUBLISHABLE_KEY_PREFIX):
        # The bare prefix with no key material is not a key.
        return None
    return key


def build_portal_public_config():
    """
    Purpose:
        Build the exact two-field PUBLIC configuration response for
        GET /portal/config.

    Inputs:
        None directly; reads SUPABASE_AUTH_ISSUER and
        SUPABASE_PUBLISHABLE_KEY from the environment at request time so a
        corrected environment takes effect without code changes.

    Returns:
        {"supabase_url": <https project url>,
         "supabase_publishable_key": <public key>}
        - exactly these two keys and nothing else. No tenant identity, no
        client_id, no secret of any kind.

    Possible failures:
        HTTPException(503) when either variable is missing or malformed.
        The client-facing detail is generic; the specific cause is logged
        server-side only (Rule 16: failure visible, no secret leakage).

    Database effects: none.  External effects: none.
    """
    supabase_url = derive_supabase_url(os.getenv(ISSUER_ENV_VAR))
    if supabase_url is None:
        logger.warning(
            "portal config unavailable: %s missing or not in the expected "
            "https://<host>/auth/v1 form",
            ISSUER_ENV_VAR,
        )
        raise HTTPException(
            status_code=503, detail="portal configuration unavailable"
        )

    publishable_key = validate_publishable_key(
        os.getenv(PUBLISHABLE_KEY_ENV_VAR)
    )
    if publishable_key is None:
        # Rule 16 + F-P3A-3: name WHICH variable failed and the accepted
        # form, but never log the supplied value itself.
        logger.warning(
            "portal config unavailable: %s missing or not a modern "
            "sb_publishable_ key (only that form is served to browsers)",
            PUBLISHABLE_KEY_ENV_VAR,
        )
        raise HTTPException(
            status_code=503, detail="portal configuration unavailable"
        )

    return {
        "supabase_url": supabase_url,
        "supabase_publishable_key": publishable_key,
    }
