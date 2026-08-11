# app/services/portal_leads_service.py
#
# OWNER OF: Office Portal READ-ONLY lead and dashboard query rules (P3-B1).
#
# This module is the SINGLE owner (Rule 3) of every rule that decides WHAT
# lead data an authenticated office may read and HOW it is selected:
#
#   - filter validation (closed status vocabulary, search length,
#     day-window bounds, pagination bounds)                (validate_list_filters)
#   - LIKE wildcard escaping so search terms are literal   (escape_like_pattern)
#   - tenant-scoped lead selection and ordering            (_lead_query/_apply_*)
#   - the paginated lead list                              (list_leads)
#   - lead detail + transcript resolution                  (get_lead_detail)
#   - dashboard counts and recent-lead selection           (get_dashboard_counts,
#                                                           get_recent_leads)
#   - the new/returning patient-type derivation            (derive_patient_type)
#
# app/routes/portal_leads.py contains ONLY transport wiring and response
# shaping - it repeats none of the rules above (the portal.py / portal_auth.py
# split, and the calendar.py / calendar_admin_auth.py split before it).
#
# DATA MODEL (P3-B1 recon result - no new tables, no migration):
#   A "lead" IS a conversations row with is_lead = TRUE plus the lead_*
#   columns (app/models.py). This module never invents a second lead store
#   and never duplicates intake data.
#
# TENANT ISOLATION (Rule 15 - non-negotiable):
#   * Every query here REQUIRES the caller to pass the Client row that
#     app/services/portal_auth.py resolved from the verified token. The
#     tenant filter (client_id == that row's id) is applied in ONE place,
#     _lead_query, before any other filter, ordering, or pagination.
#   * Nothing in this module reads a tenant identifier from request input.
#   * A lead id belonging to another office resolves EXACTLY like a lead id
#     that does not exist: the identical 404 LEAD_NOT_FOUND_DETAIL. So does
#     a conversation of the right office that never became a lead - the
#     detail surface exposes leads only.
#
# READ-ONLY CONTRACT (P3-B1 scope): this module performs no INSERT, UPDATE,
# or DELETE of any kind. It issues only SELECTs.

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import case, func
from sqlalchemy.orm import Query, Session

from app.models import Client, Conversation, Message

# --- Named limits and vocabularies (Rule 4/16: no magic values, closed sets)

# Pagination: an office cannot request an unbounded history in one call.
LIST_LIMIT_DEFAULT = 25
LIST_LIMIT_MIN = 1
LIST_LIMIT_MAX = 100
OFFSET_MIN = 0

# Optional recency window on last_lead_at, in whole days.
DAYS_MIN = 1
DAYS_MAX = 365

# Search terms longer than this are refused (never truncated silently).
SEARCH_QUERY_MAX_CHARS = 100

# Dashboard shape: how many recent leads appear, and the activity window
# behind the "leads in the last N days" count.
DASHBOARD_RECENT_LEADS = 5
DASHBOARD_ACTIVITY_DAYS = 7

# The maximum transcript messages one lead-detail response may carry
# (audit finding A2: no portal request may return an unbounded
# patient-record history). Truncation is EXPLICIT: the response always
# states the true total and whether it was cut (never a silent partial).
TRANSCRIPT_MESSAGE_LIMIT = 200

# The COMPLETE set of lead_status values the system ever writes:
#   "new"        - server default at lead creation (models.py / chat.py):
#                  the lead EXISTS but intake is not yet finished. NOT a
#                  staff-handling signal of any kind.
#   "completed"  - written AUTOMATICALLY by the intake flow in
#                  app/routes/chat.py the moment intake completes. It means
#                  the patient's REQUEST was fully captured - it says
#                  nothing about whether the office has followed up.
#   "contacted", "booked", "closed" - the operator-admin manual statuses
#     (app/routes/admin.py update_lead_status closed set)
# Because the lifecycle mixes system-written and staff-written values,
# NOTHING in this module may present lead_status as a measure of staff
# handling (audit finding A1). The portal status FILTER accepts exactly
# these values; anything else is refused with an explicit 400 (Rule 16:
# unknown vocabulary raises).
LEAD_STATUS_VALUES = frozenset(
    {"new", "contacted", "booked", "completed", "closed"}
)

