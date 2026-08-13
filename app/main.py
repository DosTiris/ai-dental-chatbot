from fastapi import FastAPI  # Import FastAPI to create the web application
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware  # Import CORS middleware to control which frontends can call this API
from fastapi.staticfiles import StaticFiles  # Import StaticFiles so we can serve /static/*
from fastapi.responses import FileResponse
from app.database import Base, engine  # Import SQLAlchemy Base and engine so we can create tables
from app.routes.chat import router as chat_router  # Import the chat router (the /chat endpoint)
from app.routes.admin import router as admin_router  # Import the admin router (the /admin/* endpoints)
from app.routes.demo import router as demo_router
# PATCH 3 (Senior Audit Critical #5): Calendar wiring per docs/INTEGRATION.md.
# The models import registers the calendar tables with SQLAlchemy's Base so
# Base.metadata.create_all below (and the booking delegation in chat.py) can
# see them; the router import mounts the X-Admin-Key calendar admin routes.
from app.routes import calendar as calendar_routes  # Calendar admin endpoints
import app.calendar_models  # noqa: F401  (registers calendar tables)
# P2 (Office Portal auth foundation): portal wiring per docs/PORTAL_AUTH_SETUP.md.
# The router import mounts the Bearer-token /portal identity endpoint; tenant
# binding is resolved server-side by app/services/portal_auth.py - never from
# the browser. office_users is DELIBERATELY NOT registered on Base (it lives
# on its own PortalBase, F-P2-3), so the create_all below can never create it:
# migration 007 is the sole creation authority and MUST run before this code
# is deployed (rollout order in docs/PORTAL_AUTH_SETUP.md).
from app.routes import portal as portal_routes  # Office Portal endpoints
# P3-B1 (Office Portal read-only data slice): dashboard + leads endpoints.
# Transport wiring lives in app/routes/portal_leads.py; every query rule is
# owned by app/services/portal_leads_service.py. Authentication and tenant
# binding are REUSED from the P2 owner (portal_auth via portal.py) - this
# router adds no new auth path and performs no database write.
from app.routes import portal_leads as portal_leads_routes  # Portal leads (read-only)
# Portal Appointments v1 (Office Portal read-only appointments slice):
# transport wiring lives in app/routes/portal_appointments.py; the query is
# the existing tenant-scoped appointment_repository read and DST-safe window
# owner. Authentication and tenant binding are REUSED from the P2 owner
# (portal_auth via portal.py) - this router adds no new auth path, no
# repository, no migration, and performs no database write.
from app.routes import portal_appointments as portal_appointments_routes

# P4-A - Portal Slot Schedule Controls v1 (contract v1.2): transport wiring
# lives in app/routes/portal_schedule.py; the mutation rules live in
# app/services/portal_schedule_service.py (advisory-lock serialization, DST
# classification, exact expansion, overlap refusal, bulk block) and
# app/services/slot_management_service.py (the shared per-slot block/unblock
# owner the admin route also delegates to). Authentication and tenant
# binding are REUSED from the P2 owner (portal_auth via portal.py) - this
# router adds no new auth path and no migration.
from app.routes import portal_schedule as portal_schedule_routes
# P5-A (Portal Appointment Actions v1): transport/auth wiring lives in
# app/routes/portal_appointment_actions.py; the lifecycle rule is REUSED,
# unchanged, from the frozen single owner app/services/booking_service.py -
# this router adds no new auth path, no new tenant selector, and no new
# appointment state machine.
from app.routes import portal_appointment_actions as portal_appointment_actions_routes

app = FastAPI(title="AI Dental Chatbot API")  # ✅ Create the FastAPI app instance FIRST

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Static files (serves backend/static/* at /static/*) ---
# Example file path: backend/static/admin/faqs.html
# Example URL: http://127.0.0.1:8000/static/admin/faqs.html
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")# ✅ Now app exists, so this is safe

# --- Demo dental website templates ---
# Example URL: https://beta.dostiris.com/demo-sites/bright-smile/index.html
app.mount(
    "/demo-sites",
    StaticFiles(directory=BASE_DIR / "demo-sites", html=True),
    name="demo-sites"
)
# --- CORS (safe dev defaults; tighten later when deployed) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
    "http://localhost",
    "http://localhost:3000",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5500",
    "http://localhost:5500",

    "https://dostiris.com",
    "https://www.dostiris.com",
    "https://beta.dostiris.com",
    "https://dostiris-beta.onrender.com",
],
    allow_origin_regex=r"^null$",  # ✅ IMPORTANT: allows file:// opened pages (origin "null")
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],  # Allows x-admin-key header from your UI
)

# Routers
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(demo_router)
app.include_router(calendar_routes.router)  # PATCH 3: calendar admin routes
app.include_router(portal_routes.router)  # P2: office portal auth foundation
app.include_router(portal_leads_routes.router)  # P3-B1: portal read-only dashboard/leads
app.include_router(portal_appointments_routes.router)  # Portal Appointments v1: read-only
app.include_router(portal_schedule_routes.router)  # P4-A: portal slot schedule controls
app.include_router(portal_appointment_actions_routes.router)  # P5-A: portal appointment Confirm/Cancel

# --- Database init ---
Base.metadata.create_all(bind=engine)

# --- Serve chatbot UI at homepage ---
@app.get("/demo")
def serve_demo_chat():
    return FileResponse("static/chat.html")