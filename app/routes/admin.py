from fastapi import APIRouter, Depends, HTTPException, Header  # Import router, DI helpers, errors, and Header auth
from sqlalchemy.orm import Session  # Import SQLAlchemy Session for DB queries
from sqlalchemy import func  # Import SQL functions (count, date_trunc, etc.)
from sqlalchemy import text as sql_text  # Import SQL text helper
from starlette.responses import StreamingResponse  # Import StreamingResponse for CSV downloads
from app.database import SessionLocal  # Import DB session factory
from app.models import Client, Conversation, Message, ClientFAQ, FAQEvent  # Import models (includes FAQEvent)
from app.config import ADMIN_API_KEY  # Import admin key from config
from datetime import datetime, timedelta, timezone  # Import datetime tools
import csv  # Import CSV writer
import io  # Import IO buffer for streaming CSV
from pydantic import BaseModel  # Import BaseModel for JSON body validation
from typing import Optional  # Import Optional for optional fields
from fastapi.responses import FileResponse  # serve a file (you imported it; keep in case you use it)


router = APIRouter(prefix="/admin", tags=["admin"])  # Create admin router with /admin prefix


# -----------------------------
# Dependencies
# -----------------------------
def get_db():  # Dependency to provide DB session
    db = SessionLocal()  # Open a DB session
    try:  # Start try block
        yield db  # Yield to route handler
    finally:  # Always run cleanup
        db.close()  # Close session


def require_admin(x_admin_key: str = Header(default="")):  # Admin auth dependency (Swagger shows header input)
    provided = (x_admin_key or "").strip()  # Normalize header value
    if not ADMIN_API_KEY:  # If server has no admin key configured
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not set on server")  # Fail loudly
    if provided != ADMIN_API_KEY:  # If key mismatch
        raise HTTPException(status_code=401, detail="Unauthorized")  # Block access


# -----------------------------
# Basic Admin Endpoints
# -----------------------------
@router.get("/health")  # Health endpoint
def admin_health(_: None = Depends(require_admin)):  # Require admin key
    return {"ok": True}  # Return OK


@router.get("/clients")  # List clients
def list_clients(_: None = Depends(require_admin), db: Session = Depends(get_db)):  # Require admin + DB session
    rows = db.query(Client).order_by(Client.created_at.desc()).all()  # Fetch clients newest first
    return [  # Return list
        {  # Client object
            "id": str(c.id),  # Client UUID
            "practice_name": c.practice_name,  # Practice name
            "api_key": c.api_key,  # Client key (admin only)
            "active": bool(c.active),  # Active flag
            "created_at": c.created_at.isoformat() if c.created_at else None,  # Created timestamp
        }
        for c in rows
    ]


@router.get("/conversations")  # List conversations
def list_conversations(
    client_key: str | None = None,  # Optional filter by client api_key
    limit: int = 50,  # Page size
    offset: int = 0,  # Pagination offset
    _: None = Depends(require_admin),  # Require admin key
    db: Session = Depends(get_db),  # Inject DB
):
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    q = db.query(Conversation)  # Start query

    if client_key:
        client = db.query(Client).filter(Client.api_key == client_key).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        q = q.filter(Conversation.client_id == client.id)

    rows = q.order_by(Conversation.created_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "id": str(conv.id),
            "client_id": str(conv.client_id),
            "visitor_id": conv.visitor_id,
            "is_lead": bool(getattr(conv, "is_lead", False)),
            "lead_email": getattr(conv, "lead_email", None),
            "lead_phone": getattr(conv, "lead_phone", None),
            "last_lead_at": getattr(conv, "last_lead_at", None).isoformat() if getattr(conv, "last_lead_at", None) else None,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
        }
        for conv in rows
    ]


@router.get("/conversations/{conversation_id}/messages")  # Messages-only endpoint
def get_conversation_messages(
    conversation_id: str,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs = db.query(Message).filter(Message.conversation_id == conv.id).order_by(Message.created_at.asc()).all()
    return {
        "conversation_id": str(conv.id),
        "client_id": str(conv.client_id),
        "visitor_id": conv.visitor_id,
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in msgs
        ],
    }