# The ONE not-found detail for the lead-detail surface. Foreign-tenant,
# nonexistent, and non-lead conversation ids all return exactly this
# (Rule 15: tenant mismatch indistinguishable from not-found).
LEAD_NOT_FOUND_DETAIL = "Lead not found."

# The escape character declared to ILIKE so %, _ and the escape character
# itself match literally inside search terms.
LIKE_ESCAPE_CHAR = "\\"


def _now_utc() -> datetime:
    """One clock source for every window computation in this module."""
    return datetime.now(timezone.utc)


def escape_like_pattern(term: str) -> str:
    """
    Purpose: Make a user-supplied search term LITERAL inside a LIKE/ILIKE
        pattern - "%" must match a percent sign, never "everything".
    Inputs:  the raw (already length-validated) search term.
    Returns: the term with the escape character, "%" and "_" escaped, ready
        to be wrapped in "%...%" and passed with escape=LIKE_ESCAPE_CHAR.
    Failures: none (pure string transformation).
    Database effects: none.
    """
    return (
        term.replace(LIKE_ESCAPE_CHAR, LIKE_ESCAPE_CHAR + LIKE_ESCAPE_CHAR)
        .replace("%", LIKE_ESCAPE_CHAR + "%")
        .replace("_", LIKE_ESCAPE_CHAR + "_")
    )


def _bad_request(detail: str) -> HTTPException:
    """One constructor for every explicit filter-validation refusal."""
    return HTTPException(status_code=400, detail=detail)


def validate_list_filters(
    status: Optional[str],
    q: Optional[str],
    days: Optional[int],
    limit: int,
    offset: int,
) -> Tuple[Optional[str], Optional[str], Optional[int], int, int]:
    """
    Purpose: Validate and normalize every lead-list filter in one place.
    Inputs:  raw query-parameter values (status/q may be None or blank;
             days may be None meaning "no window").
    Returns: (status, q, days, limit, offset) normalized - status lowercased
             from the closed vocabulary or None; q stripped or None.
    Failures: HTTPException 400 with an explicit reason for: a status
        outside LEAD_STATUS_VALUES, a search term over
        SEARCH_QUERY_MAX_CHARS, days outside [DAYS_MIN, DAYS_MAX], limit
        outside [LIST_LIMIT_MIN, LIST_LIMIT_MAX], or a negative offset.
        Nothing is silently clamped or truncated (Rule 4: no hidden repair).
    Database effects: none.
    """
    normalized_status = (status or "").strip().lower() or None
    if normalized_status is not None and normalized_status not in LEAD_STATUS_VALUES:
        raise _bad_request(
            f"status must be one of {sorted(LEAD_STATUS_VALUES)}"
        )

    normalized_q = (q or "").strip() or None
    if normalized_q is not None and len(normalized_q) > SEARCH_QUERY_MAX_CHARS:
        raise _bad_request(
            f"q must be at most {SEARCH_QUERY_MAX_CHARS} characters"
        )

    if days is not None and (days < DAYS_MIN or days > DAYS_MAX):
        raise _bad_request(
            f"days must be between {DAYS_MIN} and {DAYS_MAX}"
        )

    if limit < LIST_LIMIT_MIN or limit > LIST_LIMIT_MAX:
        raise _bad_request(
            f"limit must be between {LIST_LIMIT_MIN} and {LIST_LIMIT_MAX}"
        )

    if offset < OFFSET_MIN:
        raise _bad_request(f"offset must be >= {OFFSET_MIN}")

    return normalized_status, normalized_q, days, limit, offset


def _lead_query(db: Session, client_id: uuid.UUID) -> Query:
    """
    Purpose: The ONE starting point for every portal lead SELECT.
    Tenant isolation (Rule 15) is applied here FIRST - client_id comes from
    the verified PortalIdentity's Client row, never from request input - and
    only conversations that actually became leads are visible.
    Database effects: none (query construction only).
    """
    return (
        db.query(Conversation)
        .filter(Conversation.client_id == client_id)
        .filter(Conversation.is_lead.is_(True))
    )


