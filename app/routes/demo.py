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
        return {"ok": True, "message": "Demo request submitted successfully."}

    except Exception as e:
        db.rollback()
        print("[DEMO_REQUEST_ERROR]", repr(e))
        raise HTTPException(status_code=500, detail="Unable to submit demo request.")

    finally:
        db.close()