from pydantic import BaseModel  # Import BaseModel for validation
from typing import Optional  # Import Optional for optional fields
from pydantic import BaseModel # in app/schemas.py
from typing import Optional, Dict, Any # in app/schemas.py

# Prototype B B1 contract imports (see the B1 section below).
from datetime import date, datetime, timedelta  # B1 contract fields
from typing import List, Literal  # B1: locked vocabularies via Literal
from pydantic import Field, field_validator, model_validator  # B1 rules

class ChatRequest(BaseModel):  # Request body schema for /chat
    message: str  # User message text
    client_key: str  # Your per-office API key (from widget)
    visitor_id: Optional[str] = None  # Optional browser visitor ID
    conversation_id: Optional[str] = None  # Optional conversation ID to continue a session

class ChatResponse(BaseModel):  # Response schema for /chat
    reply: str  # Assistant reply
    conversation_id: str  # Conversation ID for follow-up messages
    meta: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Prototype B B1 — read-only availability preview contract (no route).
#
# OWNER OF: the request/response shapes for the future visual Calendar picker.
# B1 deliberately ships the contract WITHOUT any HTTP route, frontend fetch,
# or authentication — those belong to B2. Nothing here reads the database.
#
# Contract locks (Prototype B B1 spec):
#   - no slot_id                    - no patient information
#   - no time_preference            - no credentials
#   - no daily slot counts          - no notification destinations
#   - all UTC timestamps aware      - no private tenant settings
# ---------------------------------------------------------------------------

# The one legal UTC offset for contract datetimes (Rule 4: named, not a
# magic value buried in a validator).
_UTC_ZERO_OFFSET = timedelta(0)