def _apply_list_filters(
    query: Query,
    status: Optional[str],
    q: Optional[str],
    days: Optional[int],
    now: datetime,
) -> Query:
    """
    Purpose: Apply the validated, normalized list filters to a tenant-scoped
        lead query. Filters only NARROW the tenant scope - they can never
        widen it, because they are ANDed onto _lead_query's result.
    Inputs:  validate_list_filters output plus the window clock reading.
    Returns: the filtered query.
    Business rules:
      - status matches the stored value exactly (the stored vocabulary is
        the same closed lowercase set the validator enforced);
      - q is a case-insensitive LITERAL substring match against the
        captured name, phone, or email (wildcards escaped);
      - days keeps leads whose last_lead_at falls inside the window; a NULL
        last_lead_at cannot satisfy a window (same semantics as the
        operator-admin /admin/leads window).
    Database effects: none (query construction only).
    """
    if status is not None:
        query = query.filter(Conversation.lead_status == status)
    if q is not None:
        pattern = f"%{escape_like_pattern(q)}%"
        query = query.filter(
            Conversation.lead_name.ilike(pattern, escape=LIKE_ESCAPE_CHAR)
            | Conversation.lead_phone.ilike(pattern, escape=LIKE_ESCAPE_CHAR)
            | Conversation.lead_email.ilike(pattern, escape=LIKE_ESCAPE_CHAR)
        )
    if days is not None:
        query = query.filter(
            Conversation.last_lead_at >= now - timedelta(days=days)
        )
    return query


def _apply_lead_order(query: Query) -> Query:
    """
    Purpose: The ONE lead ordering rule for the portal LIST: urgency flags
        first (emergency, then priority, then after-hours), then most
        recent lead activity, then newest conversation.
    This mirrors the operator-admin /admin/leads ordering exactly, PLUS a
    final id tiebreaker: created_at values can tie (same-transaction
    timestamps), and OFFSET/LIMIT pagination over a nondeterministic order
    could repeat or drop rows across pages. The id column makes page
    boundaries stable.
    Database effects: none (query construction only).
    """
    return query.order_by(
        Conversation.lead_is_emergency.desc(),
        Conversation.lead_is_priority.desc(),
        Conversation.lead_is_outside_hours.desc(),
        Conversation.last_lead_at.desc().nullslast(),
        Conversation.created_at.desc(),
        Conversation.id.desc(),
    )


def _apply_recency_order(query: Query) -> Query:
    """
    Purpose: PURE recency ordering for the dashboard "Recent leads" strip
        (audit finding A1): a strip labeled "recent" must show the newest
        lead activity, so an old emergency/priority row can never displace
        a newer lead here. The main Leads LIST keeps the urgency-first
        ordering above; the two orderings serve two different promises.
    Ordering: last_lead_at desc (nulls last), created_at desc, id desc
        (deterministic tiebreaker).
    Database effects: none (query construction only).
    """
    return query.order_by(
        Conversation.last_lead_at.desc().nullslast(),
        Conversation.created_at.desc(),
        Conversation.id.desc(),
    )


def list_leads(
    db: Session,
    client: Client,
    *,
    status: Optional[str] = None,
    q: Optional[str] = None,
    days: Optional[int] = None,
    limit: int = LIST_LIMIT_DEFAULT,
    offset: int = OFFSET_MIN,
) -> Tuple[int, List[Conversation]]:
    """
    Purpose: The authenticated office's paginated lead list.
    Inputs:  the request session; the VERIFIED tenant Client row from
             PortalIdentity; raw filter values (validated here).
    Returns: (total, rows) - total is the filtered lead count so the
             frontend can paginate honestly; rows is the requested page in
             the portal ordering.
    Database effects: exactly two SELECTs (one COUNT, one page) - no
        per-row follow-up queries (no N+1).
    Possible failures: HTTPException 400 from validate_list_filters;
        database errors propagate to the route layer (fail closed).
    """
    status, q, days, limit, offset = validate_list_filters(
        status, q, days, limit, offset
    )
    filtered = _apply_list_filters(
        _lead_query(db, client.id), status, q, days, _now_utc()
    )
    total = filtered.count()
    rows = _apply_lead_order(filtered).offset(offset).limit(limit).all()
    return total, rows


