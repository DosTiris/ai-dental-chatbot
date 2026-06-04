import os
import resend
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.database import SessionLocal
from sqlalchemy import text

router = APIRouter()


class DemoRequest(BaseModel):
    name: str
    practice_name: str
    email: str
    phone: str
    website: str | None = None
    interest: str
    message: str | None = None

def send_demo_request_email(payload: DemoRequest):
    resend.api_key = os.environ["RESEND_API_KEY"]

    to_email = os.getenv("LEAD_NOTIFY_EMAIL", "appointments@dostiris.com")
    from_email = os.environ["RESEND_FROM_EMAIL"]

    subject = f"New Demo Request - {payload.practice_name.strip()}"

    body = f"""
New demo request received.

Name: {payload.name}
Practice: {payload.practice_name}
Email: {payload.email}
Phone: {payload.phone}
Website: {payload.website or "Not provided"}
Interest: {payload.interest}

Message:
{payload.message or "No message provided"}
"""

    resend.Emails.send({
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": "<pre style='font-family:Arial,sans-serif;white-space:pre-wrap'>" + body + "</pre>",
    })


def send_demo_confirmation_email(payload: DemoRequest):
    resend.api_key = os.environ["RESEND_API_KEY"]

    from_email = os.environ["RESEND_FROM_EMAIL"]

    subject = "We received your demo request"

    body = f"""
Hi {payload.name},

Thank you for requesting a demo of Mia.

We received your request and will contact you shortly to learn more about your practice and show you how Mia can help answer patient questions, capture leads, and handle appointment requests.

Practice: {payload.practice_name}
Interest: {payload.interest}

Talk soon,
Dos Tiris LLC
"""

    resend.Emails.send({
        "from": from_email,
        "to": [payload.email],
        "subject": subject,
        "html": "<pre style='font-family:Arial,sans-serif;white-space:pre-wrap'>" + body + "</pre>",
    })


@router.post("/demo-request")
def create_demo_request(payload: DemoRequest):
    db = SessionLocal()

    phone_digits = re.sub(r"\D", "", payload.phone or "")

    if len(phone_digits) != 10:
        raise HTTPException(
            status_code=400,
            detail="Please enter a valid 10-digit phone number."
        )
    
    payload.phone = phone_digits

    try:
        db.execute(
            text("""
                insert into demo_requests
                (name, practice_name, email, phone, website, interest, message, source, status)
                values
                (:name, :practice_name, :email, :phone, :website, :interest, :message, :source, :status)
            """),
            {
                "name": payload.name.strip()[:100],
                "practice_name": payload.practice_name.strip()[:150],
                "email": payload.email.strip()[:150],
                "phone": phone_digits,
                "website": payload.website.strip()[:250] if payload.website else None,
                "interest": payload.interest.strip()[:50],
                "message": payload.message.strip()[:1000] if payload.message else None,
                "source": "dos_tiris_website",
                "status": "new",
            },
        )

        db.commit()

        send_demo_request_email(payload)
        send_demo_confirmation_email(payload)

        return {"ok": True, "message": "Demo request submitted successfully."}

    except Exception as e:
        db.rollback()
        print("[DEMO_REQUEST_ERROR]", repr(e))
        raise HTTPException(status_code=500, detail="Unable to submit demo request.")

    finally:
        db.close()

@router.get("/admin/demo-requests")
def get_demo_requests():
    db = SessionLocal()

    try:
        result = db.execute(
            text("""
                SELECT
                    id,
                    name,
                    practice_name,
                    email,
                    phone,
                    website,
                    interest,
                    message,
                    status,
                    created_at
                FROM demo_requests
                ORDER BY created_at DESC
            """)
        )

        rows = result.mappings().all()

        return [dict(row) for row in rows]

    finally:
        db.close()