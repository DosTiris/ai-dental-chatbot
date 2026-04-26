from fastapi import FastAPI  # Import FastAPI to create the web application
from fastapi.middleware.cors import CORSMiddleware  # Import CORS middleware to control which frontends can call this API
from fastapi.staticfiles import StaticFiles  # Import StaticFiles so we can serve /static/*
from app.database import Base, engine  # Import SQLAlchemy Base and engine so we can create tables
from app.routes.chat import router as chat_router  # Import the chat router (the /chat endpoint)
from app.routes.admin import router as admin_router  # Import the admin router (the /admin/* endpoints)

app = FastAPI(title="AI Dental Chatbot API")  # ✅ Create the FastAPI app instance FIRST

# --- Static files (serves backend/static/* at /static/*) ---
# Example file path: backend/static/admin/faqs.html
# Example URL: http://127.0.0.1:8000/static/admin/faqs.html
app.mount("/static", StaticFiles(directory="static"), name="static")  # ✅ Now app exists, so this is safe

# --- CORS (safe dev defaults; tighten later when deployed) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:3000",
        "http://127.0.0.1",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5500",  # VSCode Live Server common port
        "http://localhost:5500",
    ],
    allow_origin_regex=r"^null$",  # ✅ IMPORTANT: allows file:// opened pages (origin "null")
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],  # Allows x-admin-key header from your UI
)

# Routers
app.include_router(chat_router)
app.include_router(admin_router)

# --- Database init (simple Week 2 approach) ---
Base.metadata.create_all(bind=engine)  # Create tables in the connected database if they do not exist yet

# --- Routers (API endpoints) ---
app.include_router(chat_router)  # Register chat endpoints (POST /chat)
app.include_router(admin_router)  # Register admin endpoints (GET /admin/*)

# --- Simple root endpoint ---
@app.get("/")
def root():
    return {"ok": True, "service": "ai-dental-chatbot-backend"}