def get_lead_detail(
    db: Session,
    client: Client,
    lead_id: uuid.UUID,
) -> Tuple[Conversation, List[Message], int, bool]:
    """
    Purpose: One lead plus its message transcript, for the detail view.
    Inputs:  the request session; the VERIFIED tenant Client row; the
             requested lead (conversation) id from the URL path.
    Returns: (conversation, messages, messages_total, messages_truncated).
        messages holds at most TRANSCRIPT_MESSAGE_LIMIT rows, oldest first
        (created_at ascending, id as a stable tiebreaker) - the intake
        conversation reads from its beginning. messages_total is the TRUE
        transcript length and messages_truncated says whether the bound
        cut it, so the frontend can state a partial transcript honestly
        (audit finding A2: bounded, and never silently partial).
    Database effects: exactly three SELECTs (the lead row, the transcript
        count, the bounded transcript page) - no N+1, no unbounded read.
    Possible failures: HTTPException 404 LEAD_NOT_FOUND_DETAIL when the id
        is unknown, belongs to ANOTHER office, or is a conversation that
        never became a lead - all three are indistinguishable (Rule 15).
        Database errors propagate (fail closed).
    """
    conversation = (
        _lead_query(db, client.id)
        .filter(Conversation.id == lead_id)
        .first()
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail=LEAD_NOT_FOUND_DETAIL)

    transcript_query = db.query(Message).filter(
        Message.conversation_id == conversation.id
    )
    messages_total = transcript_query.count()
    messages = (
        transcript_query
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(TRANSCRIPT_MESSAGE_LIMIT)
        .all()
    )
    messages_truncated = messages_total > len(messages)
    return conversation, messages, messages_total, messages_truncated


def get_dashboard_counts(db: Session, client: Client) -> dict:
    """
    Purpose: The small set of trustworthy, schema-backed dashboard counts.
    Inputs:  the request session; the VERIFIED tenant Client row.
    Returns: {"total_conversations", "total_leads", "urgent_leads",
              "leads_last_7_days"} - each an int, each derived directly
             from existing columns (no invented analytics):
      - total_conversations: every conversation this office has had;
      - total_leads: conversations with is_lead TRUE;
      - urgent_leads: leads flagged emergency OR priority - the rows the
        office should look at first. This deliberately uses the urgency
        FLAG columns, not lead_status: "new" only means intake is not yet
        finished and "completed" only means Mia finished capturing the
        request, so neither is a staff-handling signal (audit finding A1);
      - leads_last_7_days: leads whose last_lead_at falls within
        DASHBOARD_ACTIVITY_DAYS of now.
    Database effects: exactly one aggregated SELECT.
    Possible failures: database errors propagate (fail closed).
    """
    since = _now_utc() - timedelta(days=DASHBOARD_ACTIVITY_DAYS)
    is_lead_row = Conversation.is_lead.is_(True)
    row = (
        db.query(
            func.count(Conversation.id),
            func.coalesce(func.sum(case((is_lead_row, 1), else_=0)), 0),
            func.coalesce(
                func.sum(
                    case(
                        (
                            is_lead_row
                            & (
                                Conversation.lead_is_emergency.is_(True)
                                | Conversation.lead_is_priority.is_(True)
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            is_lead_row
                            & (Conversation.last_lead_at >= since),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .filter(Conversation.client_id == client.id)
        .one()
    )
    return {
        "total_conversations": int(row[0]),
        "total_leads": int(row[1]),
        "urgent_leads": int(row[2]),
        "leads_last_7_days": int(row[3]),
    }


def get_recent_leads(db: Session, client: Client) -> List[Conversation]:
    """
    Purpose: The dashboard's short "Recent leads" strip.
    Returns: up to DASHBOARD_RECENT_LEADS leads in PURE recency order
        (_apply_recency_order) - the strip's label promises recency, so an
        old flagged row must never displace a newer lead here (audit
        finding A1). Urgency-first ordering remains the Leads LIST rule.
    Database effects: one SELECT.
    Possible failures: database errors propagate (fail closed).
    """
    return (
        _apply_recency_order(_lead_query(db, client.id))
        .limit(DASHBOARD_RECENT_LEADS)
        .all()
    )


def derive_patient_type(conversation: Conversation) -> Optional[str]:
    """
    Purpose: Present the tri-state lead_is_new_patient column as the same
        display vocabulary the operator admin uses.
    Returns: "new" (True), "returning" (False), or None (unknown) - a
        closed derivation, never a guess.
    Database effects: none.
    """
    flag = conversation.lead_is_new_patient
    if flag is True:
        return "new"
    if flag is False:
        return "returning"
    return None