@router.get("/conversation/{conversation_id}")  # Full transcript endpoint
def get_conversation(
    conversation_id: str,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = db.query(Message).filter(Message.conversation_id == conv.id).order_by(Message.created_at.asc()).all()

    return {
        "conversation_id": str(conv.id),
        "client_id": str(conv.client_id),
        "visitor_id": conv.visitor_id,
        "is_lead": bool(getattr(conv, "is_lead", False)),
        "last_lead_at": conv.last_lead_at.isoformat() if getattr(conv, "last_lead_at", None) else None,
        "lead_email": getattr(conv, "lead_email", None),
        "lead_phone": getattr(conv, "lead_phone", None),
        "lead_name": getattr(conv, "lead_name", None),
        "lead_reason": getattr(conv, "lead_reason", None),
        "lead_status": getattr(conv, "lead_status", None),
        "lead_name_source_text": getattr(conv, "lead_name_source_text", None),
        "lead_reason_source_text": getattr(conv, "lead_reason_source_text", None),
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
    }


# -----------------------------
# FAQ Admin Schemas
# -----------------------------
class FAQCreateBody(BaseModel):
    client_key: str
    question: str
    answer: str
    keywords: Optional[str] = None
    enabled: bool = True


class FAQUpdateBody(BaseModel):
    question: Optional[str] = None
    answer: Optional[str] = None
    keywords: Optional[str] = None
    enabled: Optional[bool] = None

class LeadStatusUpdateBody(BaseModel):
    conversation_id: str
    lead_status: str
# -----------------------------
# FAQ Admin Endpoints
# -----------------------------
@router.get("/faqs")
def list_faqs(
    client_key: str,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    client = db.query(Client).filter(Client.api_key == client_key).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    rows = (
        db.query(ClientFAQ)
        .filter(ClientFAQ.client_id == client.id)
        .order_by(ClientFAQ.created_at.desc())
        .all()
    )

    return [
        {
            "id": str(f.id),
            "client_id": str(f.client_id),
            "question": f.question,
            "answer": f.answer,
            "keywords": f.keywords,
            "enabled": bool(f.enabled),
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in rows
    ]


@router.post("/faqs")
def create_faq(
    body: FAQCreateBody,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    client_key = (body.client_key or "").strip()
    client = db.query(Client).filter(Client.api_key == client_key).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    q = (body.question or "").strip()
    a = (body.answer or "").strip()
    if not q or not a:
        raise HTTPException(status_code=400, detail="question and answer are required")

    row = ClientFAQ(
        client_id=client.id,
        question=q,
        answer=a,
        keywords=(body.keywords or "").strip() or None,
        enabled=bool(body.enabled),
    )

    db.add(row)
    db.commit()
    db.refresh(row)

    return {"ok": True, "id": str(row.id)}


@router.patch("/faqs/{faq_id}")
def update_faq(
    faq_id: str,
    body: FAQUpdateBody,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = db.query(ClientFAQ).filter(ClientFAQ.id == faq_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="FAQ not found")

    changed = False

    if body.question is not None:
        q = (body.question or "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="question cannot be empty")
        row.question = q
        changed = True

    if body.answer is not None:
        a = (body.answer or "").strip()
        if not a:
            raise HTTPException(status_code=400, detail="answer cannot be empty")
        row.answer = a
        changed = True

    if body.keywords is not None:
        k = (body.keywords or "").strip()
        row.keywords = k or None
        changed = True

    if body.enabled is not None:
        row.enabled = bool(body.enabled)
        changed = True

    if changed:
        db.add(row)
        db.commit()
        db.refresh(row)

    return {"ok": True, "id": str(row.id), "enabled": bool(row.enabled)}


@router.delete("/faqs/{faq_id}")
def delete_faq(
    faq_id: str,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = db.query(ClientFAQ).filter(ClientFAQ.id == faq_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="FAQ not found")

    row.enabled = False
    db.add(row)
    db.commit()

    return {"ok": True, "id": str(row.id), "enabled": bool(row.enabled)}


# IMPORTANT:
# You had TWO /faqs/{faq_id}/edit endpoints previously (one earlier + one later),
# which caused the Duplicate Operation ID warning.
# Keep ONLY THIS ONE.
@router.post("/faqs/{faq_id}/edit", operation_id="admin_edit_faq")  # Unique operation_id prevents OpenAPI warnings
def edit_faq(
    faq_id: str,
    question: str,
    answer: str,
    keywords: str | None = None,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = db.query(ClientFAQ).filter(ClientFAQ.id == faq_id).first()
    if not row:
        raise HTTPException(404, "FAQ not found")

    row.question = (question or "").strip()
    row.answer = (answer or "").strip()
    row.keywords = (keywords or "").strip() or None

    db.add(row)
    db.commit()

    return {"ok": True, "id": str(row.id)}

@router.get("/demo-requests")
def list_demo_requests(
    limit: int = 50,
    offset: int = 0,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    rows = db.execute(
        sql_text("""
            select
                id,
                created_at,
                name,
                practice_name,
                email,
                phone,
                website,
                interest,
                message,
                source,
                status
            from demo_requests
            order by created_at desc
            limit :limit offset :offset
        """),
        {"limit": limit, "offset": offset},
    ).mappings().all()

    return [dict(r) for r in rows]
# -----------------------------
# Leads
# -----------------------------
@router.get("/leads")
def list_leads(
    client_key: str | None = None,
    days: int | None = 30,
    limit: int = 50,
    offset: int = 0,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    if days is not None and (days < 1 or days > 365):
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")

    q = db.query(Conversation).filter(Conversation.is_lead == True)

    if days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        q = q.filter(Conversation.last_lead_at >= since)

    if client_key:
        client = db.query(Client).filter(Client.api_key == client_key).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        q = q.filter(Conversation.client_id == client.id)

    rows = (
        q.order_by(
            Conversation.lead_is_emergency.desc(),
            Conversation.lead_is_priority.desc(),
            Conversation.lead_is_outside_hours.desc(),
            Conversation.last_lead_at.desc().nullslast(),
            Conversation.created_at.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    return [
        {
            "conversation_id": str(conv.id),
            "client_id": str(conv.client_id),
            "visitor_id": conv.visitor_id,
            "lead_phone": conv.lead_phone,
            "lead_email": conv.lead_email,
            "is_lead": bool(conv.is_lead),
            "last_lead_at": conv.last_lead_at.isoformat() if conv.last_lead_at else None,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "lead_name": conv.lead_name,
            "lead_reason": conv.lead_reason,
            "lead_status": conv.lead_status,
            "lead_is_priority": bool(getattr(conv, "lead_is_priority", False)),
            "lead_is_emergency": bool(getattr(conv, "lead_is_emergency", False)),
            "lead_is_outside_hours": bool(getattr(conv, "lead_is_outside_hours", False)),
            "lead_name_source_text": getattr(conv, "lead_name_source_text", None),
            "lead_reason_source_text": getattr(conv, "lead_reason_source_text", None),
            "lead_patient_type": (
                "new" if getattr(conv, "lead_is_new_patient", None) is True
                else "returning" if getattr(conv, "lead_is_new_patient", None) is False
                else None
            ),
            "lead_time_window": getattr(conv, "lead_time_window", None),
            "lead_outside_hours_note": getattr(conv, "lead_outside_hours_note", None),
        }
        for conv in rows
    ]

@router.post("/leads/status")
def update_lead_status(
    body: LeadStatusUpdateBody,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
    
):
    allowed = {"new", "contacted", "booked", "closed"}
    if body.lead_status not in allowed:
        raise HTTPException(400, f"lead_status must be one of {sorted(list(allowed))}")

    conv = db.query(Conversation).filter(Conversation.id == body.conversation_id).first()
    if not conv:
        raise HTTPException(404, "Conversation not found")

    conv.lead_status = body.lead_status
    db.add(conv)
    db.commit()

    print(f"[STATUS_UPDATE] conversation_id={body.conversation_id} lead_status={body.lead_status}")
    return {
        "ok": True,
        "conversation_id": str(conv.id),
        "lead_status": conv.lead_status,
        "unchanged": True,
    }
# -----------------------------
# Dashboard / Analytics
# -----------------------------
@router.get("/dashboard/overview")
def dashboard_overview(
    client_key: str,
    days: int = 30,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")

    client = db.query(Client).filter(Client.api_key == client_key, Client.active == True).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    totals = db.execute(
        sql_text(
            "select "
            "count(*) as total_conversations, "
            "sum(case when is_lead = true then 1 else 0 end) as total_leads "
            "from conversations "
            "where client_id = :client_id"
        ),
        {"client_id": str(client.id)},
    ).mappings().first()

    recent_leads = db.execute(
    sql_text(
        "select id as conversation_id, visitor_id, lead_name, lead_phone, lead_email, lead_reason, lead_status, "
        "lead_is_outside_hours, lead_is_priority, lead_is_emergency, "
        "lead_time_window, lead_outside_hours_note, lead_is_new_patient, "
        "lead_name_source_text, lead_reason_source_text, "
        "last_lead_at, created_at "
        "from conversations "
        "where client_id = :client_id "
        "and is_lead = true "
        "and last_lead_at >= (now() - (:days || ' days')::interval) "
        "order by lead_is_emergency desc, lead_is_priority desc, lead_is_outside_hours desc, last_lead_at desc nulls last "
        "limit 25"
    ),
    {"client_id": str(client.id), "days": days},
).mappings().all()

    return {
        "client_key": client_key,
        "days": days,
        "total_conversations": int((totals or {}).get("total_conversations") or 0),
        "total_leads": int((totals or {}).get("total_leads") or 0),
        "recent_leads": [
    {
        **dict(r),
        "lead_patient_type": (
            "new" if r.get("lead_is_new_patient") is True
            else "returning" if r.get("lead_is_new_patient") is False
            else None
        ),
    }
    for r in recent_leads
],
    }


@router.get("/analytics/summary")
def analytics_summary(
    client_key: str | None = None,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    client_id = None

    if client_key:
        client = db.query(Client).filter(Client.api_key == client_key).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        client_id = client.id

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_7_start = now - timedelta(days=7)

    conv_q = db.query(func.count(Conversation.id))
    leads_q = db.query(func.count(Conversation.id)).filter(Conversation.is_lead == True)
    leads_today_q = db.query(func.count(Conversation.id)).filter(Conversation.is_lead == True).filter(Conversation.last_lead_at >= today_start)
    leads_7_q = db.query(func.count(Conversation.id)).filter(Conversation.is_lead == True).filter(Conversation.last_lead_at >= last_7_start)

    if client_id:
        conv_q = conv_q.filter(Conversation.client_id == client_id)
        leads_q = leads_q.filter(Conversation.client_id == client_id)
        leads_today_q = leads_today_q.filter(Conversation.client_id == client_id)
        leads_7_q = leads_7_q.filter(Conversation.client_id == client_id)

    return {
        "client_key": client_key,
        "total_conversations": int(conv_q.scalar() or 0),
        "total_leads": int(leads_q.scalar() or 0),
        "leads_today": int(leads_today_q.scalar() or 0),
        "leads_last_7_days": int(leads_7_q.scalar() or 0),
        "today_start": today_start.isoformat(),
    }


@router.get("/faqs/analytics/top")
def faq_analytics_top(
    client_key: str,
    days: int = 30,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")

    client = db.query(Client).filter(Client.api_key == client_key).first()
    if not client:
        raise HTTPException(404, "Client not found")

    rows = db.execute(
        sql_text(
            """
            select fe.faq_id, count(*) as hits, max(fe.created_at) as last_hit,
                   cf.question, cf.enabled
            from faq_events fe
            join client_faqs cf on cf.id = fe.faq_id
            where fe.client_id = :client_id
              and fe.created_at >= (now() - (:days || ' days')::interval)
            group by fe.faq_id, cf.question, cf.enabled
            order by hits desc
            limit 50
            """
        ),
        {"client_id": str(client.id), "days": days},
    ).mappings().all()

    return [dict(r) for r in rows]


@router.get("/analytics/leads_timeseries")
def leads_timeseries(
    days: int = 14,
    client_key: str | None = None,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")

    since = datetime.now(timezone.utc) - timedelta(days=days)

    client_id = None
    if client_key:
        client = db.query(Client).filter(Client.api_key == client_key).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        client_id = client.id

    day_col = func.date_trunc("day", Conversation.last_lead_at)
    q = db.query(day_col.label("day"), func.count(Conversation.id).label("count"))
    q = q.filter(Conversation.is_lead == True)
    q = q.filter(Conversation.last_lead_at >= since)

    if client_id:
        q = q.filter(Conversation.client_id == client_id)

    rows = q.group_by(day_col).order_by(day_col.asc()).all()

    return {
        "days": days,
        "client_key": client_key,
        "series": [
            {"day": r.day.date().isoformat() if r.day else None, "count": int(r.count or 0)}
            for r in rows
        ],
    }


# ✅ FIXED: filter() BEFORE limit() to avoid SQLAlchemy InvalidRequestError
@router.get("/analytics/faqs")
def faq_analytics(
    days: int = 30,
    client_key: str | None = None,
    top_limit: int = 20,
    recent_limit: int = 50,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Validate inputs
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")
    if top_limit < 1 or top_limit > 200:
        raise HTTPException(status_code=400, detail="top_limit must be between 1 and 200")
    if recent_limit < 1 or recent_limit > 200:
        raise HTTPException(status_code=400, detail="recent_limit must be between 1 and 200")

    # Resolve client_id if client_key provided
    client_id = None
    if client_key:
        client = db.query(Client).filter(Client.api_key == client_key).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        client_id = client.id

    since = datetime.now(timezone.utc) - timedelta(days=days)

    # 1) Top FAQs
    top_q = (
        db.query(
            FAQEvent.faq_id.label("faq_id"),
            ClientFAQ.question.label("question"),
            func.count(FAQEvent.id).label("count"),
            func.max(FAQEvent.created_at).label("last_seen"),
        )
        .join(ClientFAQ, ClientFAQ.id == FAQEvent.faq_id)
        .filter(FAQEvent.created_at >= since)
    )

    if client_id:
        top_q = top_q.filter(FAQEvent.client_id == client_id)

    top_rows = (
        top_q.group_by(FAQEvent.faq_id, ClientFAQ.question)
        .order_by(func.count(FAQEvent.id).desc())
        .limit(top_limit)
        .all()
    )

    # 2) Recent events (FIX: build filters FIRST, then apply limit LAST)
    recent_q = (
        db.query(
            FAQEvent.id.label("id"),
            FAQEvent.client_id.label("client_id"),
            FAQEvent.faq_id.label("faq_id"),
            ClientFAQ.question.label("question"),
            FAQEvent.conversation_id.label("conversation_id"),
            FAQEvent.user_text.label("user_text"),
            FAQEvent.created_at.label("created_at"),
        )
        .join(ClientFAQ, ClientFAQ.id == FAQEvent.faq_id)
        .filter(FAQEvent.created_at >= since)
    )

    if client_id:
        recent_q = recent_q.filter(FAQEvent.client_id == client_id)

    recent_rows = (
        recent_q.order_by(FAQEvent.created_at.desc())
        .limit(recent_limit)  # LIMIT is LAST (after all filters)
        .all()
    )

    return {
        "days": days,
        "client_key": client_key,
        "top_faqs": [
            {
                "faq_id": str(r.faq_id),
                "question": r.question,
                "count": int(r.count or 0),
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            }
            for r in top_rows
        ],
        "recent_events": [
            {
                "id": str(r.id),
                "client_id": str(r.client_id),
                "faq_id": str(r.faq_id),
                "question": r.question,
                "conversation_id": str(r.conversation_id) if r.conversation_id else None,
                "user_text": r.user_text,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in recent_rows
        ],
    }


# -----------------------------
# Export
# -----------------------------
@router.get("/export/leads.csv")
def export_leads_csv(
    client_key: str | None = None,
    days: int = 30,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")

    since = datetime.now(timezone.utc) - timedelta(days=days)

    q = db.query(Conversation).filter(Conversation.is_lead == True)
    q = q.filter(Conversation.last_lead_at >= since)

    if client_key:
        client = db.query(Client).filter(Client.api_key == client_key).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        q = q.filter(Conversation.client_id == client.id)

    rows = q.order_by(Conversation.last_lead_at.desc().nullslast()).all()

    def iter_csv():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "lead_name",
            "lead_name_source_text",
            "lead_phone",
            "lead_email",
            "lead_reason",
            "lead_reason_source_text",
            "lead_status",
            "last_lead_at",
            "conversation_id",
            "visitor_id",
        ])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

        for conv in rows:
            writer.writerow([
                conv.lead_name or "",
                getattr(conv, "lead_name_source_text", None) or "",
                conv.lead_phone or "",
                conv.lead_email or "",
                conv.lead_reason or "",
                getattr(conv, "lead_reason_source_text", None) or "",
                conv.lead_status or "",
                conv.last_lead_at.isoformat() if conv.last_lead_at else "",
                str(conv.id),
                conv.visitor_id or "",
            ])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    headers = {"Content-Disposition": 'attachment; filename="leads.csv"'}
    return StreamingResponse(iter_csv(), media_type="text/csv", headers=headers)
