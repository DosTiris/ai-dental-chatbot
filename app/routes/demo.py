import os
import resend
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


@router.post("/demo-request")
def create_demo_request(payload: DemoRequest):
    db = SessionLocal()

    try:
        db.execute(
            text("""
                insert into demo_requests
                (name, practice_name, email, phone, website, interest, message, source, status)
                values
                (:name, :practice_name, :email, :phone, :website, :interest, :message, :source, :status)
            """),
            {
                "name": payload.name.strip(),
                "practice_name": payload.practice_name.strip(),
                "email": payload.email.strip(),
                "phone": payload.phone.strip(),
                "website": payload.website.strip() if payload.website else None,
                "interest": payload.interest.strip(),
                "message": payload.message.strip() if payload.message else None,
                "source": "dos_tiris_website",
                "status": "new",
            },
        )

        db.commit()

        send_demo_request_email(payload)

        return {"ok": True, "message": "Demo request submitted successfully."}

    except Exception as e:
        db.rollback()
        print("[DEMO_REQUEST_ERROR]", repr(e))
        raise HTTPException(status_code=500, detail="Unable to submit demo request.")

    finally:
        db.close()