def _require_aware_utc(value: datetime) -> datetime:
    """
    Purpose: single owner (Rule 3) of the B1 aware-UTC field rule used by
             every contract datetime validator below.
    Inputs:  value — a datetime parsed by pydantic.
    Returns: the value unchanged when it is timezone-aware AND actually in
             UTC (offset 00:00).
    Possible failures: ValueError (pydantic surfaces it as a
        ValidationError) for naive datetimes and for aware datetimes in
        any non-UTC offset — the contract promises real UTC instants, so
        a +05:00 value is a construction bug, never silently converted.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("contract datetimes must be timezone-aware UTC")
    if value.utcoffset() != _UTC_ZERO_OFFSET:
        raise ValueError(
            "contract datetimes must be in UTC (offset 00:00), got "
            f"offset {value.utcoffset()}"
        )
    return value


# Maximum INCLUSIVE preview range: start_day .. end_day may span at most this
# many local calendar days (a full month view plus padding). Named here, not
# buried in a validator (Rule 4).
PREVIEW_MAX_RANGE_DAYS = 31


class AvailabilityPreviewRequest(BaseModel):
    """
    Purpose: Validated input for the B1 availability preview service.
    Fields:
        start_day / end_day: INCLUSIVE office-local calendar dates. Pydantic
            rejects values that are not valid ISO dates, so an invalid local
            date never reaches the service.
        selected_day: optional; when present the response also carries that
            day's bookable slots. Must lie inside [start_day, end_day].
        service_key: OPTIONAL (approved B2 request-contract expansion).
            None - the default - means a GENERIC preview: the value is
            passed unchanged to the policy owner, which then applies no
            service filter. When supplied, it is an OPAQUE, non-blank
            value in the EXISTING Calendar policy vocabulary (the values
            carried by slot rows and compared by evaluate_slot_policy,
            e.g. "cleaning/checkup"). The B2 route owns master-key
            validation and translation BEFORE constructing this model;
            blank and whitespace-only input remains rejected here.
    Possible failures: pydantic.ValidationError on any violated rule below —
        the caller (a future B2 route) surfaces it; nothing is guessed.
    """
    start_day: date
    end_day: date
    selected_day: Optional[date] = None
    service_key: Optional[str] = None

    @model_validator(mode="after")
    def _validate_range_rules(self):
        # end before start is a caller bug, never silently reordered (Rule 4).
        if self.end_day < self.start_day:
            raise ValueError("end_day must not be before start_day")
        # INCLUSIVE day count: July 30 .. July 30 is 1 day, not 0.
        inclusive_days = (self.end_day - self.start_day).days + 1
        if inclusive_days > PREVIEW_MAX_RANGE_DAYS:
            raise ValueError(
                f"requested range spans {inclusive_days} days; the maximum "
                f"inclusive preview range is {PREVIEW_MAX_RANGE_DAYS} days"
            )
        if self.selected_day is not None and not (
            self.start_day <= self.selected_day <= self.end_day
        ):
            raise ValueError("selected_day must lie inside start_day..end_day")
        if self.service_key is not None and not self.service_key.strip():
            raise ValueError("service_key is required and must not be blank")
        return self


class PreviewDay(BaseModel):
    """One office-local calendar day in the preview range.

    state is ENFORCED (Literal, not a comment) to the B1 locked day-state
    vocabulary owned by availability_preview_service: open / full /
    unavailable / past. "closed" is IMPOSSIBLE in this contract — the
    backend has no authoritative office-hours model, so it cannot
    distinguish office-closed from unpublished availability. A sync test
    keeps this Literal aligned with the service's ALL_DAY_STATES.
    selectable is ENFORCED equal to (state == "open").
    NOTE (contract lock): no daily slot count field may ever be added here.
    """
    local_date: date
    weekday: str
    state: Literal["open", "full", "unavailable", "past"]
    selectable: bool

    @model_validator(mode="after")
    def _selectable_is_derived_from_state(self):
        # Contract lock: selectable is DERIVED, never independent. A payload
        # where the two disagree is a construction bug and must fail loudly
        # (Rule 4), not be silently corrected.
        if self.selectable != (self.state == "open"):
            raise ValueError(
                "selectable must be True exactly when state is 'open'"
            )
        return self


class PreviewSlot(BaseModel):
    """One bookable slot on the selected day.

    Contract lock: carries NO slot_id — the B1 preview is purely visual, and
    a future selection flow must re-resolve real slots server-side. UTC
    fields are timezone-aware; local_* fields are pre-rendered office-local
    presentation strings so the frontend never does timezone math.
    """
    start_utc: datetime
    end_utc: datetime
    local_start_time: str
    local_end_time: str
    accessible_date_label: str
    accessible_time_label: str
    # ENFORCED grouping vocabulary: local start hour < 12 is "morning",
    # otherwise "afternoon". No third value exists in the B1 contract.
    time_of_day: Literal["morning", "afternoon"]
    # Every emitted preview slot is selectable by definition in B1 —
    # non-bookable rows are filtered out before rendering, so a False here
    # is impossible and ENFORCED as such.
    selectable: Literal[True] = True

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _bounds_are_aware_utc(cls, value: datetime) -> datetime:
        # Single aware-UTC owner; see _require_aware_utc.
        return _require_aware_utc(value)

    @model_validator(mode="after")
    def _end_is_after_start(self):
        # A zero- or negative-length slot is a data bug, never rendered.
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc must be strictly after start_utc")
        return self


class AvailabilityPreviewResponse(BaseModel):
    """
    Purpose: Complete read-only payload for the visual picker.
    Fields:
        booking_enabled: INFORMATIONAL — false never blocks building this
            read-only response (production booking is frozen; the preview
            must still render).
        generated_at: aware-UTC instant the preview was computed.
        days: one entry per local date in start_day..end_day inclusive, in
            calendar order.
        selected_day/slots: slots is non-empty only when the request supplied
            selected_day AND that day has bookable slots; ordering is
            deterministic (soonest first).
    """
    client_id: str
    practice_name: str
    timezone_name: str
    booking_enabled: bool
    range_start: date
    range_end: date
    generated_at: datetime
    days: List[PreviewDay]
    selected_day: Optional[date] = None
    slots: List[PreviewSlot] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def _generated_at_is_aware_utc(cls, value: datetime) -> datetime:
        # Single aware-UTC owner; see _require_aware_utc.
        return _require_aware_utc(value)
