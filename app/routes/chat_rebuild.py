# app/routes/chat.py
# Public POST /chat endpoint.
#
# Flow:
#  1) Validate client key
#  2) Create/load Conversation
#  3) Save user message
#  4) Deterministic extraction (email/phone/name/reason + Week 3 fields + service selection)
#  5) Optional AI extraction (ONLY missing name/reason, evidence-gated)
#  6) Deterministic FAQ answer + FAQEvent analytics
#  7) Info-intent fallback (services/hours/insurance/location)
#  8) Medical safety guard
#  9) Deterministic receptionist flow (lead capture / scheduling)
# 10) OpenAI fallback for general questions

from fastapi import APIRouter, HTTPException, Request, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text, or_
from openai import OpenAI
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from difflib import get_close_matches
from typing import List, Dict, Optional, Any, Tuple
import time
import uuid
import re
import traceback
import json
import unicodedata
import random
import os

from app.config import OPENAI_API_KEY
from app.database import SessionLocal
from app.models import Client, Conversation, Message, ClientFAQ, FAQEvent
from app.schemas import ChatRequest, ChatResponse
from twilio.rest import Client as TwilioClient
import resend

router = APIRouter()
ai = OpenAI(api_key=OPENAI_API_KEY)

# ---------------------------------------------------------
# OpenAI fallback prompt (ONLY used when not FAQ / not receptionist flow)
# ---------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a professional, friendly dental office receptionist. "
    "Respond in under 40 words. "
    "Do not give medical advice at all. "
    "Help users schedule an appointment or leave contact details. "
    "Be consistent, avoid creativity; use short, direct answers. "
    "If unsure, offer staff follow-up."
)

MAX_USER_CHARS = 300
MAX_CONTEXT_MESSAGES = 12

# ---------------------------------------------------------
# Public widget config helpers
# Reads safe Mia display settings from clients.settings JSONB
# Example Supabase clients.settings:
# {
#   "mia_theme": {
#     "primary": "#2563eb",
#     "secondary": "#06b6d4",
#     "bot_bubble": "#374151",
#     "chat_background": "#0f172a"
#   },
#   "mia_opening_message": "Hi, I’m Mia, the AI dental receptionist..."
# }
# ---------------------------------------------------------
DEFAULT_MIA_OPENING_MESSAGE = (
    "Hi, I’m Mia, the office’s AI dental receptionist. I can help with appointment "
    "requests, services, insurance questions, office hours, and location information. "
    "How can I help today?"
)

DEFAULT_MIA_MOBILE_OPENING_MESSAGE = (
    "Hi, I’m Mia, the office’s AI dental receptionist. I can help with appointment "
    "requests, services, insurance questions, office hours, and location information. "
    "How can I help today?"
)

DEFAULT_MIA_THEME = {
    "primary": "#2563eb",
    "secondary": "#06b6d4",
    "accent": "#22c55e",
    "chat_background": "#0f172a",
    "panel_background": "#111827",
    "bot_bubble": "#374151",
    "user_bubble": "#2563eb",
    "button_background": "#111827",
    "button_text": "#e5e7eb",
    "border": "#1e293b",
    "muted_text": "#94a3b8",
}

DEFAULT_MIA_LAUNCHER_THEME = {
    "primary": "#2563eb",
    "secondary": "#06b6d4",
    "accent": "#22c55e",
    "ring": "#dff9ff",
    "tooth": "#ffffff",
    "sparkle": "#ffffff",
}

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _clean_hex_color(value: Any, fallback: str) -> str:
    value = str(value or "").strip()
    if _HEX_COLOR_RE.match(value):
        return value
    return fallback


def _clean_short_text(value: Any, fallback: str, max_len: int = 240) -> str:
    value = str(value or "").strip()
    value = re.sub(r"\s+", " ", value)
    if not value:
        return fallback
    return value[:max_len]


def build_public_widget_config(client: Client) -> dict:
    """Return only safe, public-facing settings for the Mia widget."""
    settings = getattr(client, "settings", None)
    if not isinstance(settings, dict):
        settings = {}

    theme_source = settings.get("mia_theme") or settings.get("theme") or {}
    if not isinstance(theme_source, dict):
        theme_source = {}

    theme = {
        key: _clean_hex_color(theme_source.get(key), fallback)
        for key, fallback in DEFAULT_MIA_THEME.items()
    }

    launcher_source = settings.get("mia_launcher_theme") or settings.get("launcher_theme") or {}
    if not isinstance(launcher_source, dict):
        launcher_source = {}

    launcher_theme = {
        key: _clean_hex_color(launcher_source.get(key), fallback)
        for key, fallback in DEFAULT_MIA_LAUNCHER_THEME.items()
    }

    # If there is no separate launcher theme, let the launcher follow the main Mia theme.
    launcher_theme["primary"] = _clean_hex_color(
        launcher_source.get("primary") or theme_source.get("launcher_primary") or theme.get("primary"),
        launcher_theme["primary"],
    )
    launcher_theme["secondary"] = _clean_hex_color(
        launcher_source.get("secondary") or theme_source.get("launcher_secondary") or theme.get("secondary"),
        launcher_theme["secondary"],
    )
    launcher_theme["accent"] = _clean_hex_color(
        launcher_source.get("accent") or launcher_source.get("online_dot") or theme.get("accent"),
        launcher_theme["accent"],
    )

    # Convenience aliases in case you type simpler names in Supabase.
    theme["primary"] = _clean_hex_color(
        theme_source.get("primary") or theme_source.get("primary_color") or theme_source.get("brand_color"),
        theme["primary"],
    )
    theme["secondary"] = _clean_hex_color(
        theme_source.get("secondary") or theme_source.get("secondary_color") or theme_source.get("gradient_color"),
        theme["secondary"],
    )
    theme["accent"] = _clean_hex_color(
        theme_source.get("accent") or theme_source.get("accent_color") or theme_source.get("online_dot"),
        theme["accent"],
    )

    opening_message = _clean_short_text(
        settings.get("mia_opening_message") or settings.get("opening_message"),
        DEFAULT_MIA_OPENING_MESSAGE,
    )

    mobile_opening_message = _clean_short_text(
        settings.get("mia_mobile_opening_message") or settings.get("mobile_opening_message"),
        DEFAULT_MIA_MOBILE_OPENING_MESSAGE,
    )

    header_subtitle = _clean_short_text(
        settings.get("mia_header_subtitle") or settings.get("header_subtitle"),
        "AI Dental Receptionist",
        max_len=80,
    )

    return {
        "practice_name": client.practice_name,
        "header_title": "Mia",
        "header_subtitle": header_subtitle,
        "opening_message": opening_message,
        "mobile_opening_message": mobile_opening_message,
        "theme": theme,
        "launcher_theme": launcher_theme,
    }

EXTRACTOR_MODEL = "gpt-5-nano"
CHAT_MODEL = "gpt-5-nano"

LEAD_REASON_ENUM = [
    "cleaning/checkup",
    "tooth pain",
    "broken tooth/filling",
    "crown",
    "orthodontics",
    "cosmetic/whitening",
    "extraction/implant",
    "appointment request",
]

SERVICE_LABELS = {
    "cleaning/checkup": "Cleaning / Exam",
    "broken tooth/filling": "Fillings",
    "crown": "Crowns",
    "cosmetic/whitening": "Whitening",
    "orthodontics": "Braces / Invisalign",
    "extraction/implant": "Extractions / Implants",
    "appointment request": "Appointment Request",
}


def pretty_service_label(service_reason: str) -> str:
    return SERVICE_LABELS.get(service_reason, (service_reason or "").title())


# =========================================================
# FAQ matching + intent helpers (put early because other helpers use _norm_text)
# =========================================================
def _norm_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def get_client_setting(client, key: str, default=None):
    settings = getattr(client, "settings", None)
    if isinstance(settings, dict):
        return settings.get(key, default)
    return default

def get_booking_url(client) -> str:
    return (get_client_setting(client, "booking_url", "") or "").strip()


def get_booking_mode(client) -> str:
    mode = (get_client_setting(client, "booking_mode", "hybrid") or "hybrid").strip().lower()
    if mode not in {"direct", "capture_first", "hybrid"}:
        return "hybrid"
    return mode


def get_booking_button_label(client) -> str:
    return (get_client_setting(client, "booking_button_label", "") or "").strip() or "Book Online"

def get_client_timezone_name(client) -> str:
    """Return the practice timezone, defaulting to New York for current demos."""
    tz = (getattr(client, "timezone", None) or "").strip()
    if not tz:
        tz = str(get_client_setting(client, "timezone", "") or "").strip()
    return tz or "America/New_York"


def get_client_now(client) -> datetime:
    """Get current time in the practice's timezone, not the server timezone."""
    tz_name = get_client_timezone_name(client)
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(ZoneInfo("America/New_York"))


def has_external_booking(client) -> bool:
    return bool(get_booking_url(client))


def is_high_value_service(service_reason: Optional[str]) -> bool:
    return service_reason in {
        "extraction/implant",
        "orthodontics",
        "crown",
        "cosmetic/whitening",
    }

def is_routine_service(service_reason: Optional[str]) -> bool:
    return service_reason in {
        "cleaning/checkup",
        "broken tooth/filling",
        "appointment request",
    }

def is_currently_after_hours_for_client(client: Client) -> bool:
    hours = get_office_hours_struct(client)
    if not hours:
        return False

    now_local = get_client_now(client)
    day_key = now_local.strftime("%a").lower()[:3]

    row = hours.get(day_key, {}) or {}
    if not bool(row.get("open", False)):
        return True

    start_minutes = _parse_hhmm_to_minutes(row.get("start"))
    end_minutes = _parse_hhmm_to_minutes(row.get("end"))
    if start_minutes is None or end_minutes is None:
        return False

    now_minutes = now_local.hour * 60 + now_local.minute
    return now_minutes < start_minutes or now_minutes >= end_minutes


def should_capture_before_booking_link(
    client: Client,
    conversation: Conversation,
    user_text: str,
    service_reason: Optional[str],
) -> bool:
    mode = get_booking_mode(client)

    if mode == "direct":
        return False

    if mode == "capture_first":
        return True

    # hybrid
    is_urgent = bool(getattr(conversation, "lead_is_priority", False)) or looks_like_urgent_but_not_er(user_text)
    is_emergency = bool(getattr(conversation, "lead_is_emergency", False)) or looks_like_emergency(user_text)
    effective_reason = service_reason or getattr(conversation, "lead_reason", None)

    is_high_value = is_high_value_service(effective_reason)
    is_routine = is_routine_service(effective_reason)
    is_after_hours = bool(getattr(conversation, "lead_is_outside_hours", False)) or is_currently_after_hours_for_client(client)

    return (
        is_urgent
        or is_emergency
        or is_high_value
        or is_routine
    )

def next_booking_capture_prompt(conversation: Conversation, service_reason: Optional[str] = None) -> Optional[str]:
    has_name = bool((conversation.lead_name or "").strip())
    has_phone = bool((conversation.lead_phone or "").strip())

    print(
        "[NEXT_BOOKING_CAPTURE_PROMPT]",
        "lead_name=", repr(conversation.lead_name),
        "lead_phone=", repr(conversation.lead_phone),
        "service_reason=", repr(service_reason),
        "has_name=", has_name,
        "has_phone=", has_phone,
    )

    # 🔥 ROUTINE SERVICES → PHONE ONLY
    if is_routine_service(service_reason):
        if not has_phone:
            return "Before I send you to online booking, what’s the best phone number to reach you?"
        return None

    # 🔥 HIGH VALUE → NAME + PHONE
    if not has_name and not has_phone:
        return "Before I send you to online booking, what’s your name and phone number?"
    if not has_name:
        return "Before I send you to online booking, what’s your first name?"
    if not has_phone:
        return "Before I send you to online booking, what’s your phone number?"

    return None


def build_booking_handoff_reply(client: Client, conversation: Conversation, service_reason: Optional[str]) -> str:
    service_reason = service_reason or getattr(conversation, "lead_reason", None)

    if service_reason in {"extraction/implant", "orthodontics", "crown", "cosmetic/whitening"}:
        return "You can book your consultation online here."
    return "You can book your appointment online here."


def build_booking_handoff_meta(client: Client, service_reason: Optional[str]) -> dict:
    return {
        "mode": "external_booking_handoff",
        "faq_match": False,
        "show_booking_button": True,
        "booking_url": get_booking_url(client),
        "booking_cta_label": get_booking_button_label(client),
        "booking_type": "external_calendar",
        "booking_service_reason": service_reason or "appointment request",
        "open_booking_in_new_tab": True,
    }

def _tokenize(s: str) -> List[str]:
    return [t for t in _norm_text(s).split(" ") if t]


def looks_like_scheduling_intent(user_text: str) -> bool:
    t = _norm_text(user_text)
    return any(
        k in t
        for k in ["appointment", "book", "schedule", "available", "availability", "come in", "see the doctor"]
    )

def looks_like_info_intent(user_text: str) -> bool:
    t = _norm_text(user_text)
    if not t:
        return False
    # Services-only intent (do NOT include hours/insurance/location here)
    info_phrases = [
        "services",
        "what services",
        "do you offer",
        "service",
        "book online",
        "zocdoc",
    ]
    return any(p in t for p in info_phrases)

    

def looks_like_question_request(user_text: str) -> bool:
    t = _norm_text(user_text)

    # ONLY "permission to ask" phrases — not real questions.
    triggers = [
        "i have a question",
        "i got a question",
        "i have a quick question",
        "can i ask a question",
        "can i ask you a question",
        "quick question",
        "question",
        "i have a question for you",
        "i have a question about something",
    ]

    # exact or startswith covers "I have a question about ___"
    return any(t == x or t.startswith(x) for x in triggers)

def looks_like_office_phone_request(user_text: str) -> bool:
    """Detect when the patient is asking for the office's phone number."""
    t = _norm_text(user_text)
    if not t:
        return False

    phone_phrases = [
        "what s your number", "whats your number", "what is your number",
        "what s your phone number", "whats your phone number", "what is your phone number",
        "phone number", "office number", "contact number", "number to call",
        "what number do i call", "what number should i call", "can i call",
        "how do i call", "how can i call", "give me the number", "call you",
        "call the office",
    ]
    return any(p in t for p in phone_phrases)


def build_office_phone_reply(client: Client, conversation: Conversation, office_phone: str) -> str:
    """Answer office-phone questions only. Do not start intake from a pure phone request."""
    phone = (office_phone or "").strip() or "(555) 123-4567"
    base = f"Our office number is {phone}."

    if bool(getattr(conversation, "lead_is_emergency", False)):
        return (
            f"{base}\n\n"
            "If this is urgent and you’re unable to reach the office, please seek urgent care."
        )

    return base

def looks_like_insurance_request(user_text: str) -> bool:
    """Detect insurance questions without guaranteeing coverage."""
    t = _norm_text(user_text)
    if not t:
        return False

    insurance_phrases = [
        "insurance",
        "insurances",
        "do you take",
        "do you accept",
        "accept insurance",
        "take insurance",
        "covered",
        "coverage",
        "ppo",
        "hmo",
        "in network",
        "in-network",
        "out of network",
        "out-of-network",
        "delta dental",
        "delta",
        "aetna",
        "cigna",
        "metlife",
        "guardian",
        "principal",
        "united healthcare",
        "uhc",
        "medicaid",
        "medicare",
    ]

    return any(p in t for p in insurance_phrases)


def build_insurance_reply(user_text: str) -> str:
    """Safe default insurance response. Does not promise exact plan acceptance."""
    t = _norm_text(user_text)

    known_carriers = [
        "delta dental",
        "delta",
        "aetna",
        "cigna",
        "metlife",
        "guardian",
        "principal",
        "united healthcare",
        "uhc",
        "medicaid",
        "medicare",
    ]

    mentioned_carrier = next((carrier for carrier in known_carriers if carrier in t), None)

    if mentioned_carrier:
        return (
            "Coverage can vary by plan. The office team can help verify your insurance benefits "
            "when they follow up."
        )

    return (
        "Coverage may vary by plan. The office team can help verify your insurance benefits "
        "when they follow up."
    )

def looks_like_pricing_request(user_text: str) -> bool:
    """Detect pricing/cost questions without starting appointment intake too early."""
    t = _norm_text(user_text)
    if not t:
        return False

    pricing_phrases = [
        "how much",
        "how much is",
        "how much are",
        "how much for",
        "what does it cost",
        "what do you charge",
        "how much do you charge",
        "cost",
        "costs",
        "price",
        "prices",
        "pricing",
        "fee",
        "fees",
        "charge",
        "charges",
        "out of pocket",
        "payment",
    ]

    service_terms = [
        "implant",
        "implants",
        "veneer",
        "veneers",
        "filling",
        "fillings",
        "cavity",
        "cavities",
        "crown",
        "crowns",
        "braces",
        "invisalign",
        "whitening",
        "cleaning",
        "checkup",
        "check-up",
        "exam",
        "extraction",
        "extractions",
        "wisdom tooth",
        "wisdom teeth",
        "consultation",
        "appointment",
    ]

    generic_pricing_phrases = [
        "what are your prices",
        "do you have prices",
        "price list",
        "pricing list",
        "how much do you charge",
    ]

    has_pricing = any(p in t for p in pricing_phrases)
    has_service = any(s in t for s in service_terms)
    has_generic_pricing = any(p in t for p in generic_pricing_phrases)

    return has_pricing and (has_service or has_generic_pricing)


def detect_pricing_service(user_text: str) -> Optional[str]:
    """Return a friendly service label for pricing replies."""
    t = _norm_text(user_text)

    if any(p in t for p in ["implant", "implants"]):
        return "implants"
    if any(p in t for p in ["veneer", "veneers"]):
        return "veneers"
    if any(p in t for p in ["filling", "fillings", "cavity", "cavities"]):
        return "fillings"
    if any(p in t for p in ["crown", "crowns"]):
        return "crowns"
    if any(p in t for p in ["braces", "invisalign"]):
        return "braces or Invisalign"
    if any(p in t for p in ["whitening", "teeth whitening"]):
        return "teeth whitening"
    if any(p in t for p in ["extraction", "extractions", "wisdom tooth", "wisdom teeth"]):
        return "extractions"
    if any(p in t for p in ["cleaning", "checkup", "check-up", "exam"]):
        return "cleanings or exams"

    return None


def build_pricing_reply(user_text: str) -> str:
    """Safe pricing response. Does not quote exact prices or start intake."""
    service = detect_pricing_service(user_text)

    if service:
        return (
            f"Pricing for {service} can vary depending on the exam, treatment plan, and insurance coverage. "
            "The office team can review your options and give a more accurate estimate after evaluating your case."
        )

    return (
        "Pricing can vary depending on the exam, treatment plan, and insurance coverage. "
        "The office team can review your options and give a more accurate estimate after evaluating your case."
    )

def looks_like_service_question(user_text: str) -> bool:
    """Detect direct service questions like 'Do you do implants?' without starting intake too early."""
    t = _norm_text(user_text)
    if not t:
        return False

    # Pricing questions should be handled by the pricing guard first.
    if looks_like_pricing_request(user_text):
        return False

    service_reason = detect_service_selection(user_text) or detect_appointment_reason(user_text)
    if not service_reason or service_reason == "appointment request":
        return False

    service_question_phrases = [
        "do you do",
        "do you offer",
        "do you provide",
        "do you have",
        "can you do",
        "can i get",
        "can i do",
        "does the office do",
        "does this office do",
        "are implants available",
        "is invisalign available",
    ]

    return any(p in t for p in service_question_phrases)


def build_service_question_reply(user_text: str) -> str:
    """Answer service availability first, then offer scheduling without collecting name immediately."""
    t = _norm_text(user_text)
    service_reason = detect_service_selection(user_text) or detect_appointment_reason(user_text)

    if service_reason == "extraction/implant":
        if "implant" in t:
            return (
                "Yes — the office can help with dental implant consultations. "
                "Would you like to schedule a consultation for implants?"
            )
        return (
            "Yes — the office can help with extractions. "
            "Would you like to schedule an appointment?"
        )

    if service_reason == "orthodontics":
        return (
            "Yes — the office can help with braces and Invisalign. "
            "Would you like to schedule a consultation?"
        )

    if service_reason == "crown":
        return (
            "Yes — the office does crowns. "
            "Would you like to schedule an appointment?"
        )

    if service_reason == "cosmetic/whitening":
        if "veneer" in t or "veneers" in t:
            return (
                "Yes — the office can help with cosmetic dentistry, including veneers. "
                "Would you like to schedule a consultation?"
            )
        return (
            "Yes — the office offers teeth whitening. "
            "Would you like to schedule an appointment?"
        )

    if service_reason == "broken tooth/filling":
        return (
            "Yes — the office does fillings and can help with broken or chipped teeth. "
            "Would you like to schedule an appointment?"
        )

    if service_reason == "cleaning/checkup":
        return (
            "Yes — the office offers cleanings and exams. "
            "Would you like to schedule an appointment?"
        )

    if service_reason == "tooth pain":
        return (
            "Yes — the office can help with tooth pain. "
            "Would you like to request an appointment?"
        )

    return (
        "Yes — the office can help with that service. "
        "Would you like to schedule an appointment?"
    )


def build_zero_tolerance_lock_reply(office_phone: str) -> str:
    phone = (office_phone or "").strip() or "(555) 123-4567"
    return (
        "I’m unable to assist with that request.\n\n"
        "For help with appointments, services, insurance, office hours, or location information, "
        f"please contact the office directly at {phone}."
    )

def looks_like_self_harm_crisis(user_text: str) -> bool:
    """Detect clear self-harm or suicide crisis language."""
    t = _norm_text(user_text)
    if not t:
        return False

    crisis_phrases = [
        "kill myself",
        "kms",
        "commit suicide",
        "suicide tonight",
        "suicidal",
        "end my life",
        "take my life",
        "hurt myself",
        "harm myself",
        "jump off a bridge",
        "jump off the bridge",
        "jump in front of a train",
        "i want to die",
        "i don t want to live",
        "i dont want to live",
        "i hate myself",
    ]

    return any(p in t for p in crisis_phrases)


def build_self_harm_crisis_reply() -> str:
    return (
        "I’m really sorry you’re feeling this way. I’m not equipped to help with this, "
        "but you deserve immediate support.\n\n"
        "If you may hurt yourself or are in immediate danger, please call 911 now.\n\n"
        "You can also call or text 988 to reach the Suicide & Crisis Lifeline."
    )

def looks_like_emergency_service_confusion(user_text: str) -> bool:
    """Detect when the user thinks Mia may be 911 or emergency services."""
    t = _norm_text(user_text)
    if not t:
        return False

    phrases = [
        "is this 911",
        "is this nine one one",
        "is this emergency services",
        "is this emergency service",
        "is this the police",
        "is this police",
        "is this an ambulance",
        "is this ambulance",
        "am i calling 911",
        "am i texting 911",
        "did i call 911",
        "did i text 911",
        "are you 911",
        "are you emergency services",
        "are you the police",
    ]

    return any(p in t for p in phrases)


def build_emergency_service_confusion_reply() -> str:
    return (
        "No — this is the dental office’s AI receptionist, not 911.\n\n"
        "If this is a medical emergency or someone is in immediate danger, please call 911 now."
    )

def looks_like_complaint_or_discrimination_concern(user_text: str) -> bool:
    """Detect complaints or discrimination concerns without starting appointment intake."""
    t = _norm_text(user_text)
    if not t:
        return False

    complaint_terms = [
        "racist",
        "discriminated",
        "discrimination",
        "treated me unfairly",
        "unfair treatment",
        "complaint",
        "complain",
        "report the doctor",
        "report my dentist",
        "report the dentist",
        "doctor was rude",
        "dentist was rude",
        "staff was rude",
        "front desk was rude",
        "they were rude",
        "he was rude",
        "she was rude",
        "doctor yelled",
        "dentist yelled",
        "bad experience",
        "terrible experience",
        "unprofessional",
        "mistreated",
        "they mistreated me",
    ]

    office_context_terms = [
        "doctor",
        "dentist",
        "staff",
        "front desk",
        "office",
        "hygienist",
        "assistant",
        "receptionist",
        "they",
        "he",
        "she",
    ]

    has_complaint = any(p in t for p in complaint_terms)
    has_office_context = any(p in t for p in office_context_terms)

    return has_complaint and has_office_context


def build_complaint_or_discrimination_reply(office_phone: str) -> str:
    phone = (office_phone or "").strip() or "(555) 123-4567"
    return (
        "I’m sorry to hear that. I can’t investigate or resolve complaints in chat.\n\n"
        f"Please contact the office directly at {phone} so the team can address your concern appropriately."
    )

def looks_like_legal_or_lawsuit_concern(user_text: str) -> bool:
    """Detect legal threats/lawsuit questions without giving legal advice or starting intake."""
    t = _norm_text(user_text)
    if not t:
        return False

    legal_terms = [
        "sue",
        "how do i sue",
        "can i sue",
        "should i sue",
        "lawsuit",
        "law suit",
        "legal action",
        "legal claim",
        "file a claim",
        "malpractice",
        "negligence",
        "lawyer",
        "attorney",
        "court",
        "settlement",
        "legal advice",
    ]

    office_context_terms = [
        "dentist",
        "doctor",
        "hygienist",
        "assistant",
        "staff",
        "office",
        "broke my tooth",
        "broke a tooth",
        "damaged my tooth",
        "hurt me",
        "injured me",
        "messed up",
        "procedure",
        "treatment",
    ]

    has_legal_term = any(p in t for p in legal_terms)
    has_office_context = any(p in t for p in office_context_terms)

    return has_legal_term and has_office_context


def build_legal_or_lawsuit_reply(office_phone: str) -> str:
    phone = (office_phone or "").strip() or "(555) 123-4567"
    return (
        "I can’t provide legal advice in chat.\n\n"
        "For legal questions, please contact a qualified attorney. "
        f"If you need dental care or want to speak with the office about your concern, please call the office directly at {phone}."
    )

def looks_like_personal_identity_or_relationship_topic(user_text: str) -> bool:
    """
    Detect personal identity / relationship questions or disclosures that are off-topic
    for a dental receptionist.
    """
    t = _norm_text(user_text)
    if not t:
        return False

    assistant_identity_patterns = [
        r"\bare you gay\b",
        r"\bare u gay\b",
        r"\bare you queer\b",
        r"\bare u queer\b",
        r"\bso you are gay\b",
        r"\bso you are queer\b",
        r"\bare you straight\b",
        r"\bare you lgbt\b",
        r"\bare you lesbian\b",
        r"\bare you bisexual\b",
        r"\bare you trans\b",
        r"\bwhat gender are you\b",
        r"\bare you male\b",
        r"\bare you female\b",
        r"\bare you married\b",
        r"\bdo you have a boyfriend\b",
        r"\bdo you have a girlfriend\b",
        r"\bdo you have a husband\b",
        r"\bdo you have a wife\b",
    ]

    user_disclosure_patterns = [
        r"\bi am gay\b",
        r"\bi'm gay\b",
        r"\bim gay\b",
        r"\bi am queer\b",
        r"\bi'm queer\b",
        r"\bim queer\b",
        r"\bi am lesbian\b",
        r"\bi'm lesbian\b",
        r"\bim lesbian\b",
        r"\bi am bisexual\b",
        r"\bi'm bisexual\b",
        r"\bim bisexual\b",
        r"\bi am trans\b",
        r"\bi'm trans\b",
        r"\bim trans\b",
    ]

    care_concern_patterns = [
        r"\bdo you accept gay patients\b",
        r"\bdo you take gay patients\b",
        r"\bcan gay patients come here\b",
        r"\bwill the office treat gay patients\b",
        r"\bwill the dentist treat gay patients\b",
    ]

    all_patterns = assistant_identity_patterns + user_disclosure_patterns + care_concern_patterns
    return any(re.search(p, t) for p in all_patterns)


def build_personal_identity_or_relationship_reply(user_text: str, office_phone: str) -> str:
    t = _norm_text(user_text)
    phone = (office_phone or "").strip() or "(555) 123-4567"

    assistant_question_terms = [
        "are you gay",
        "are u gay",
        "are you queer",
        "are u queer",
        "so you are gay",
        "so you are queer",
        "are you straight",
        "are you lgbt",
        "are you lesbian",
        "are you bisexual",
        "are you trans",
        "what gender are you",
        "are you male",
        "are you female",
        "are you married",
        "do you have a boyfriend",
        "do you have a girlfriend",
        "do you have a husband",
        "do you have a wife",
    ]

    care_concern_terms = [
        "accept gay patients",
        "take gay patients",
        "gay patients come here",
        "treat gay patients",
    ]

    if any(term in t for term in assistant_question_terms):
        return (
            "I’m Mia, the office’s AI receptionist, so I don’t have personal identity or relationships. "
            "I can help with appointments, services, insurance, hours, or location."
        )

    if any(term in t for term in care_concern_terms):
        return (
            "I can help with appointment requests and general office information. "
            f"If you have a specific concern you want the office team to address directly, please call the office at {phone}."
        )

    return (
        "Thanks for sharing. I’m here to help with dental appointments, services, insurance, hours, or location. "
        "How can I help?"
    )

def looks_like_dangerous_dental_instruction(user_text: str) -> bool:
    t = _norm_text(user_text)
    dangerous_action = any(p in t for p in [
        "pull out",
        "pull my tooth",
        "pull a tooth",
        "take out my tooth",
        "extract my tooth",
        "walk me through",
        "pliers",
        "yank",
        "rip out",
        "use glue",
        "super glue",
        "glue it",
        "glue my tooth",
        "floss string",
        "with floss",
        "use floss to pull",
        "paper towel",
        "baby wipe",
        "baby wipes",
    ])
    dental_context = any(p in t for p in ["tooth", "teeth", "bleeding", "gum", "gums", "mouth"])
    return dangerous_action and dental_context

def build_dangerous_dental_self_treatment_reply(user_text: str) -> str:
    """
    Safety reply for dangerous DIY dental questions.
    Keep the wording specific to what the patient actually mentioned,
    while avoiding step-by-step medical or home-care instructions.
    """
    t = _norm_text(user_text)

    base = (
        "I can’t provide medical advice or home-care instructions in chat. "
    )

    if any(p in t for p in ["pull out", "pull my tooth", "pull a tooth", "take out my tooth", "extract my tooth", "yank", "rip out", "pliers"]):
        return (
            f"{base}"
            "A tooth extraction should be handled by a dental professional and should not be attempted at home."
        )

    if any(p in t for p in ["use glue", "super glue", "glue it", "glue my tooth"]):
        return (
            f"{base}"
            "Household glue or adhesives are not appropriate for dental treatment. "
            "A dental professional should evaluate it."
        )

    if any(p in t for p in ["floss string", "with floss", "use floss to pull"]):
        return (
            f"{base}"
            "Trying to remove a tooth with floss or string should be handled by a dental professional, not at home."
        )

    if any(p in t for p in ["paper towel", "baby wipe", "baby wipes"]):
        return (
            f"{base}"
            "This should be evaluated by a dental professional rather than handled with household items."
        )

    return (
        f"{base}"
        "This should be evaluated by a dental professional."
    )

DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}


def get_office_hours_struct(client) -> dict:
    raw = getattr(client, "office_hours", None)
    if isinstance(raw, dict):
        return raw
    return {}


def is_day_open(client, day_key: str) -> bool:
    hours = get_office_hours_struct(client)
    day = hours.get(day_key, {})
    return bool(day.get("open", False))


def get_open_day_keys(client) -> list[str]:
    return [d for d in DAY_ORDER if is_day_open(client, d)]


def get_weekday_open_day_keys(client) -> list[str]:
    return [d for d in ["mon", "tue", "wed", "thu", "fri"] if is_day_open(client, d)]


def get_available_example_days(client, prefer_weekdays: bool = True) -> list[str]:
    days = get_weekday_open_day_keys(client) if prefer_weekdays else get_open_day_keys(client)
    return [DAY_LABELS[d] for d in days[:3]]


DAY_LABELS_FULL = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}
DAY_LABELS_SHORT = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}

def looks_like_priority_time_request(text: str) -> bool:
    t = _norm_text(text)

    strong_phrases = [
        "asap",
        "as soon as possible",
        "as soon as you can",
        "earliest available",
        "earliest opening",
        "next available",
        "right away",
        "soon as possible",
        "soonest",
    ]

    if any(p in t for p in strong_phrases):
        return True

    if "today" in t and looks_like_scheduling_intent(text):
        return True

    return False

def _format_time_label(hhmm: Optional[str]) -> str:
    if not hhmm:
        return ""
    try:
        hh, mm = hhmm.split(":")
        h = int(hh)
        m = int(mm)
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12
        if h12 == 0:
            h12 = 12
        if m == 0:
            return f"{h12} {suffix}"
        return f"{h12}:{m:02d} {suffix}"
    except Exception:
        return hhmm


def build_office_hours_answer(client) -> Optional[str]:
    hours = get_office_hours_struct(client)
    if not hours:
        return None

    lines = ["Office hours:"]
    has_any_day = False

    for day in DAY_ORDER:
        row = hours.get(day, {}) or {}
        is_open = bool(row.get("open", False))
        start = row.get("start")
        end = row.get("end")

        if is_open and start and end:
            label = f"{_format_time_label(start)} – {_format_time_label(end)}"
            has_any_day = True
        else:
            label = "Closed"

        lines.append(f"{DAY_LABELS_FULL[day]}: {label}")

    if not has_any_day:
        return None

    return "\n".join(lines)


def detect_specific_hours_day(user_text: str) -> Optional[str]:
    """Detect questions about a specific day's office hours."""
    t = _norm_text(user_text)
    if not t:
        return None

    # Only answer direct hours/open/closed questions here.
    # Appointment requests like "I want Sunday" should stay in scheduling flow.
    hours_words = [
        "open",
        "closed",
        "close",
        "closing",
        "hours",
        "what time",
        "available on",
        "are you open",
        "do you open",
    ]
    if not any(p in t for p in hours_words):
        return None

    day_map = {
        "monday": "mon",
        "mon": "mon",
        "tuesday": "tue",
        "tues": "tue",
        "tue": "tue",
        "wednesday": "wed",
        "wed": "wed",
        "thursday": "thu",
        "thurs": "thu",
        "thur": "thu",
        "thu": "thu",
        "friday": "fri",
        "fri": "fri",
        "saturday": "sat",
        "sat": "sat",
        "sunday": "sun",
        "sun": "sun",
    }

    for token, day_key in day_map.items():
        if re.search(rf"\b{re.escape(token)}\b", t):
            return day_key

    return None


def build_specific_day_hours_answer(client, day_key: str) -> Optional[str]:
    """Build direct answer for questions like 'Are you open on Sunday?'."""
    hours = get_office_hours_struct(client)
    if not hours:
        return None

    row = hours.get(day_key, {}) or {}
    day_name = DAY_LABELS_FULL.get(day_key, day_key.title())

    is_open = bool(row.get("open", False))
    start = row.get("start")
    end = row.get("end")

    if not is_open or not start or not end:
        return f"The office is closed on {day_name}."

    return f"Yes — the office is open on {day_name} from {_format_time_label(start)} to {_format_time_label(end)}."

def looks_like_general_hours_request(user_text: str) -> bool:
    """Detect general office-hours questions without handling specific-day questions here."""
    t = _norm_text(user_text)
    if not t:
        return False

    # Specific day questions are handled by detect_specific_hours_day().
    if detect_specific_hours_day(user_text):
        return False

    phrases = [
        "hours",
        "office hours",
        "business hours",
        "what are your hours",
        "when are you open",
        "when do you open",
        "when do you close",
        "what time do you open",
        "what time do you close",
    ]

    return any(p in t for p in phrases)

def _parse_hhmm_to_minutes(hhmm: Optional[str]) -> Optional[int]:
    if not hhmm or ":" not in hhmm:
        return None
    try:
        hh, mm = hhmm.split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return None


def _extract_exact_time_minutes_from_tw(tw: Optional[str]) -> Optional[int]:
    if not tw:
        return None

    tl = (tw or "").lower().strip()

    m12 = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", tl)
    if m12:
        hh = int(m12.group(1))
        mm = int(m12.group(2) or "0")
        ap = m12.group(3)

        if ap == "am":
            if hh == 12:
                hh = 0
        else:
            if hh != 12:
                hh += 12

        return hh * 60 + mm

    m24 = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", tl)
    if m24:
        hh = int(m24.group(1))
        mm = int(m24.group(2))
        return hh * 60 + mm

    # Bare hour after a day token, like "Tue 1" or "Mon 4:45".
    # In a dental-office context, 1–7 usually means PM, not early morning.
    m_bare_day_time = re.search(
        r"\b(?:mon|tue|wed|thu|fri|sat|sun)\s+(\d{1,2})(?::(\d{2}))?\b",
        tl,
    )
    if m_bare_day_time:
        hh = int(m_bare_day_time.group(1))
        mm = int(m_bare_day_time.group(2) or "0")

        if 1 <= hh <= 7:
            hh += 12
        elif hh == 12:
            hh = 12

        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh * 60 + mm

    if "morning" in tl:
        return 9 * 60
    if "afternoon" in tl:
        return 15 * 60
    if "evening" in tl or "night" in tl:
        return 18 * 60

    return None

def extract_bare_office_hour_time_window(client: Client, user_text: str) -> Optional[str]:
    """
    Convert bare dental-office times into explicit AM/PM when office hours make it obvious.

    Examples:
    - "wed 9" -> "wed 9am" if 9 AM is open and 9 PM is closed
    - "monday 3" -> "monday 3pm" if 3 PM is open and 3 AM is closed
    - "today at 1" -> "today 1pm" if 1 PM is open and 1 AM is closed

    Explicit AM/PM like "today at 1am" is not changed.
    """
    raw = (user_text or "").strip()
    t = _norm_text(raw)
    if not t:
        return None

    # Do not override explicit AM/PM.
    if re.search(r"\b(am|pm|a\.m\.|p\.m\.)\b", t):
        return None

    day_pattern = (
        r"(?P<day>"
        r"today|tomorrow|"
        r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
        r"fri(?:day)?|sat(?:urday)?|sun(?:day)?"
        r")"
    )

    m = re.search(
        rf"\b{day_pattern}\s*(?:at\s*)?(?P<hour>\d{{1,2}})(?P<minute>:\d{{2}})?\b",
        t,
    )
    if not m:
        return None

    day_raw = m.group("day")
    hour = int(m.group("hour"))
    minute_text = m.group("minute") or ""
    minute = int(minute_text.replace(":", "") or "0")

    if hour < 1 or hour > 12 or minute < 0 or minute > 59:
        return None

    def _loose_day_key(day_text: str) -> Optional[str]:
        day_text = (day_text or "").lower().strip()
        today_key = get_client_now(client).strftime("%a").lower()[:3]
        order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

        if day_text == "today":
            return today_key

        if day_text == "tomorrow":
            try:
                return order[(order.index(today_key) + 1) % 7]
            except ValueError:
                return None

        if day_text.startswith("mon"):
            return "mon"
        if day_text.startswith("tue"):
            return "tue"
        if day_text.startswith("wed"):
            return "wed"
        if day_text.startswith("thu"):
            return "thu"
        if day_text.startswith("fri"):
            return "fri"
        if day_text.startswith("sat"):
            return "sat"
        if day_text.startswith("sun"):
            return "sun"

        return None

    day_key = _loose_day_key(day_raw)
    if not day_key:
        return None

    hours = get_office_hours_struct(client)
    row = hours.get(day_key, {}) or {}

    if not bool(row.get("open", False)):
        return None

    start_minutes = _parse_hhmm_to_minutes(row.get("start"))
    end_minutes = _parse_hhmm_to_minutes(row.get("end"))

    if start_minutes is None or end_minutes is None:
        return None

    am_hour = 0 if hour == 12 else hour
    pm_hour = 12 if hour == 12 else hour + 12

    am_minutes = am_hour * 60 + minute
    pm_minutes = pm_hour * 60 + minute

    am_is_open = start_minutes <= am_minutes < end_minutes
    pm_is_open = start_minutes <= pm_minutes < end_minutes

    suffix = None

    if am_is_open and not pm_is_open:
        suffix = "am"
    elif pm_is_open and not am_is_open:
        suffix = "pm"
    elif am_is_open and pm_is_open:
        # Rare, but if both are valid, prefer AM for bare "9" style dental-office requests.
        suffix = "am"
    else:
        return None

    minute_part = minute_text if minute_text else ""
    return f"{day_raw} {hour}{minute_part}{suffix}"

def canonicalize_time_window_for_storage(client: Client, user_text: str) -> Optional[str]:
    """
    Return the best saved version of a user's time preference.
    This makes sure inputs like 'wed 9' are saved as 'wed 9am'
    when office hours make that obvious.
    """
    bare_tw = extract_bare_office_hour_time_window(client, user_text)
    if bare_tw:
        return bare_tw

    detected_tw = detect_time_window(user_text, client)
    if detected_tw:
        return detected_tw

    return None

def _minutes_to_time_token(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    if m == 0:
        return f"{h12}{suffix}"
    return f"{h12}:{m:02d}{suffix}"


def _resolve_ambiguous_time_label_for_client(
    client: Optional[Client],
    day_key: Optional[str],
    time_label: Optional[str],
) -> Optional[str]:
    """
    If user says '4:45' without AM/PM, choose the version that makes sense
    for the dental office's hours. Example: 4:45 -> 4:45pm, not 4:45am.
    """
    if not time_label:
        return time_label

    tl = (time_label or "").strip().lower()

    # Already explicit.
    if "am" in tl or "pm" in tl:
        return time_label

    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", tl)
    if not m:
        return time_label

    hour = int(m.group(1))
    minute = int(m.group(2))

    # 13:00–23:59 is already 24-hour time.
    if hour == 0 or hour >= 13:
        return time_label

    # 12:30 naturally means noon-ish in this context.
    if hour == 12:
        return time_label

    am_minutes = hour * 60 + minute
    pm_minutes = (hour + 12) * 60 + minute

    hours = get_office_hours_struct(client) if client is not None else {}
    row = hours.get(day_key, {}) if day_key else {}

    if bool(row.get("open", False)):
        start_minutes = _parse_hhmm_to_minutes(row.get("start"))
        end_minutes = _parse_hhmm_to_minutes(row.get("end"))

        if start_minutes is not None and end_minutes is not None:
            am_fits = start_minutes <= am_minutes < end_minutes
            pm_fits = start_minutes <= pm_minutes < end_minutes

            if pm_fits and not am_fits:
                return _minutes_to_time_token(pm_minutes)

            if am_fits and not pm_fits:
                return _minutes_to_time_token(am_minutes)

    # Fallback dental-office logic:
    # 1–7 without AM/PM is usually afternoon/evening, not early morning.
    if 1 <= hour <= 7:
        return _minutes_to_time_token(pm_minutes)

    return time_label


def _get_day_key_from_time_window(tw: Optional[str]) -> Optional[str]:
    if not tw:
        return None
    m = re.search(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", tw)
    if not m:
        return None

    token = m.group(1).lower()
    mapping = {
        "mon": "mon",
        "tue": "tue",
        "wed": "wed",
        "thu": "thu",
        "fri": "fri",
        "sat": "sat",
        "sun": "sun",
    }
    return mapping.get(token)


def check_outside_hours(client: Client, time_window: Optional[str]) -> Tuple[bool, Optional[str]]:
    if not time_window:
        return (False, None)

    day_key = _get_day_key_from_time_window(time_window)
    if not day_key:
        return (False, None)

    hours = get_office_hours_struct(client)
    row = hours.get(day_key, {}) or {}

    is_open = bool(row.get("open", False))
    start = row.get("start")
    end = row.get("end")

    pretty_tw = pretty_time_window(time_window)

    if not is_open:
        day_name = DAY_LABELS_FULL.get(day_key, day_key.title())
        return (True, f"Requested {pretty_tw}, but the office is closed on {day_name}.")

    req_minutes = _extract_exact_time_minutes_from_tw(time_window)
    start_minutes = _parse_hhmm_to_minutes(start)
    end_minutes = _parse_hhmm_to_minutes(end)

    if req_minutes is None or start_minutes is None or end_minutes is None:
        return (False, None)

    if req_minutes < start_minutes or req_minutes >= end_minutes:
        return (
            True,
            f"Requested {pretty_tw}, outside normal hours ({_format_time_label(start)}–{_format_time_label(end)})."
        )

    return (False, None)

def build_time_window_issue_reply(client: Client, time_window: Optional[str]) -> Optional[str]:
    """
    Detect when a requested exact time is outside office hours or already in the past today.
    Outside-hours comes first so 1 AM / 3 AM does not get described as a normal past appointment time.
    """
    if not time_window:
        return None

    day_key = _get_day_key_from_time_window(time_window)
    if not day_key:
        return None

    req_minutes = _extract_exact_time_minutes_from_tw(time_window)
    if req_minutes is None:
        return None

    hours = get_office_hours_struct(client)
    row = hours.get(day_key, {}) or {}

    is_open = bool(row.get("open", False))
    start_minutes = _parse_hhmm_to_minutes(row.get("start"))
    end_minutes = _parse_hhmm_to_minutes(row.get("end"))

    if not is_open:
        now_local = get_client_now(client)
        today_key = now_local.strftime("%a").lower()[:3]

        if day_key == today_key:
            return "The office is closed today. What day/time works better for you?"

        day_name = DAY_LABELS_FULL.get(day_key, day_key.title())
        return f"The office is closed on {day_name}. What day/time works better for you?"

    if start_minutes is not None and end_minutes is not None:
        if req_minutes < start_minutes or req_minutes >= end_minutes:
            return "That time is outside normal office hours. What day/time works better for you?"

    now_local = get_client_now(client)
    today_key = now_local.strftime("%a").lower()[:3]
    now_minutes = now_local.hour * 60 + now_local.minute

    if day_key == today_key and req_minutes <= now_minutes:
        return "That time has already passed today. What later time today or another day works better for you?"

    return None

def _extract_time_only_minutes_from_user_text(user_text: str) -> Optional[int]:
    """
    Detect explicit AM/PM times with no day attached.
    Example: "appointment at 3am" -> 180.
    Does not handle bare "3" because that is ambiguous.
    """
    t = _norm_text(user_text)
    if not t:
        return None

    # If a day is present, let normal day/time logic handle it.
    if re.search(r"\b(today|tomorrow|mon|monday|tue|tuesday|wed|wednesday|thu|thursday|fri|friday|sat|saturday|sun|sunday)\b", t):
        return None

    m = re.search(r"\b(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b", t)
    if not m:
        return None

    hour = int(m.group(1))
    minute = int(m.group(2) or "0")
    suffix = m.group(3).replace(".", "").lower()

    if hour < 1 or hour > 12 or minute < 0 or minute > 59:
        return None

    if suffix == "am":
        hour_24 = 0 if hour == 12 else hour
    else:
        hour_24 = 12 if hour == 12 else hour + 12

    return hour_24 * 60 + minute


def time_only_request_is_outside_office_hours(client: Client, user_text: str) -> bool:
    """
    If the user gives only an exact AM/PM time and that time is outside
    all normal open office windows, do not start intake.
    """
    req_minutes = _extract_time_only_minutes_from_user_text(user_text)
    if req_minutes is None:
        return False

    hours = get_office_hours_struct(client)
    saw_open_day = False

    for _day_key, row in (hours or {}).items():
        row = row or {}
        if not bool(row.get("open", False)):
            continue

        start_minutes = _parse_hhmm_to_minutes(row.get("start"))
        end_minutes = _parse_hhmm_to_minutes(row.get("end"))

        if start_minutes is None or end_minutes is None:
            continue

        saw_open_day = True

        if start_minutes <= req_minutes < end_minutes:
            return False

    return saw_open_day

def build_hours_hint_text(client) -> Optional[str]:
    hours = get_office_hours_struct(client)
    if not hours:
        return None

    open_days = []
    for day in DAY_ORDER:
        row = hours.get(day, {}) or {}
        if bool(row.get("open", False)) and row.get("start") and row.get("end"):
            open_days.append(
                f"{DAY_LABELS_SHORT[day]} {_format_time_label(row.get('start'))}–{_format_time_label(row.get('end'))}"
            )

    if not open_days:
        return None

    return "Office hours: " + ", ".join(open_days) + "."





def build_time_window_examples(client, prefer_weekdays: bool = True) -> str:
    days = get_available_example_days(client, prefer_weekdays=prefer_weekdays)

    if not days:
        return "For example: Tuesday morning."

    if len(days) == 1:
        return f"For example: {days[0]} morning."

    return f"For example: {days[0]} morning or {days[1]} afternoon."


def is_sunday_closed(client) -> bool:
    row = get_office_hours_struct(client).get("sun", {}) or {}
    return not bool(row.get("open", False))


def is_saturday_open(client) -> bool:
    row = get_office_hours_struct(client).get("sat", {}) or {}
    return bool(row.get("open", False))


def pretty_time_window(tw: Optional[str]) -> str:
    if not tw:
        return ""

    tl = (tw or "").strip().lower()
    parts = tl.split()

    if not parts:
        return tw

    day_token = parts[0]  # e.g. "thu"
    rest = " ".join(parts[1:])  # e.g. "morning"

    today_dt = datetime.now()
    today_str = today_dt.strftime("%a").lower()
    tomorrow_dt = today_dt + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%a").lower()

    # Helper to format date like "Mar 18"
    def fmt(dt):
        return dt.strftime("%b %d").replace(" 0", " ")

    # Today
    if day_token == today_str:
        label = f"Today ({fmt(today_dt)})"

    # Tomorrow
    elif day_token == tomorrow_str:
        label = f"Tomorrow ({fmt(tomorrow_dt)})"

    # Future weekday (find next occurrence)
    else:
        try:
            target_idx = ["mon","tue","wed","thu","fri","sat","sun"].index(day_token)
            current_idx = today_dt.weekday()

            days_ahead = (target_idx - current_idx + 7) % 7
            if days_ahead == 0:
                days_ahead = 7  # next week

            target_dt = today_dt + timedelta(days=days_ahead)
            label = f"{day_token.capitalize()} ({fmt(target_dt)})"
        except Exception:
            return tw  # fallback if something unexpected

    if rest:
        return f"{label} {rest}"

    return label
    
# =========================================================
# Deterministic extractors (email/phone/name/reason)
# =========================================================
def extract_email(text_in: str) -> Optional[str]:
    match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text_in or "")
    return match.group(0) if match else None

COMMON_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "aol.com",
    "icloud.com",
    "msn.com",
    "live.com",
}


def email_domain_typo_suspected(email: str) -> bool:
    e = (email or "").strip().lower()

    # Invalid email format
    if not re.fullmatch(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        e
    ):
        return True

    domain = e.split("@", 1)[1]

    # Obvious malformed domains
    if ".." in domain:
        return True

    # Exact match to common providers
    if domain in COMMON_EMAIL_DOMAINS:
        return False

    # Looks very similar to a common provider
    close = get_close_matches(
        domain,
        COMMON_EMAIL_DOMAINS,
        n=1,
        cutoff=0.78
    )

    return bool(close)


def extract_phone(text_in: str) -> Optional[str]:
    digits = re.sub(r"\D", "", text_in or "")
    if len(digits) == 10:
        return digits
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return None


def is_valid_phone(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone or "")

    if len(digits) != 10:
        return False

    if digits[0] in {"0", "1"}:
        return False

    if len(set(digits)) <= 2:
        return False

    return True

def extract_name_from_name_phone_reply(text_in: str) -> Optional[str]:
    raw = (text_in or "").strip()
    if not raw:
        return None

    # remove the phone-like part first
    without_phone = re.sub(r"\+?1?\s*\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", " ", raw)
    without_phone = re.sub(r"\s+", " ", without_phone).strip()

    if not without_phone:
        return None

    # reuse your safe name normalizer
    return safe_name_normalize(without_phone)


def extract_name(text_in: str) -> Optional[str]:
    t = (text_in or "").strip()
    tl = t.lower()

    if any(p in tl for p in [
        "i am interested in",
        "i'm interested in",
        "im interested in",
        "i am looking for",
        "i'm looking for",
        "i need",
        "i want",
    ]):
        return None
    
    patterns = [
        r"\bmy name is\s+([A-Za-z][A-Za-z'-]{1,30})(?:\s+([A-Za-z][A-Za-z'-]{1,30}))?\b",
        r"\bi am\s+([A-Za-z][A-Za-z'-]{1,30})(?:\s+([A-Za-z][A-Za-z'-]{1,30}))?\b",
        r"\bi'm\s+([A-Za-z][A-Za-z'-]{1,30})(?:\s+([A-Za-z][A-Za-z'-]{1,30}))?\b",
        r"\bthis is\s+([A-Za-z][A-Za-z'-]{1,30})(?:\s+([A-Za-z][A-Za-z'-]{1,30}))?\b",
    ]
    for pat in patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            first = (m.group(1) or "").strip()
            last = (m.group(2) or "").strip()
            name = f"{first} {last}".strip()
            if len(name) >= 2:
                return " ".join(w.capitalize() for w in name.split())
    return None


def detect_appointment_reason(text_in: str) -> Optional[str]:
    t = (text_in or "").lower()
    if any(k in t for k in ["cleaning", "checkup", "check-up", "routine", "exam"]):
        return "cleaning/checkup"
    if any(k in t for k in ["toothache", "tooth ache", "pain", "hurt", "swelling"]):
        return "tooth pain"
    if any(k in t for k in [
    "broken",
    "broke",
    "broke a tooth",
    "broke tooth",
    "broken tooth",
    "cracked",
    "cracked tooth",
    "chipped",
    "chipped tooth",
    "fell out",
    "filling fell out",
    "lost filling",
    "broke a filling",
    "broke filling",
    "broken filling",]): 
        return "broken tooth/filling"
    if any(k in t for k in ["crown", "crowns", "cap", "caps"]):
        return "crown"
    if any(k in t for k in ["braces", "invisalign", "orthodont", "straighten"]):
        return "orthodontics"
    if any(k in t for k in ["whiten", "whitening", "cosmetic", "veneers"]):
        return "cosmetic/whitening"
    if any(k in t for k in ["implant", "extraction", "wisdom tooth", "wisdom teeth"]):
        return "extraction/implant"
    if any(k in t for k in ["appointment", "schedule", "book", "availability", "available"]):
        return "appointment request"
    return None

def looks_like_safe_reason_detail(text_in: str) -> bool:
    t = (text_in or "").strip()
    if not t:
        return False

    # Keep it short so people cannot dump long payloads
    if len(t) > 120:
        return False

    tl = t.lower()

    # Block obvious code / script / payload patterns
    blocked_patterns = [
        "<script",
        "</script",
        "javascript:",
        "onerror=",
        "onclick=",
        "drop table",
        "union select",
        "select * from",
        "--",
        "/*",
        "*/",
        "<?php",
        "console.log",
        "document.cookie",
        "window.location",
    ]
    if any(p in tl for p in blocked_patterns):
        return False

    # Allow mostly normal text characters only
    if re.search(r"[^a-zA-Z0-9\s\-\?',./()]", t):
        return False

    return True


def map_reason_detail_to_enum(text_in: str) -> Optional[str]:
    t = (text_in or "").strip()
    if not looks_like_safe_reason_detail(t):
        return None

    # Reuse your existing deterministic mapper first
    mapped = detect_appointment_reason(t)
    if mapped:
        return mapped

    tl = t.lower()

    # Extra fallback phrases for free-text "other"
    if any(k in tl for k in ["chip", "chipped", "crack", "cracked", "broken tooth", "lost filling", "filling fell out"]):
        return "broken tooth/filling"

    if any(k in tl for k in ["hurt", "hurts", "ache", "toothache", "pain", "sore tooth", "sore gum"]):
        return "tooth pain"

    if any(k in tl for k in ["retainer", "aligner", "straighten", "crooked", "spacing"]):
        return "orthodontics"

    if any(k in tl for k in ["white", "whiter", "whitening", "bleaching", "cosmetic"]):
        return "cosmetic/whitening"

    if any(k in tl for k in ["pull tooth", "remove tooth", "wisdom tooth", "implant", "extraction"]):
        return "extraction/implant"

    if any(k in tl for k in ["cap", "crown"]):
        return "crown"

    # If it still looks safe but doesn't map cleanly, use generic appointment request
    return "appointment request"
# =========================================================
# Service selection detector (UI buttons / short replies)
# =========================================================
def detect_service_selection(user_text: str) -> Optional[str]:
    t = (user_text or "").strip().lower()
    if "tooth pain" in t or "tooth hurts" in t or "toothache" in t:
        return "tooth pain"

    if "cleaning" in t or "checkup" in t or "check-up" in t:
        return "cleaning/checkup"

    if "broken tooth" in t or "broke a tooth" in t or "filling" in t:
        return "broken tooth/filling"

    if "implant" in t or "extraction" in t:
        return "extraction/implant"

    if "whitening" in t or "cosmetic" in t or "veneer" in t or "veneers" in t:
        return "cosmetic/whitening"

    if "braces" in t or "invisalign" in t:
        return "orthodontics"
    service_map = {
        "cleaning": "cleaning/checkup",
        "checkup": "cleaning/checkup",
        "exam": "cleaning/checkup",
        "filling": "broken tooth/filling",
        "fillings": "broken tooth/filling",
        "cavity": "broken tooth/filling",
        "cavities": "broken tooth/filling",
        "broken tooth": "broken tooth/filling",
        "broke tooth": "broken tooth/filling",
        "broke a tooth": "broken tooth/filling",
        "broken filling": "broken tooth/filling",
        "broke filling": "broken tooth/filling",
        "broke a filling": "broken tooth/filling",
        "lost filling": "broken tooth/filling",
        "filling fell out": "broken tooth/filling",
        "chipped tooth": "broken tooth/filling",
        "cracked tooth": "broken tooth/filling",
        "crown": "crown",
        "crowns": "crown",
        "cap": "crown",
        "caps": "crown",
        "implant": "extraction/implant",
        "implants": "extraction/implant",
        "extraction": "extraction/implant",
        "extractions": "extraction/implant",
        "wisdom tooth": "extraction/implant",
        "wisdom teeth": "extraction/implant",
        "braces": "orthodontics",
        "invisalign": "orthodontics",
        "whitening": "cosmetic/whitening",
        "teeth whitening": "cosmetic/whitening",
        "appointment request": "appointment request",
        "other": "other",
    }
    return service_map.get(t)

def pretty_lead_reason(reason: Optional[str]) -> str:
    mapping = {
        "cleaning/checkup": "Cleaning / Checkup",
        "tooth pain": "Tooth Pain",
        "broken tooth/filling": "Broken Tooth / Filling",
        "crown": "Crown",
        "orthodontics": "Braces / Invisalign",
        "cosmetic/whitening": "Cosmetic / Whitening",
        "extraction/implant": "Extraction / Implant",
        "appointment request": "Appointment Request",
    }
    return mapping.get((reason or "").strip(), (reason or "").strip() or "Not provided")


def _explicit_urgency_requested(text: str) -> bool:
    t = _norm_text(text)
    urgent_terms = [
        "asap",
        "as soon as possible",
        "earliest",
        "earliest available",
        "next available",
        "right away",
        "soon as possible",
        "soonest",
        "immediately",
    ]
    return any(term in t for term in urgent_terms)


def lead_is_same_day_without_explicit_urgency(conversation: Conversation) -> bool:
    """
    Same-day appointment requests should be treated as priority,
    but staff notifications should not display them as ASAP unless the user actually said ASAP/urgent.
    """
    source_text = getattr(conversation, "lead_reason_source_text", "") or ""
    time_window = getattr(conversation, "lead_time_window", "") or ""

    source_norm = _norm_text(source_text)
    time_norm = _norm_text(time_window)

    mentioned_today = "today" in source_norm or "today" in time_norm
    if not mentioned_today:
        return False

    if _explicit_urgency_requested(source_text):
        return False

    if bool(getattr(conversation, "lead_is_emergency", False)):
        return False

    if conversation_has_symptom_or_safety_reason(conversation):
        return False

    return True


def pretty_staff_time_window(conversation: Conversation) -> str:
    tw = (getattr(conversation, "lead_time_window", None) or "").strip()
    if not tw:
        return ""

    # If same-day logic internally stored this as ASAP, make the staff label more accurate.
    if tw.strip().lower() == "asap" and lead_is_same_day_without_explicit_urgency(conversation):
        return "Today / same-day request"

    return pretty_time_window(tw)

def build_staff_lead_summary(client: Client, conversation: Conversation) -> str:
    practice_name = getattr(client, "practice_name", None) or "Dental Office"

    if bool(getattr(conversation, "lead_is_emergency", False)):
        lines = [f"🚨 EMERGENCY lead for {practice_name}"]
    elif bool(getattr(conversation, "lead_is_priority", False)):
        lines = [f"🔥 PRIORITY lead for {practice_name}"]
    else:
        lines = [f"✅ New appointment request for {practice_name}"]

    if (conversation.lead_name or "").strip():
        lines.append(f"Name: {conversation.lead_name}")

    if (conversation.lead_phone or "").strip():
        lines.append(f"Phone: {conversation.lead_phone}")

    if (conversation.lead_email or "").strip():
        lines.append(f"Email: {conversation.lead_email}")
    elif bool(getattr(conversation, "lead_email_opt_out", False)):
        lines.append("Email: Not provided")

    if (conversation.lead_reason or "").strip():
        lines.append(f"Reason: {pretty_lead_reason(conversation.lead_reason)}")

    if (getattr(conversation, "lead_time_window", None) or "").strip():
        lines.append(f"Preferred time: {pretty_staff_time_window(conversation)}")

    if bool(getattr(conversation, "lead_is_outside_hours", False)):
        lines.append("Outside hours: Yes")

    if (getattr(conversation, "lead_outside_hours_note", None) or "").strip():
        lines.append(f"Outside-hours note: {conversation.lead_outside_hours_note}")

    np = getattr(conversation, "lead_is_new_patient", None)
    if np is True:
        lines.append("Patient type: New")
    elif np is False:
        lines.append("Patient type: Returning")

    return "\n".join(lines)

def build_staff_lead_sms(client: Client, conversation: Conversation) -> str:
    practice_name = getattr(client, "practice_name", None) or "Dental Office"

    if bool(getattr(conversation, "lead_is_emergency", False)):
        parts = [f"🚨 EMERGENCY lead for {practice_name}"]
    elif bool(getattr(conversation, "lead_is_priority", False)):
        parts = [f"🔥 PRIORITY lead for {practice_name}"]
    else:
        parts = [f"✅ New lead for {practice_name}"]

    if (conversation.lead_name or "").strip():
        parts.append(f"Name: {conversation.lead_name}")

    if (conversation.lead_phone or "").strip():
        parts.append(f"Phone: {conversation.lead_phone}")

    if (conversation.lead_reason or "").strip():
        parts.append(f"Reason: {pretty_lead_reason(conversation.lead_reason)}")

    if (getattr(conversation, "lead_time_window", None) or "").strip():
        parts.append(f"Time: {pretty_staff_time_window(conversation)}")

    if bool(getattr(conversation, "lead_is_outside_hours", False)):
        note = (getattr(conversation, "lead_outside_hours_note", None) or "").strip()
        if note:
            parts.append(f"Outside hours: {note}")
        else:
            parts.append("Outside hours")

    np = getattr(conversation, "lead_is_new_patient", None)
    if np is True:
        parts.append("New patient")
    elif np is False:
        parts.append("Returning patient")

    return " | ".join(parts)


def get_office_hours_hint(db: Session, client_id) -> Optional[str]:
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return None

    structured = build_hours_hint_text(client)
    if structured:
        return structured

    faq = (
        db.query(ClientFAQ)
        .filter(ClientFAQ.client_id == client_id, ClientFAQ.enabled == True)
        .filter(
            or_(
                ClientFAQ.question.ilike("%hours%"),
                ClientFAQ.keywords.ilike("%hours%"),
            )
        )
        .order_by(ClientFAQ.id.desc())
        .first()
    )
    if not faq:
        return None
    return (faq.answer or "").strip() or None

def send_office_lead_sms(to_phone: str, body: str) -> None:
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    from_phone = os.environ["TWILIO_FROM_PHONE"]

    twilio_client = TwilioClient(account_sid, auth_token)
    twilio_client.messages.create(
        body=body,
        from_=from_phone,
        to=to_phone,
    )


def send_office_lead_email(to_email: str, subject: str, body_text: str) -> None:
    resend.api_key = os.environ["RESEND_API_KEY"]

    params: resend.Emails.SendParams = {
        "from": os.environ["RESEND_FROM_EMAIL"],   # e.g. "Demo Dental <leads@yourdomain.com>"
        "to": [to_email],
        "subject": subject,
        "html": "<pre style='font-family:Arial,sans-serif;white-space:pre-wrap'>" + body_text + "</pre>",
    }

    resend.Emails.send(params)


def notify_office_of_completed_lead(db: Session, client: Client, conversation: Conversation) -> tuple[bool, bool, Optional[str], Optional[str]]:
    """
    Send internal office SMS/email once a lead is completed.
    This restores the notification trigger after the rebuilt chat flow.
    """
    staff_summary = build_staff_lead_summary(client, conversation)

    try:
        staff_sms = build_staff_lead_sms(client, conversation)
    except Exception:
        staff_sms = staff_summary

    office_notify_email = (getattr(client, "notification_email", None) or "").strip()
    office_notify_phone = (getattr(client, "notification_phone", None) or "").strip()

    print("[LEAD_NOTIFY_EMAIL]", office_notify_email)
    print("[LEAD_NOTIFY_PHONE]", office_notify_phone)
    print("[LEAD_SUMMARY]\n" + staff_summary)
    print("[LEAD_SMS]\n" + staff_sms)

    email_send_error = None
    sms_send_error = None

    email_sent = bool(getattr(conversation, "lead_email_sent", False))
    sms_sent = bool(getattr(conversation, "lead_sms_sent", False))

    subject_prefix = "New appointment request"
    if bool(getattr(conversation, "lead_is_emergency", False)):
        subject_prefix = "URGENT emergency lead"
    elif bool(getattr(conversation, "lead_is_priority", False)):
        subject_prefix = "Priority appointment request"

    practice_name = getattr(client, "practice_name", "Dental Office") or "Dental Office"

    try:
        if office_notify_email and not email_sent:
            send_office_lead_email(
                to_email=office_notify_email,
                subject=f"{subject_prefix} - {practice_name}",
                body_text=staff_summary,
            )
            conversation.lead_email_sent = True
            email_sent = True
            print("✅ LEAD EMAIL SENT")
    except Exception as e:
        email_send_error = str(e)
        print("[LEAD_EMAIL_ERROR]", email_send_error)

    try:
        if office_notify_phone and not sms_sent:
            send_office_lead_sms(
                to_phone=office_notify_phone,
                body=staff_sms,
            )
            conversation.lead_sms_sent = True
            sms_sent = True
            print("✅ LEAD SMS SENT")
    except Exception as e:
        sms_send_error = str(e)
        print("[LEAD_SMS_ERROR]", sms_send_error)

    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    return email_sent, sms_sent, email_send_error, sms_send_error


def lead_is_ready_for_office_notification(conversation: Conversation) -> bool:
    """
    A lead is notification-ready once Mia has at least a name and phone number.
    This is important for urgent/short flows and conversation-ending flows.
    """
    has_name = bool((getattr(conversation, "lead_name", None) or "").strip())
    has_phone = bool((getattr(conversation, "lead_phone", None) or "").strip())
    is_lead = bool(getattr(conversation, "is_lead", False))

    return is_lead and has_name and has_phone


def finalize_and_notify_if_ready(
    db: Session,
    client: Client,
    conversation: Conversation,
) -> tuple[bool, bool, Optional[str], Optional[str]]:
    """
    Final safety net: if the lead has enough info and has not notified the office yet,
    mark completed and send the internal office SMS/email.
    """
    if not lead_is_ready_for_office_notification(conversation):
        return (
            bool(getattr(conversation, "lead_email_sent", False)),
            bool(getattr(conversation, "lead_sms_sent", False)),
            None,
            None,
        )

    if (getattr(conversation, "lead_status", None) or "").strip() != "completed":
        conversation.lead_status = "completed"
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    if bool(getattr(conversation, "lead_email_sent", False)) and bool(getattr(conversation, "lead_sms_sent", False)):
        return True, True, None, None

    print("✅ FINALIZE AND NOTIFY SAFETY NET TRIGGERED")

    return notify_office_of_completed_lead(
        db,
        client,
        conversation,
    )

# =========================================================
# Week 3 fields (email opt-out + new/returning + time window)
# =========================================================
def detect_email_opt_out(user_text: str) -> bool:
    raw = (user_text or "").strip().lower()
    if not raw:
        return False

    t = re.sub(r"[^a-z0-9\s']", " ", raw)
    t = re.sub(r"\s+", " ", t).strip()

    exact_skip_replies = {
        "skip",
        "skp",
        "skip email",
        "no",
        "nope",
        "nah",
        "none",
        "no email",
        "no e mail",
    }

    if t in exact_skip_replies:
        return True

    email_phrases = [
        "dont have email",
        "don't have email",
        "do not have email",
        "no e-mail",
        "rather not give my email",
        "i dont want to give my email",
        "i don't want to give my email",
        "skip my email",
        "skip the email",
    ]

    return any(p in t for p in email_phrases)

def detect_new_patient_flag(user_text: str) -> Optional[bool]:
    t = (user_text or "").strip().lower()
    if not t:
        return None
    
    tl = re.sub(r"[^a-z0-9\s']", " ", t)
    tl = re.sub(r"\s+", " ", tl).strip()

    if tl in {"new", "new patient", "first time", "first-time"}:
        return True
    if tl in {
        "returning",
        "existing",
        "current",
        "returning patient",
        "existing patient",
        "current patient",
    }:
        return False
    

    # NEW: handle mixed short replies like "ok and new"
    if re.search(r"\bnew\b", tl) and not re.search(r"\breturning\b|\bexisting\b|\bcurrent\b", tl):
        return True

    if re.search(r"\b(returning|existing|current)\b", tl):
        return False

    new_patterns = [
        r"\bnew patient\b",
        r"\b(i'?m|im|i am)\s+new\b",
        r"\bfirst time\b",
        r"\bnever been\b",
        r"\bnew here\b",
        r"\bnew to (your|this) (office|clinic|practice)\b",
    ]
    returning_patterns = [
        r"\breturning patient\b",
        r"\bexisting patient\b",
        r"\bcurrent patient\b",
        r"\b(i'?m|im|i am)\s+(a\s+)?returning\b",
        r"\b(i'?m|im|i am)\s+(an?\s+)?existing\b",
        r"\b(i'?ve|ive)\s+been\b",
        r"\bbeen (here|there) before\b",
        r"\bbeen before\b",
    ]
    for pat in new_patterns:
        if re.search(pat, tl):
            return True
    for pat in returning_patterns:
        if re.search(pat, tl):
            return False
    return None


def detect_time_window(user_text: str, client: Optional[Client] = None) -> Optional[str]:
    t = (user_text or "").strip()
    if not t:
        return None
    tl = t.lower()

    # reusable time regex
    time_pattern = r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b([01]?\d|2[0-3]):([0-5]\d)\b"

    # relative day handling
    if re.search(r"\btoday\b", tl) or re.search(r"\btomorrow\b", tl):
        base = get_client_now(client) if client is not None else datetime.now()
        if re.search(r"\btomorrow\b", tl):
            base = base + timedelta(days=1)

        day = base.strftime("%a")  # Mon, Tue, Wed...

        # specific time FIRST
        time_match = re.search(time_pattern, tl)
        if time_match:
            if time_match.group(1):
                hh = int(time_match.group(1))
                mm = time_match.group(2) or "00"
                ap = time_match.group(3)
                time_label = f"{hh}:{mm}{ap}" if mm != "00" else f"{hh}{ap}"
            else:
                time_label = f"{time_match.group(4)}:{time_match.group(5)}"

            day_key_for_time = day.lower()[:3]
            time_label = _resolve_ambiguous_time_label_for_client(client, day_key_for_time, time_label)
            return f"{day} {time_label}"

        # part of day SECOND
        if re.search(r"\b(morning|morn)\b", tl):
            return f"{day} morning"
        if re.search(r"\b(afternoon|aft)\b", tl):
            return f"{day} afternoon"
        if re.search(r"\b(evening|eve|night)\b", tl):
            return f"{day} evening"

        return day

    day_map = {
        "mon": "Mon",
        "monday": "Mon",
        "tue": "Tue",
        "tues": "Tue",
        "tuesday": "Tue",
        "wed": "Wed",
        "wednesday": "Wed",
        "thu": "Thu",
        "thur": "Thu",
        "thurs": "Thu",
        "thursday": "Thu",
        "fri": "Fri",
        "friday": "Fri",
        "sat": "Sat",
        "saturday": "Sat",
        "sun": "Sun",
        "sunday": "Sun",
    }

    day = None
    for k, v in day_map.items():
        if re.search(rf"\b{k}\b", tl):
            day = v
            break

    # specific time FIRST
    time_match = re.search(time_pattern, tl)

    part = None
    if re.search(r"\b(morning|morn)\b", tl):
        part = "morning"
    elif re.search(r"\b(afternoon|aft)\b", tl):
        part = "afternoon"
    elif re.search(r"\b(evening|eve|night)\b", tl):
        part = "evening"

    if re.search(r"\bweekday\b", tl) and part in {"morning", "afternoon", "evening"}:
        return f"Weekday {part}"

    if not day and not time_match and not part:
        return None

    time_label = None
    if time_match:
        if time_match.group(1):
            hh = int(time_match.group(1))
            mm = time_match.group(2) or "00"
            ap = time_match.group(3)
            time_label = f"{hh}:{mm}{ap}" if mm != "00" else f"{hh}{ap}"
        else:
            time_label = f"{time_match.group(4)}:{time_match.group(5)}"

        day_key_for_time = day.lower()[:3] if day else None
        time_label = _resolve_ambiguous_time_label_for_client(client, day_key_for_time, time_label)

    detail = time_label or part
    if day and detail:
        return f"{day} {detail}"
    if day:
        return day
    return detail

def get_next_open_day_label(client, start_dt: datetime) -> Optional[str]:
    day_keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    labels = {
        "mon": "Monday",
        "tue": "Tuesday",
        "wed": "Wednesday",
        "thu": "Thursday",
        "fri": "Friday",
        "sat": "Saturday",
        "sun": "Sunday",
    }

    for i in range(1, 8):
        candidate = start_dt + timedelta(days=i)
        key = candidate.strftime("%a").lower()[:3]
        if is_day_open(client, key):
            return labels[key]

    return None

def time_window_has_specific_day(tw: Optional[str]) -> bool:
    if not tw:
        return False

    t = _norm_text(tw)

    return bool(
        re.search(
            r"\b(today|tomorrow|"
            r"mon|monday|"
            r"tue|tuesday|"
            r"wed|wednesday|"
            r"thu|thursday|"
            r"fri|friday|"
            r"sat|saturday|"
            r"sun|sunday)\b",
            t,
        )
    )


def looks_like_urgent_but_not_er(text: str) -> bool:
    t = _norm_text(text)
    urgency_words = [
        "asap",
        "as soon as possible",
        "earliest",
        "earliest available",
        "next available",
        "right away",
        "soon as possible",
        "immediately",
    ]
    symptom_words = [
        "tooth hurts",
        "tooth pain",
        "pain",
        "swelling",
        "broken tooth",
        "cracked tooth",
        "chipped tooth",
        "lost filling",
        "infection",
        "abscess",
    ]
    return any(u in t for u in urgency_words) and any(s in t for s in symptom_words)

def last_assistant_asked_asap_today_vs_tomorrow(db: Session, conversation_id: uuid.UUID) -> bool:
    last_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    if not last_msg:
        return False

    t = _norm_text(last_msg.content or "")
    return "if we can t fit you in today would tomorrow work" in t


def time_window_has_detail(tw: Optional[str]) -> bool:
    if not tw:
        return False

    tl = (tw or "").lower()

    if re.search(r"\b(morning|morn|am|a\.m\.)\b|\b(afternoon|aft|pm|p\.m\.)\b|\b(evening|eve|night)\b", tl):
        return True

    if re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", tl):
        return True

    if re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", tl):
        return True

    # Bare hour after a day token, like "Wed 9" or "Monday 3".
    # If a dental-office time can be inferred, treat it as specific enough.
    day_token = r"(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
    if re.search(rf"\b{day_token}\s+(?:at\s+)?\d{{1,2}}(?::\d{{2}})?\s*(am|pm)?\b", tl):
        return True

    return False


def time_window_is_complete(tw: Optional[str]) -> bool:
    if not tw:
        return False

    tl = (tw or "").strip().lower()
    if tl in {"asap", "asap / tomorrow ok"}:
        return True

    return bool(time_window_has_specific_day(tw) and time_window_has_detail(tw))


def looks_like_weekend_request(user_text: str) -> bool:
    t = _norm_text(user_text)
    return any(k in t for k in ["saturday", "sat", "sunday", "sun", "weekend"])


def _time_window_specificity_score(tw: Optional[str]) -> int:
    if not tw:
        return 0
    has_day = time_window_has_specific_day(tw)
    has_detail = time_window_has_detail(tw)
    tl = (tw or "").lower().strip()
    if tl in {"weekday morning", "weekday afternoon"}:
        return 1
    if has_day and not has_detail:
        return 2
    if has_day and has_detail:
        return 3
    if has_detail and not has_day:
        return 1
    return 0


def _extract_day_token(tw: str) -> Optional[str]:
    if not tw:
        return None
    m = re.search(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", tw)
    return m.group(1) if m else None


def handle_time_window_capture(
    client: Client,
    conversation: Conversation,
    user_text: str,
    last_assistant_text: str
) -> Tuple[Optional[str], bool]:
    current_tw = (getattr(conversation, "lead_time_window", None) or "").strip()
    detected_tw = canonicalize_time_window_for_storage(client, user_text)
    norm_user_text = _norm_text(user_text).strip()
    is_priority_time = looks_like_priority_time_request(user_text)

    now_local = get_client_now(client)
    is_after_noon = now_local.hour >= 12
    today_tok = now_local.strftime("%a")

    # convert exact "today" / "tomorrow" into weekday token
    if norm_user_text in {"today", "tomorrow"}:
        base = now_local
        if norm_user_text == "tomorrow":
            base = base + timedelta(days=1)
        detected_tw = base.strftime("%a")

    is_today_request = ("today" in norm_user_text) or (detected_tw == today_tok)

    time_issue_reply = build_time_window_issue_reply(client, detected_tw)
    if time_issue_reply:
        return (time_issue_reply, False)

    last_t = (last_assistant_text or "").lower()

    weekday_example = build_time_window_examples(client, prefer_weekdays=True)
    anyday_example = build_time_window_examples(client, prefer_weekdays=False)

    # ASAP / earliest availability handling
    if is_priority_time:
        conversation.lead_time_window = "ASAP"

        if not getattr(conversation, "lead_is_priority", False):
            conversation.lead_is_priority = True

        if not (conversation.lead_name or "").strip():
            return ("What’s your first name?", True)

        if not (conversation.lead_phone or "").strip():
            return (f"Thanks {conversation.lead_name}! What’s the best phone number to reach you?", True)

        if priority_intake_is_complete(conversation):
            return (build_priority_handoff_reply(conversation), True)

        next_open_day = get_next_open_day_label(client, now_local)

        if is_after_noon:
            if next_open_day:
                return (
                    f"Got it — we’ll look for the earliest available time. If we can’t fit you in today, would {next_open_day} work?",
                    True,
                )
            return ("Got it — we’ll look for the earliest available time.", True)

        if next_open_day:
            return (
                f"Got it — we’ll look for the earliest available time today. If needed, we can also look at {next_open_day}.",
                True,
            )

        return ("Got it — we’ll look for the earliest available time today.", True)

    # If we already have ASAP and user now gives a specific time preference,
    # replace ASAP with the more specific value.
    if current_tw in {"ASAP", "ASAP / tomorrow ok"} and detected_tw:
        conversation.lead_time_window = detected_tw
        return (None, True)

    # Sunday-only nudge
    if detected_tw:
        dtl = detected_tw.lower()
        if re.search(r"\b(sun|sunday)\b", dtl) and is_sunday_closed(client):
            return (
                "Just a heads up—we’re typically closed on Sundays. Do you prefer a weekday morning or afternoon?",
                False,
            )

    
    # Same-day scheduling rule
        
    if is_today_request:
        # If user already gave a specific time, accept it.
        if detected_tw and time_window_has_detail(detected_tw):
            conversation.lead_time_window = detected_tw
            return (None, True)

        # If user only said "today" / "tomorrow", save the day token first
        if detected_tw and time_window_has_specific_day(detected_tw) and not time_window_has_detail(detected_tw):
            current_score = _time_window_specificity_score(current_tw)
            new_score = _time_window_specificity_score(detected_tw)

            if new_score >= current_score:
                conversation.lead_time_window = detected_tw
                saved = True
            else:
                saved = False
        else:
            saved = False

        # If it's already afternoon, don't offer morning
        if is_after_noon:
            return (
                "Got it — what time later today works best? If today is too tight, tomorrow afternoon works too.",
                saved,
            )

        return ("Got it — do you prefer today morning or afternoon?", saved)

    # If we just nudged for weekday morning/afternoon, interpret answer here
    if "weekday morning or afternoon" in last_t:
        tl = _norm_text(user_text)

        if tl in {"no", "nope", "nah", "not really", "not weekday", "not weekdays"}:
            return (
                f"No problem — what day/time works better for you? {anyday_example}",
                False,
            )

        if tl in {"not morning", "not mornings", "afternoons only", "afternoon only", "only afternoon", "only afternoons"}:
            weekday_tw = "Weekday afternoon"
            if _time_window_specificity_score(weekday_tw) >= _time_window_specificity_score(current_tw):
                conversation.lead_time_window = weekday_tw
                return ("Thanks — which weekday works best (Mon–Fri)?", True)
            return ("Thanks — which weekday works best (Mon–Fri)?", False)

        if tl in {"not afternoon", "not afternoons", "mornings only", "morning only", "only morning", "only mornings"}:
            weekday_tw = "Weekday morning"
            if _time_window_specificity_score(weekday_tw) >= _time_window_specificity_score(current_tw):
                conversation.lead_time_window = weekday_tw
                return ("Thanks — which weekday works best (Mon–Fri)?", True)
            return ("Thanks — which weekday works best (Mon–Fri)?", False)

        if not detected_tw:
            return ("Got it — do you prefer weekday morning or afternoon?", False)

        dt = (detected_tw or "").strip()

        if re.search(r"\b(Sat|Sun)\b", dt):
            return ("Please choose a weekday (Mon–Fri). Do you prefer morning or afternoon?", False)

        if dt in {"morning", "afternoon"}:
            weekday_tw = f"Weekday {dt}"
            if _time_window_specificity_score(weekday_tw) >= _time_window_specificity_score(current_tw):
                conversation.lead_time_window = weekday_tw
                return ("Thanks — which weekday works best (Mon–Fri)?", True)
            return ("Thanks — which weekday works best (Mon–Fri)?", False)

        if dt in {"Weekday morning", "Weekday afternoon"}:
            if _time_window_specificity_score(dt) >= _time_window_specificity_score(current_tw):
                conversation.lead_time_window = dt
                return ("Thanks — which weekday works best (Mon–Fri)?", True)
            return ("Thanks — which weekday works best (Mon–Fri)?", False)

        if time_window_has_specific_day(dt) and time_window_has_detail(dt):
            day_tok = _extract_day_token(dt)
            if day_tok in {"Mon", "Tue", "Wed", "Thu", "Fri"}:
                conversation.lead_time_window = dt
                return (None, True)
            return ("Please choose a weekday (Mon–Fri). Do you prefer morning or afternoon?", False)

        if time_window_has_specific_day(dt) and not time_window_has_detail(dt):
            day_tok = _extract_day_token(dt)
            if day_tok in {"Mon", "Tue", "Wed", "Thu", "Fri"}:
                conversation.lead_time_window = dt
                return ("Got it — do you prefer morning or afternoon?", True)
            return ("Please choose a weekday (Mon–Fri). Do you prefer morning or afternoon?", False)

        return ("Do you prefer weekday morning or afternoon?", False)

    # If we already have Weekday morning/afternoon and user gives a day, combine
    if current_tw in {"Weekday morning", "Weekday afternoon"} and detected_tw:
        if detected_tw in {"Mon", "Tue", "Wed", "Thu", "Fri"}:
            part = "morning" if current_tw == "Weekday morning" else "afternoon"
            conversation.lead_time_window = f"{detected_tw} {part}"
            return (None, True)
        if detected_tw in {"Sat", "Sun"}:
            return ("Please choose a weekday (Mon–Fri). Which day works best?", False)

    # If we have day-only and user provides part-of-day or exact time, combine
    if current_tw and time_window_has_specific_day(current_tw) and not time_window_has_detail(current_tw) and detected_tw:
        day_tok = _extract_day_token(current_tw)
        dtl = (detected_tw or "").lower().strip()

        if day_tok in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat"}:
            # morning / afternoon / evening (including natural mixed phrases)
            if any(x in dtl for x in ["morning", "afternoon", "evening"]):
                if "morning" in dtl:
                    part = "morning"
                elif "afternoon" in dtl:
                    part = "afternoon"
                else:
                    part = "evening"

                if day_tok == today_tok and part == "morning" and is_after_noon:
                    return (
                        "Since it’s already afternoon, what time later today works best? Or I can help with tomorrow.",
                        False,
                    )

                conversation.lead_time_window = f"{day_tok} {part}"
                return (None, True)

            # exact time like 2pm or 14:00
            if re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", dtl) or re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", dtl):
                conversation.lead_time_window = f"{day_tok} {detected_tw}"
                return (None, True)

        return ("Please choose another day/time that works.", False)
    # Normal save path: only save if it improves specificity
    if detected_tw:
        if detected_tw in {"Sat", "Sun"} and not is_saturday_open(client):
            return ("Please choose a weekday (Mon–Fri). Which day works best?", False)

        new_score = _time_window_specificity_score(detected_tw)
        old_score = _time_window_specificity_score(current_tw)

        if new_score > old_score:
            conversation.lead_time_window = detected_tw
            current_tw = detected_tw
            saved = True
        else:
            saved = False

        if current_tw in {"Weekday morning", "Weekday afternoon"}:
            return ("Thanks — which weekday works best (Mon–Fri)?", saved)

        if time_window_has_specific_day(current_tw) and not time_window_has_detail(current_tw):
            day_tok = _extract_day_token(current_tw)

            if day_tok == today_tok and is_after_noon:
                return ("Got it — what time later today works best?", saved)

            if day_tok in {"Mon", "Tue", "Wed", "Thu", "Fri"}:
                return ("Got it — do you prefer morning or afternoon?", saved)

            return (f"Please choose another day/time that works. {weekday_example}", False)


    return (None, False)

# =========================================================
# DB dependency
# =========================================================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/chat/config")
def get_chat_config(client_key: str, db: Session = Depends(get_db)):
    client_key = (client_key or "").strip()
    if not client_key:
        raise HTTPException(status_code=400, detail="client_key is required")

    client = (
        db.query(Client)
        .filter(Client.api_key == client_key, Client.active == True)
        .first()
    )
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    return build_public_widget_config(client)


# =========================================================
# OpenAI context builders
# =========================================================
def build_context_messages(db: Session, conversation_id: uuid.UUID) -> List[Dict[str, str]]:
    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(MAX_CONTEXT_MESSAGES)
        .all()
    )
    msgs = list(reversed(msgs))
    context: List[Dict[str, str]] = []
    for m in msgs:
        role = (m.role or "").strip()
        content = (m.content or "").strip()
        if not role or not content:
            continue
        if role not in ["user", "assistant"]:
            continue
        context.append({"role": role, "content": content})
    return context

def last_assistant_asked_for_question(db: Session, conversation_id: uuid.UUID) -> bool:
    last_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    if not last_msg:
        return False
    t = _norm_text(last_msg.content or "")
    if not t:
        return False

    prompts = [
        # explicit question prompt
        "what s your question",
        "whats your question",
        "what is your question",
        "sure what s your question",
        "sure whats your question",
        "sure what is your question",

        # common generic prompts that mean "ask your question now"
        "what can i help with",
        "sure what can i help with",
        "how can i help",
        "how can i help you",
        "what can i help you with",
        "sure what can i help you with",
        "what can i help with",
        "sure how can i help",
    ]
    return any(p in t for p in prompts)

def looks_like_explicit_conversation_ending(user_text: str) -> bool:
    """Detect clear conversation-ending messages."""
    t = _norm_text(user_text)
    if not t:
        return False

    ending_phrases = {
        "thanks that s all",
        "thank you that s all",
        "thats all",
        "that s all",
        "that is all",
        "that s it",
        "thats it",
        "that is it",
        "all good",
        "i m good",
        "im good",
        "no thanks",
        "no thank you",
        "nothing else",
        "bye",
        "goodbye",
    }

    if t in ending_phrases:
        return True

    if ("thanks" in t or "thank you" in t or "thx" in t) and any(
        p in t for p in ["that s all", "thats all", "that s it", "thats it", "all good", "nothing else"]
    ):
        return True

    return False


def looks_like_simple_thanks(user_text: str) -> bool:
    t = _norm_text(user_text)
    return t in {
        "thanks",
        "thank you",
        "thx",
        "appreciate it",
        "thank you so much",
        "thanks so much",
    }


def looks_like_acknowledgement_after_final(user_text: str) -> bool:
    t = _norm_text(user_text)
    return t in {
        "ok",
        "okay",
        "perfect",
        "great",
        "sounds good",
        "got it",
        "thank you",
        "thanks",
        "thx",
        "ok thanks",
        "okay thanks",
        "ok thank you",
        "okay thank you",
        "ok thanls",
        "thanls",
    }


def last_assistant_was_final_handoff(db: Session, conversation_id: uuid.UUID) -> bool:
    last_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    if not last_msg:
        return False

    t = _norm_text(last_msg.content or "")
    if not t:
        return False

    final_signatures = [
        "we ve got your request",
        "team will contact you shortly",
        "office team will call you back shortly",
        "they ll reach out shortly",
        "flagged this as urgent",
        "mark this as urgent",
        "will call you back shortly",
        "reach out shortly",
        "contact you shortly",
    ]

    return any(p in t for p in final_signatures)

def last_assistant_requested_time_correction(db: Session, conversation_id: uuid.UUID) -> bool:
    """
    Detect when Mia recently asked the patient to choose a better day/time.
    This prevents Mia from jumping to name/phone too early if the patient asks for hours first.
    """
    last_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    if not last_msg:
        return False

    t = _norm_text(last_msg.content or "")
    if not t:
        return False

    return any(p in t for p in [
        "what day time works better for you",
        "what day time window works better for you",
        "what day time would work better for you",
        "what day and time works better for you",
    ])


def conversation_has_symptom_or_safety_reason(conversation: Conversation) -> bool:
    """
    Detect whether the lead is symptom/safety based.
    This keeps urgent wording for pain/bleeding/trauma cases,
    but avoids emergency-style endings for implant/cosmetic consultations.
    """
    reason_parts = [
        getattr(conversation, "lead_reason", "") or "",
        getattr(conversation, "lead_reason_source_text", "") or "",
        getattr(conversation, "lead_time_window", "") or "",
    ]

    t = _norm_text(" ".join(reason_parts))

    symptom_terms = [
        "tooth pain",
        "pain",
        "hurt",
        "hurts",
        "bleeding",
        "blood",
        "swelling",
        "swollen",
        "trauma",
        "injury",
        "injured",
        "fell",
        "fall",
        "broken",
        "broke",
        "cracked",
        "chipped",
        "knocked out",
        "infection",
        "abscess",
        "emergency",
    ]

    return any(term in t for term in symptom_terms)


def build_conversation_ending_reply(conversation: Conversation) -> str:
    is_emergency = bool(getattr(conversation, "lead_is_emergency", False))
    is_priority = bool(getattr(conversation, "lead_is_priority", False))
    has_symptom_reason = conversation_has_symptom_or_safety_reason(conversation)

    if is_emergency or (is_priority and has_symptom_reason):
        return "You’re welcome. If anything gets worse, please call the office right away."

    if is_priority:
        return "You’re welcome. The office team will follow up shortly."

    return "You’re welcome. Please contact the office if you need anything else."

def last_assistant_asked_for_phone(db: Session, conversation_id: uuid.UUID) -> bool:
    last_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    if not last_msg:
        return False

    t = _norm_text(last_msg.content or "")
    return any(p in t for p in [
        "best phone number",
        "what s your phone number",
        "whats your phone number",
        "what is your phone number",
        "phone number to reach you",
    ])


def last_assistant_asked_for_name(db: Session, conversation_id: uuid.UUID) -> bool:
    last_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    if not last_msg:
        return False
    t = _norm_text(last_msg.content or "")
    if not t:
        return False
    name_prompts = [
        "what s your first name",
        "whats your first name",
        "what is your first name",
        "your first name",
        "what s your name",
        "whats your name",
        "what is your name",
    ]
    return any(p in t for p in name_prompts)

def last_assistant_was_emergency_prompt(db: Session, conversation_id: uuid.UUID) -> bool:
    last_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    if not last_msg:
        return False

    t = _norm_text(last_msg.content or "")
    if not t:
        return False

    exact_prompts = [
        "to help quickly what s your first name",
        "thanks what s the best phone number to reach you right now",
        "briefly what s going on",
    ]
    return any(p in t for p in exact_prompts)

def _looks_like_affirmative(user_text: str) -> bool:
    t = _norm_text(user_text)
    return t in {
        "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "sounds good", "please", "lets do it", "let s do it"
    }


def last_assistant_offered_scheduling_service(db: Session, conversation_id: uuid.UUID) -> Optional[str]:
    """
    If the last assistant message was one of our service confirmations like:
      'Yes — we offer dental implants. Would you like to schedule a consultation?'
    return the canonical service_reason enum (e.g. 'extraction/implant').
    """
    last_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    if not last_msg:
        return None

    t = _norm_text(last_msg.content or "")
    if not t:
        return None

    # Keep these aligned with your 3.6 service confirmation text
    if "offer dental implants" in t or ("implants" in t and "schedule" in t):
        return "extraction/implant"
    if "offer braces" in t or "invisalign" in t or "orthodont" in t:
        return "orthodontics"
    if "do crowns" in t or "crown" in t:
        return "crown"
    if "offer teeth whitening" in t or "whitening" in t:
        return "cosmetic/whitening"
    if "do fillings" in t or "fillings" in t or "cavity" in t:
        return "broken tooth/filling"
    if "do extractions" in t or "wisdom" in t or "extractions" in t:
        return "extraction/implant"

    if "cleanings and exams" in t or "offer cleanings" in t:
        return "cleaning/checkup"

    if "veneers" in t and ("schedule" in t or "consultation" in t):
        return "cosmetic/whitening"

    if "tooth pain" in t and ("request an appointment" in t or "schedule" in t):
        return "tooth pain"

    return None

def last_assistant_was_emergency(db: Session, conversation_id: uuid.UUID) -> bool:
    last_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    if not last_msg:
        return False
    t = _norm_text(last_msg.content or "")
    # simple signature phrases from your emergency message
    return ("call 911" in t or "go to the er" in t) and ("dental emergency" in t or "call the office" in t)

def user_accepted_scheduling(user_text: str) -> bool:
    t = _norm_text(user_text)
    return t in {"yes", "yeah", "yep", "yup", "ok", "okay", "sure", "sounds good", "please", "lets do it", "let s do it"}

def build_lead_context(conversation: Conversation) -> Optional[Dict[str, str]]:
    parts: List[str] = []
    if (conversation.lead_name or "").strip():
        parts.append(f"Lead name: {conversation.lead_name}")
    if (conversation.lead_phone or "").strip():
        parts.append(f"Lead phone: {conversation.lead_phone}")
    if (conversation.lead_email or "").strip():
        parts.append(f"Lead email: {conversation.lead_email}")
    if (conversation.lead_reason or "").strip():
        parts.append(f"Lead reason: {conversation.lead_reason}")
    if getattr(conversation, "lead_is_new_patient", None) is True:
        parts.append("New patient: yes")
    if getattr(conversation, "lead_is_new_patient", None) is False:
        parts.append("New patient: no (returning)")
    if (getattr(conversation, "lead_time_window", None) or "").strip():
        parts.append(f"Preferred time window: {conversation.lead_time_window}")
    if getattr(conversation, "lead_email_opt_out", False):
        parts.append("Email opt-out: yes")
    if not parts:
        return None
    summary = "Conversation lead info (already captured):\n" + "\n".join(parts)
    return {"role": "system", "content": summary}


# =========================================================
# Evidence-safe AI extraction helpers
# =========================================================
def _safe_substring_evidence(user_text: str, evidence: Optional[str]) -> Optional[str]:
    if not evidence:
        return None
    e = evidence.strip()
    if not e:
        return None
    if e not in (user_text or ""):
        return None
    if len(e) > 120:
        return None
    return e


def _validate_lead_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    n = name.strip()
    if not n or len(n) > 60:
        return None
    if re.search(r"\b(my name is|this is|i am|i'm)\b", n.lower()):
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z'\- ]{0,59}", n):
        return None
    return " ".join(w.capitalize() for w in n.split())


def _validate_lead_reason(reason: Optional[str]) -> Optional[str]:
    if not reason:
        return None
    r = reason.strip()
    if r not in LEAD_REASON_ENUM:
        return None
    return r

EMERGENCY_TRIGGERS = [
    "emergency",
    "severe pain", "extreme pain", "unbearable",
    "swelling", "face swelling", "jaw swelling",
    "bleeding", "won't stop bleeding",
    "knocked out", "knocked-out", "tooth fell out",
    "broken tooth", "cracked tooth",
    "abscess", "infection", "pus",
    "can't swallow", "cant swallow",
    "can't breathe", "cant breathe",
]

def looks_like_emergency(text: str) -> bool:
    t = _norm_text(text)
    return any(k in t for k in EMERGENCY_TRIGGERS)

def looks_like_urgent_dental_safety_issue(text: str) -> bool:
    """
    Catches urgent dental safety situations that may not use exact emergency keywords.
    This is intentionally narrow so normal broken-tooth messages do not all become emergencies.
    """
    t = _norm_text(text)
    if not t:
        return False

    trauma_words = [
        "fell",
        "fall",
        "fallen",
        "hit",
        "accident",
        "injury",
        "injured",
    ]

    tooth_damage_words = [
        "tooth broke",
        "broke my tooth",
        "my tooth broke",
        "broken tooth",
        "cracked tooth",
        "chipped tooth",
        "tooth fell out",
        "knocked out",
    ]

    strong_bleeding_words = [
        "blood everywhere",
        "bleeding everywhere",
        "there is blood everywhere",
        "blood all over",
        "bleeding a lot",
        "a lot of blood",
        "lots of blood",
        "mouth is bleeding",
        "bleeding from my mouth",
    ]

    dangerous_self_treatment = looks_like_dangerous_dental_instruction(text)

    has_trauma = any(p in t for p in trauma_words)
    has_tooth_damage = any(p in t for p in tooth_damage_words)
    has_strong_bleeding = any(p in t for p in strong_bleeding_words)

    return (
        (has_trauma and (has_tooth_damage or has_strong_bleeding))
        or (has_tooth_damage and has_strong_bleeding)
        or (dangerous_self_treatment and has_strong_bleeding)
    )

def is_after_hours(now: datetime, office_hours_obj) -> bool:
    """
    office_hours_obj is whatever you already store for hours.
    If you don't have structured hours, you can just return False
    and rely on policy text only.
    """
    # If you have no structured hours yet:
    # return False
    # Otherwise implement your hours logic here.
    return False

def get_emergency_defaults() -> tuple[str, str]:
    during = (
        "If you have trouble breathing or swallowing, uncontrolled bleeding, or rapidly worsening swelling, "
        "please call 911 or go to the ER now.\n\n"
        "If it’s a dental emergency (severe tooth pain, swelling, broken tooth, knocked-out tooth), "
        "please call the office so we can advise and try to fit you in as soon as possible."
    )
    after = (
        "If this is an emergency and it’s after hours, please call the office and follow the voicemail instructions. "
        "If you have trouble breathing or swallowing, uncontrolled bleeding, or rapidly worsening swelling, "
        "call 911 or go to the ER."
    )
    return during, after

def extract_lead_fields_with_ai(user_text: str) -> Dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "lead_name": {"type": ["string", "null"]},
            "lead_reason": {"type": ["string", "null"], "enum": LEAD_REASON_ENUM + [None]},
            "lead_name_source_text": {"type": ["string", "null"]},
            "lead_reason_source_text": {"type": ["string", "null"]},
        },
        "required": ["lead_name", "lead_reason", "lead_name_source_text", "lead_reason_source_text"],
        "additionalProperties": False,
    }

    extractor_system = (
        "You extract lead fields from a single user message for a dental office chatbot. "
        "Return ONLY JSON that matches the provided schema. "
        "Do NOT guess. If unclear or not explicitly provided, return null. "
        "For lead_name: return exactly the name the user provided; do not add a last name if not provided. "
        "For evidence fields (*_source_text): return the exact substring from the user message that supports the extracted value. "
        "If you return a non-null field, its *_source_text must also be non-null and must appear verbatim in the user message."
    )

            
    print("USING CHAT_MODEL:", CHAT_MODEL)  # confirms which model Render/local is using
    print("OPENAI KEY EXISTS:", bool(OPENAI_API_KEY))  # confirms API key is loaded without exposing it
    
    response = ai.responses.create(
        model=EXTRACTOR_MODEL,
        input=[
            {"role": "system", "content": extractor_system},
            {"role": "user", "content": user_text},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "lead_extractor_v1",
                "strict": True,
                "schema": schema,
            }
        },
        max_output_tokens=200,
    )

    raw = (response.output_text or "").strip()
    if not raw:
        return {
            "lead_name": None,
            "lead_reason": None,
            "lead_name_source_text": None,
            "lead_reason_source_text": None,
        }
    try:
        data = json.loads(raw)
    except Exception:
        return {
            "lead_name": None,
            "lead_reason": None,
            "lead_name_source_text": None,
            "lead_reason_source_text": None,
        }
    if not isinstance(data, dict):
        return {
            "lead_name": None,
            "lead_reason": None,
            "lead_name_source_text": None,
            "lead_reason_source_text": None,
        }
    return data


def classify_message_guard_with_ai(user_text: str) -> Dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "is_abusive": {"type": "boolean"},
            "is_sexual": {"type": "boolean"},
            "is_hate": {"type": "boolean"},
            "is_attack": {"type": "boolean"},
            "is_offtopic": {"type": "boolean"},
            "reason": {"type": ["string", "null"]},
        },
        "required": ["is_abusive", "is_sexual", "is_hate", "is_attack", "is_offtopic", "reason"],
        "additionalProperties": False,
    }

    system_text = (
        "You are a safety + intent classifier for a dental office chatbot. "
        "Classify the user's single message. Works in any language. "
        "Return ONLY JSON matching the schema. "
        "is_attack=true for hacking attempts, SQL injection, malicious payloads, credential/data exfiltration requests. "
        "is_offtopic=true for unrelated trivia/politics/general knowledge not about dental scheduling or office info. "
        "If uncertain, set all booleans false and reason null."
    )

    try:
        resp = ai.responses.create(
            model=EXTRACTOR_MODEL,
            input=[{"role": "system", "content": system_text}, {"role": "user", "content": user_text}],
            text={"format": {"type": "json_schema", "name": "msg_guard_v1", "strict": True, "schema": schema}},
            max_output_tokens=140,
        )
        raw = (resp.output_text or "").strip()
        if not raw:
            return {
                "is_abusive": False,
                "is_sexual": False,
                "is_hate": False,
                "is_attack": False,
                "is_offtopic": False,
                "reason": None,
            }
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {
                "is_abusive": False,
                "is_sexual": False,
                "is_hate": False,
                "is_attack": False,
                "is_offtopic": False,
                "reason": None,
            }
        return data
    except Exception:
        return {
            "is_abusive": False,
            "is_sexual": False,
            "is_hate": False,
            "is_attack": False,
            "is_offtopic": False,
            "reason": None,
        }


# =========================================================
# Abuse / profanity helpers
# =========================================================
NAME_DENYLIST = {
    "puta",
    "bitch",
    "ass",
    "a55",
    "a$$",
    "mutherfucker",
    "motherfucka",
    "muthafucka",
    "tard",
    "retard",
    "bitchass",
    "bitchass nigga",
    "b1tch",
    "b1+ch",
    "huevon",
    "bastard",
    "whore",
    "slut",
    "cunt",
    "anal",
    "fuck",
    "shit",
    "asshole",
    "motherfucker",
    "mierda",
    "cabron",
    "pendejo",
    "puto",
}

PROFANITY_WORDS = {
    "fuck",
    "puta",
    "bitch",
    "ass",
    "a55",
    "a$$",
    "mutherfucker",
    "motherfucka",
    "muthafucka",
    "tard",
    "retard",
    "bitchass",
    "bitchass nigga",
    "fucker",
    "b1tch",
    "b1+ch",
    "huevon",
    "bastard",
    "whore",
    "fag",
    "faggot",
    "homo",
    "byach",
    "beeyach",
    "biznitch",
    "fucking",
    "shit",
    "bitch",
    "asshole",
    "cunt",
    "dick",
    "pussy",
    "cock",
    "cocksucker",
    "motherfucker",
    "puta",
    "puto",
    "pendejo",
    "cabron",
    "mierda",
    "coño",
}

HATE_SLUR_WORDS = {
    "nigger",
    "nigga",
    "kike",
    "spic",
}


def contains_profanity(user_text: str) -> bool:
    t = _norm_text(user_text)
    if not t:
        return False
    tokens = set(re.findall(r"[a-z']+", t))
    if tokens & PROFANITY_WORDS:
        return True
    if tokens & HATE_SLUR_WORDS:
        return True
    if re.search(r"\bf+u+c+k+\b|\bs+h+i+t+\b", t):
        return True
    return False


def safe_name_normalize(name_in: str) -> Optional[str]:
    if not name_in:
        return None

    raw = name_in.strip()
    if not raw:
        return None

    cleaned = re.sub(r"[^A-Za-z'\- ]", " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if len(cleaned) < 2 or len(cleaned) > 40:
        return None

    if not re.fullmatch(r"[A-Za-z][A-Za-z'\-]{1,30}(?:\s+[A-Za-z][A-Za-z'\-]{1,30})?", cleaned):
        return None

    if not re.search(r"[aeiouAEIOU]", cleaned):
        return None

    if contains_profanity(cleaned):
        return None

    return " ".join(w[:1].upper() + w[1:].lower() for w in cleaned.split())


def safe_normalize_name(raw: str) -> str:
    s = unicodedata.normalize("NFKC", (raw or "").strip())
    s = re.sub(r"\s+", " ", s).strip()
    s = "".join(ch for ch in s if ch.isalpha() or ch in {" ", "-", "'"}).strip()
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    parts = [p for p in s.split(" ") if p]
    parts = [p[:1].upper() + p[1:].lower() if p else "" for p in parts]
    return " ".join(parts).strip()


def is_plausible_name(normalized_name: str) -> bool:
    n = (normalized_name or "").strip()
    if not n:
        return False
    if len(n) < 2 or len(n) > 40:
        return False
    low = _norm_text(n)
    if low in NAME_DENYLIST:
        return False
    words = [w for w in n.split(" ") if w]
    if len(words) > 2:
        return False
    for w in words:
        letters_only = "".join(ch for ch in w if ch.isalpha())
        if len(letters_only) < 2:
            return False
    return True


def notify_office_of_lock(db, client, conversation, user_text: str, ip: str) -> None:
    if getattr(conversation, "office_notified_on_lock", False):
        return

    if hasattr(conversation, "office_notified_on_lock"):
        conversation.office_notified_on_lock = True
        db.add(conversation)
        db.commit()

    note = f"[SYSTEM] Conversation locked for abuse. ip={ip}. last_user_text={user_text[:200]}"
    db.add(Message(conversation_id=conversation.id, role="assistant", content=note))
    db.commit()


def looks_like_obscene_or_harassing(user_text: str) -> bool:
    t = (user_text or "").lower().strip()
    if not t:
        return False
    if contains_profanity(user_text):
        return True
    if any(p in t for p in ["suck my","suck ma", "suk ma","suk my","send nudes", "sex", "porn", "nude", "blowjob"]):
        return True
    if any(p in t for p in ["kill yourself", "i will kill", "i will hurt", "rape"]):
        return True
    return False

def looks_like_greeting(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"hi", "hello", "hey", "good morning", "good afternoon", "good evening"}

def looks_like_friendly_greeting_or_small_talk(user_text: str) -> bool:
    """Detect simple greetings and small talk without hijacking real office questions."""
    t = _norm_text(user_text)
    if not t:
        return False

    # Do not treat real office/service questions as small talk.
    if (
        looks_like_office_phone_request(user_text)
        or looks_like_insurance_request(user_text)
        or looks_like_pricing_request(user_text)
        or looks_like_scheduling_intent(user_text)
        or detect_service_selection(user_text)
        or detect_appointment_reason(user_text)
    ):
        return False

    greeting_phrases = [
        "hi",
        "hello",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
        "hi mia",
        "hello mia",
        "hey mia",
    ]

    small_talk_phrases = [
        "how are you",
        "how are you doing",
        "how are u",
        "how r you",
        "how s it going",
        "hows it going",
        "how is it going",
    ]

    return any(t == p or t.startswith(p + " ") for p in greeting_phrases) or any(p in t for p in small_talk_phrases)


def build_friendly_greeting_reply(user_text: str) -> str:
    t = _norm_text(user_text)

    if any(p in t for p in ["how are you", "how are you doing", "how are u", "how r you", "how s it going", "hows it going", "how is it going"]):
        return (
            "I’m doing well, thank you for asking. "
            "How can I help you today with appointments, services, insurance, hours, or location?"
        )

    return (
        "Hi, I’m Mia. "
        "How can I help you today with appointments, services, insurance, hours, or location?"
    )

def looks_like_sensitive_data_request(user_text: str) -> bool:
    t = _norm_text(user_text)
    if not t:
        return False
    if any(p in t for p in ["ssn", "social security", "credit card", "cvv", "bank account", "routing number", "password"]):
        return True
    return False

def _next_emergency_prompt(conversation) -> str:
    if not (conversation.lead_name or "").strip():
        return "To help quickly, what’s your first name?"
    if not (conversation.lead_phone or "").strip():
        return "Thanks — what’s the best phone number to reach you right now?"
    if not (conversation.lead_reason or "").strip():
        return "Briefly, what’s going on? (e.g., severe pain, swelling, broken tooth)"
    return "Thanks — please call the office now so we can advise you and fit you in."


def looks_like_sql_injection_attempt(user_text: str) -> bool:
    t = (user_text or "").lower()
    if not t:
        return False
    if re.search(r"(\bor\b|\band\b)\s+1\s*=\s*1", t):
        return True
    if any(p in t for p in ["';", "\";", "--", "/*", "*/", "drop table", "union select", "information_schema"]):
        return True
    return False


def looks_like_random_trivia(user_text: str) -> bool:
    t = _norm_text(user_text)
    if not t:
        return False

    if looks_like_info_intent(user_text) or looks_like_scheduling_intent(user_text):
        return False

    trivia_phrases = [
        "is the sky blue",
        "who is the president",
        "who wrote this code",
        "what model are you",
        "tell me a joke",
        "how much wood could a woodchuck chuck",
    ]
    if any(p in t for p in trivia_phrases):
        return True

    dental_keywords = [
        "tooth",
        "teeth",
        "gum",
        "gums",
        "cavity",
        "cavities",
        "filling",
        "fillings",
        "cleaning",
        "exam",
        "xray",
        "x ray",
        "crown",
        "crowns",
        "whitening",
        "braces",
        "invisalign",
        "implant",
        "extraction",
        "wisdom",
        "insurance",
        "hours",
        "address",
        "location",
        "parking",
        "appointment",
        "schedule",
        "book",
        "pain",
        "swelling",
    ]

    looks_like_question = (("?" in (user_text or "")) or t.startswith(("who ", "what ", "when ", "where ", "why ", "how ")))
    if looks_like_question and not any(k in t for k in dental_keywords):
        return True

    return False


def looks_like_medical_advice(user_text: str) -> bool:
    t = (user_text or "").strip().lower()
    if not t:
        return False

    if any(k in t for k in ["book", "schedule", "appointment", "availability", "available", "set up an appointment", "make an appointment"]):
        return False

    strong_phrases = [
        "what should i do",
        "what do i do",
        "should i",
        "do you recommend",
        "what do you recommend",
        "what medicine",
        "can i take",
        "take antibiotics",
        "do i need antibiotics",
        "home remedy",
        "how do i treat",
        "how to treat",
        "how do i fix",
        "what to do if",
        "what to do about",
        "should i pull",
        "pull it out",
    ]
    if any(p in t for p in strong_phrases):
        return True

    symptoms = [
        "toothache",
        "tooth ache",
        "tooth hurts",
        "tooth pain",
        "pain",
        "swelling",
        "bleeding",
        "infection",
        "abscess",
        "pus",
        "fever",
        "broken tooth",
        "cracked tooth",
        "jaw pain",
        "gum pain",
        "sensitive",
    ]
    if any(s in t for s in symptoms) and ("?" in t or "what" in t or "how" in t or "should" in t):
        return True

    return False


ABUSE_STRIKE_LIMIT = 3
ABUSE_LOCK_MINUTES = 10


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def conversation_is_locked(conversation: Conversation) -> bool:
    until = getattr(conversation, "abuse_locked_until", None)
    if until is None:
        return False
    try:
        return until > _now_utc()
    except Exception:
        return False


def record_abuse_strike(db: Session, conversation: Conversation) -> None:
    current = int(getattr(conversation, "abuse_strikes", 0) or 0) + 1
    conversation.abuse_strikes = current
    if current >= ABUSE_STRIKE_LIMIT:
        conversation.abuse_locked_until = _now_utc() + timedelta(minutes=ABUSE_LOCK_MINUTES)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)


def decay_abuse_strikes(db: Session, conversation: Conversation) -> None:
    current = int(getattr(conversation, "abuse_strikes", 0) or 0)
    if current <= 0:
        return
    conversation.abuse_strikes = max(0, current - 1)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)


def best_faq_match(db: Session, client_id, user_text: str) -> Optional[ClientFAQ]:
    t_norm = _norm_text(user_text)
    if not t_norm:
        return None

    # ✅ Canonical rewrites for operational intents (boost match rate)
    if any(k in t_norm for k in ["where are you", "where is your office", "office located", "address", "location", "directions", "parking", "located"]):
        t_norm = "where are you located"
    elif any(k in t_norm for k in ["hours", "open", "close", "when are you open", "what time does your office close","closing"]):
        t_norm = "what are your hours"
    elif any(k in t_norm for k in ["insurance", "do you take",  "cover", "do you accept", "delta", "metlife", "cigna", "aetna", "accept insurance", "ppo", "in network with", "hmo", "medicare", "medicaid"]):
        t_norm = "what insurance do you accept"

    # use normalized text consistently
    if len(_tokenize(t_norm)) < 2:
        return None

    # ... continue with your existing matching logic, BUT use t_norm from here down

    # ---------------------------------------------------------
    # HARD TOPIC DETECTORS (user-side)
    # ---------------------------------------------------------
    user_is_hours_query = any(
        k in t_norm for k in ["hours", "office hours", "open", "close", "closing", "what time", "when do you close", "when are you open"]
    )

    user_is_location_query = any(
        k in t_norm for k in ["address", "location", "where are you", "where are you located", "parking", "directions"]
    )

    user_is_insurance_query = any(
        k in t_norm for k in ["insurance", "insurances", "accept insurance", "take insurance", "do you take", "cover", "do you accept", "delta", "metlife", "cigna", "aetna", "ppo", "in network with","in-network", "hmo", "medicare", "medicaid"]

    )

    # Optional: treat phone/email/contact as "contact" topic
    user_is_contact_query = any(
        k in t_norm for k in ["phone", "phone number", "fax", "email", "contact"]
    )

    # ---------------------------------------------------------
    # Load FAQs
    # ---------------------------------------------------------
    rows = (
        db.query(ClientFAQ)
        .filter(ClientFAQ.client_id == client_id, ClientFAQ.enabled == True)
        .all()
    )

    user_tokens = set(_tokenize(t_norm))
    best = None
    best_score = 0

    for f in rows:
        q_text = (f.question or "")
        kw_text = (f.keywords or "")
        f_text = f"{q_text} {kw_text}"
        f_norm = _norm_text(f_text)

        # ---------------------------------------------------------
        # HARD GATES (FAQ-side topic -> must match user topic)
        # This prevents "hours" FAQ from matching implant questions, etc.
        # ---------------------------------------------------------
        faq_is_hours = any(k in f_norm for k in ["hours", "office hours", "open", "close", "closing time"])
        if faq_is_hours and not user_is_hours_query:
            continue

        faq_is_location = any(k in f_norm for k in ["address", "location", "where are you", "where are you located", "parking", "directions"])
        if faq_is_location and not user_is_location_query:
            continue

        faq_is_insurance = "insurance" in f_norm or "insurances" in f_norm or "in network" in f_norm
        if faq_is_insurance and not user_is_insurance_query:
            continue

        faq_is_contact = any(k in f_norm for k in ["phone", "phone number", "fax", "email", "contact"])
        if faq_is_contact and not user_is_contact_query:
            continue

        # ---------------------------------------------------------
        # Your existing scoring logic
        # ---------------------------------------------------------
        q_tokens = set(_tokenize(q_text))
        kw_tokens = set()
        if kw_text.strip():
            for part in kw_text.split(","):
                kw_tokens.update(_tokenize(part))

        score = 0
        if kw_tokens and (user_tokens & kw_tokens):
            score += 6
        score += len(user_tokens & q_tokens)

        q_norm = _norm_text(q_text)
        if (q_norm and (q_norm in t_norm or t_norm in q_norm)) and len(t_norm) <= 120:
            score += 8

        if score > best_score:
            best_score = score
            best = f

    return best if best_score >= 8 else None

# =========================================================
# Scheduling intent + receptionist flow
# =========================================================
def is_scheduling_intent(user_text: str) -> bool:
    t = (user_text or "").lower()
    scheduling_keywords = [
        "appointment",
        "schedule",
        "book",
        "availability",
        "available",
        "come in",
        "set up an appointment",
        "make an appointment",
        "book an appointment",
    ]
    if any(k in t for k in scheduling_keywords):
        return True
    if extract_phone(user_text) or extract_email(user_text):
        return True
    return False


def looks_like_name_only(user_text: str) -> Optional[str]:
    raw = (user_text or "").strip()
    if not raw:
        return None

    if detect_service_selection(raw):
        return None

    if looks_like_obscene_or_harassing(raw):
        return None

    bad = {
        "skip",
        "no",
        "nope",
        "nah",
        "yes",
        "yep",
        "new",
        "returning",
        "cleaning",
        "cleanings",
        "checkup",
        "check-up",
        "exam",
        "exams",
        "filling",
        "fillings",
        "cavity",
        "cavities",
        "implant",
        "implants",
        "extraction",
        "extractions",
        "wisdom tooth",
        "wisdom teeth",
        "braces",
        "invisalign",
        "whitening",
        "teeth whitening",
    }
    if raw.lower() in bad:
        return None

    normalized = safe_name_normalize(raw)
    if not normalized:
        return None

    needs_ai = any(ord(ch) > 127 for ch in raw)
    if needs_ai:
        guard = classify_message_guard_with_ai(raw)
        if (
            bool(guard.get("is_abusive", False))
            or bool(guard.get("is_hate", False))
            or bool(guard.get("is_sexual", False))
            or bool(guard.get("is_attack", False))
        ):
            return None

    return normalized


def receptionist_bypass_reply(conversation: Conversation) -> Tuple[Optional[str], Optional[str]]:
    has_reason = bool((conversation.lead_reason or "").strip())
    has_name = bool((conversation.lead_name or "").strip())
    has_phone = bool((conversation.lead_phone or "").strip())
    has_email = bool((conversation.lead_email or "").strip())
    np_known = getattr(conversation, "lead_is_new_patient", None) is not None
    tw_val = (getattr(conversation, "lead_time_window", None) or "").strip()
    tw_known = time_window_is_complete(tw_val)

    email_opt_out = bool(getattr(conversation, "lead_email_opt_out", False))

    time_window = (getattr(conversation, "lead_time_window", None) or "").strip()
    is_priority_non_emergency = (
        (
            bool(getattr(conversation, "lead_is_priority", False))
            or time_window in {"ASAP", "ASAP / tomorrow ok"}
        )
        and not bool(getattr(conversation, "lead_is_emergency", False))
    )

    if not has_reason:
        return (
            "What brings you in—cleaning/checkup, tooth pain, fillings, crowns, braces/Invisalign, whitening, or something else?",
            "reason",
        )

    # Priority non-emergency flow: name + phone only.
    # Do not ask email, preferred time, or new/returning for ASAP tooth pain.
    if is_priority_non_emergency:
        if not has_name:
            return ("Got it — I’ll mark this as urgent. What’s your first name?", "name")
        if not has_phone:
            return (f"Thanks {conversation.lead_name}! What’s the best phone number for the office to call you back?", "phone")
        return (build_priority_handoff_reply(conversation), "complete")
    if not has_name:
        return ("No problem — I can help you schedule an appointment. What’s your first name?", "name")
    if not has_phone:
        return (f"Thanks {conversation.lead_name}! What’s the best phone number to reach you?", "phone")
    if not has_email and not email_opt_out:
        return ("Do you also have an email for confirmation? (Optional—Type ‘skip’ to continue.)", "email")

    if not tw_known:
        if tw_val in {"Weekday morning", "Weekday afternoon"}:
            return ("Thanks — which weekday works best (Mon–Fri)?", "time_window")
        if tw_val and time_window_has_specific_day(tw_val) and not time_window_has_detail(tw_val):
            return ("Got it — do you prefer morning or afternoon?", "time_window")
        name = (conversation.lead_name or "").strip()
        name_part = f" {name}" if name else ""
        return (f"Great—thanks{name_part}. What day/time window works best (e.g., Tue morning)?", "time_window")
    if not np_known:
        name = (conversation.lead_name or "").strip()
        prefix = f"{name}, " if name else ""

        return (f"One quick question — {prefix}are you a new or returning patient?", "new_patient")

    return (f"Thanks! We’ve got your request—our team will contact you shortly to confirm the appointment time.")

def priority_intake_is_complete(conversation: Conversation) -> bool:
    """Priority non-emergency leads only need name + phone."""
    time_window = (getattr(conversation, "lead_time_window", None) or "").strip()

    is_priority = (
        bool(getattr(conversation, "lead_is_priority", False))
        or time_window in {"ASAP", "ASAP / tomorrow ok"}
    )

    is_emergency = bool(getattr(conversation, "lead_is_emergency", False))

    return (
        is_priority
        and not is_emergency
        and bool((conversation.lead_name or "").strip())
        and bool((conversation.lead_phone or "").strip())
    )


def build_priority_handoff_reply(conversation: Conversation) -> str:
    name = (conversation.lead_name or "").strip()
    name_part = f", {name}" if name else ""

    reason_parts = [
        getattr(conversation, "lead_reason", "") or "",
        getattr(conversation, "lead_reason_source_text", "") or "",
        getattr(conversation, "lead_time_window", "") or "",
    ]
    t = _norm_text(" ".join(reason_parts))

    symptom_terms = [
        "tooth pain",
        "pain",
        "hurt",
        "hurts",
        "bleeding",
        "blood",
        "swelling",
        "swollen",
        "trauma",
        "injury",
        "injured",
        "fell",
        "fall",
        "broken",
        "broke",
        "cracked",
        "chipped",
        "knocked out",
        "infection",
        "abscess",
        "emergency",
    ]

    has_symptom_reason = any(term in t for term in symptom_terms)
    is_emergency = bool(getattr(conversation, "lead_is_emergency", False))

    if is_emergency or has_symptom_reason:
        return (
            f"Thanks{name_part} — I’ll mark this as urgent for the team.\n\n"
            "The office team will call you back shortly."
        )

    if "today" in t:
        return (
            f"Thanks{name_part} — I’ll send this same-day appointment request to the team.\n\n"
            "The office team will call you back shortly."
        )

    return (
        f"Thanks{name_part} — I’ll send this priority appointment request to the team.\n\n"
        "The office team will call you back shortly."
    )

def _next_intake_prompt(client: Client, conversation) -> str:
    name = (conversation.lead_name or "").strip()
    name_prefix = f"{name}, " if name else ""
    # Match your existing intake field order
    if not (conversation.lead_name or "").strip():
        return "What’s your first name?"
    if not (conversation.lead_phone or "").strip():
        return "Thanks — what’s the best phone number to reach you?"
    if priority_intake_is_complete(conversation):
        return build_priority_handoff_reply(conversation)
    if not (conversation.lead_email or "").strip() and not bool(getattr(conversation, "lead_email_opt_out", False)):
        return "What’s your email? (You can also type 'skip'.)"
    if not (getattr(conversation, "lead_time_window", None) or "").strip():
        return f"What day/time works best for you? {build_time_window_examples(client, prefer_weekdays=False)}"
    if getattr(conversation, "lead_is_new_patient", None) is None:
        return f"One quick question — {name_prefix}are you a new or returning patient?"
    return "Thanks — our team will reach out to confirm your appointment."

def _emergency_meta(label="Call the office now") -> dict:
    return {
        "mode": "emergency",
        "faq_match": False,
        "show_booking_button": True,
        "booking_type": "emergency",
        "booking_service_reason": "emergency",
        "booking_cta_label": label,
        
    }
# =========================================================
# The /chat endpoint
# =========================================================
@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host
    user_text = (req.message or "").strip()
    t_lower = user_text.lower()
   

    if not user_text:
        raise HTTPException(400, "Message cannot be empty")
    if len(user_text) > MAX_USER_CHARS:
        raise HTTPException(400, f"Message too long (max {MAX_USER_CHARS} chars)")

    client = (
        db.query(Client)
        .filter(Client.api_key == req.client_key, Client.active == True)
        .first()
    )
    
    if not client:
        raise HTTPException(403, "Invalid client key")
    
    show_start_over = get_client_setting(client, "show_start_over", True)

    office_phone = getattr(client, "office_phone", None) or "(555) 123-4567"

    conversation: Optional[Conversation] = None
    if req.conversation_id:
        try:
            conv_uuid = uuid.UUID(req.conversation_id)
            conversation = (
                db.query(Conversation)
                .filter(Conversation.id == conv_uuid, Conversation.client_id == client.id)
                .first()
            )
        except Exception:
            conversation = None

    if conversation is None:
        conversation = Conversation(
            client_id=client.id,
            visitor_id=req.visitor_id,
            is_lead=False,
            lead_status="new",
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    db.add(Message(conversation_id=conversation.id, role="user", content=user_text))
    db.commit()

    # =========================================================
    # Final closed guard (prevents further chatting)
    # =========================================================
    if bool(getattr(conversation, "final_closed", False)):
        reply_text = "This conversation has ended. Please tap Start Over to begin a new request."
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "final_closed",
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # 0) Guard rails
    # =========================================================
    if conversation_is_locked(conversation):
        reply_text = build_zero_tolerance_lock_reply(office_phone)
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "locked",
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )

    if looks_like_obscene_or_harassing(user_text):
        conversation.abuse_strikes = 1
        conversation.abuse_locked_until = datetime.now(timezone.utc) + timedelta(days=3650)

        db.add(conversation)
        db.commit()
        db.refresh(conversation)

        has_contact = bool((conversation.lead_phone or "").strip() or (conversation.lead_email or "").strip())
        has_reason = bool((conversation.lead_reason or "").strip())
        if has_contact or has_reason:
            notify_office_of_lock(db, client, conversation, user_text=user_text, ip=ip)

        reply_text = build_zero_tolerance_lock_reply(office_phone)
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "one_strike_locked",
                "locked": True,
                "show_menu": False,
                "disable_input": True,
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )

    if looks_like_sql_injection_attempt(user_text):
        record_abuse_strike(db, conversation)
        reply_text = "I can help with scheduling and office info only. Please rephrase your request."
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={"mode": "guard_sql", "faq_match": False, "strikes": int(conversation.abuse_strikes or 0), "show_start_over": show_start_over,},
        )

    if looks_like_sensitive_data_request(user_text):
        reply_text = (
            "For your security, please don’t share sensitive info here. "
            "I can help you schedule an appointment or share office info."
        )
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={"mode": "guard_sensitive", "faq_match": False, "show_start_over": show_start_over,},
        )

    # =========================================================
    # Intake mode gate
    # =========================================================
    asked_for_question = last_assistant_asked_for_question(db, conversation.id)
    question_mode = bool(asked_for_question)

    offered_service_reason = last_assistant_offered_scheduling_service(db, conversation.id)
    accepted_schedule = bool(offered_service_reason) and user_accepted_scheduling(user_text)

    service_reason_now = offered_service_reason if accepted_schedule else detect_service_selection(user_text)
    if user_text.strip().lower() in {"something else", "other", "not sure"}:
        service_reason_now = "other"
        question_mode = False   # ✅ force it back into intake
    is_scheduling_now = True if accepted_schedule else is_scheduling_intent(user_text)

    has_any_lead_data = bool(
        (conversation.lead_reason or "").strip()
        or (conversation.lead_name or "").strip()
        or (conversation.lead_phone or "").strip()
        or (conversation.lead_email or "").strip()
        or bool(getattr(conversation, "lead_email_opt_out", False))
        or bool((getattr(conversation, "lead_time_window", None) or "").strip())
        or (getattr(conversation, "lead_is_new_patient", None) is not None)
    )

    would_be_in_intake = bool(service_reason_now or is_scheduling_now or has_any_lead_data)
    resume_intake_after_answer = False

    if question_mode and not accepted_schedule and not is_scheduling_now:
        in_intake_mode = False
        resume_intake_after_answer = would_be_in_intake
    else:
        in_intake_mode = would_be_in_intake
        resume_intake_after_answer = False

    print(
        "[GATE]",
        "question_mode=", question_mode,
        "accepted_schedule=", accepted_schedule,
        "offered_service_reason=", offered_service_reason,
        "service_reason_now=", service_reason_now,
        "is_scheduling_now=", is_scheduling_now,
        "has_any_lead_data=", has_any_lead_data,
        "in_intake_mode=", in_intake_mode,
        "resume_after_answer=", resume_intake_after_answer,
        "text=", user_text[:80]
    )

    # =========================================================
    # Self-harm / suicide crisis guard
    # =========================================================
    if looks_like_self_harm_crisis(user_text):
        reply_text = build_self_harm_crisis_reply()

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "self_harm_crisis",
                "faq_match": False,
                "hide_booking_button": True,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # 911 / emergency-service confusion guard
    # =========================================================
    if looks_like_emergency_service_confusion(user_text):
        reply_text = build_emergency_service_confusion_reply()

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "emergency_service_confusion",
                "faq_match": False,
                "hide_booking_button": True,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Complaint / discrimination concern guard
    # =========================================================
    if looks_like_complaint_or_discrimination_concern(user_text):
        reply_text = build_complaint_or_discrimination_reply(office_phone)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "complaint_or_discrimination_concern",
                "faq_match": False,
                "hide_booking_button": True,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Legal threat / lawsuit concern guard
    # =========================================================
    if looks_like_legal_or_lawsuit_concern(user_text):
        reply_text = build_legal_or_lawsuit_reply(office_phone)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "legal_or_lawsuit_concern",
                "faq_match": False,
                "hide_booking_button": True,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Personal identity / relationship off-topic guard
    # =========================================================
    if looks_like_personal_identity_or_relationship_topic(user_text) and not in_intake_mode:
        reply_text = build_personal_identity_or_relationship_reply(user_text, office_phone)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "personal_identity_or_relationship_topic",
                "faq_match": False,
                "hide_booking_button": True,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Early question guard
    # =========================================================
    if looks_like_question_request(user_text) and not in_intake_mode:
        reply_text = "Sure — what’s your question?"
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={"mode": "question_guard", "faq_match": False, "show_start_over": show_start_over,},
        )
    
    # =========================================================
    # Friendly greeting / small-talk guard
    # =========================================================
    if looks_like_friendly_greeting_or_small_talk(user_text) and not in_intake_mode:
        reply_text = build_friendly_greeting_reply(user_text)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "friendly_greeting",
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )
    
    # =========================================================
    # Conversation ending guard
    # =========================================================
    explicit_done = looks_like_explicit_conversation_ending(user_text)
    simple_thanks_done = looks_like_simple_thanks(user_text) and not in_intake_mode
    ack_after_final = (
        looks_like_acknowledgement_after_final(user_text)
        and last_assistant_was_final_handoff(db, conversation.id)
    )

    if explicit_done or simple_thanks_done or ack_after_final:
        reply_text = build_conversation_ending_reply(conversation)

        lead_email_sent, lead_sms_sent, lead_email_error, lead_sms_error = finalize_and_notify_if_ready(
            db,
            client,
            conversation,
        )

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "conversation_ending",
                "faq_match": False,
                "lead_email_sent": bool(getattr(conversation, "lead_email_sent", False)),
                "lead_sms_sent": bool(getattr(conversation, "lead_sms_sent", False)),
                "lead_email_error": lead_email_error,
                "lead_sms_error": lead_sms_error,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Time-only outside-hours guard
    # =========================================================
    if looks_like_scheduling_intent(user_text) and time_only_request_is_outside_office_hours(client, user_text):
        reply_text = "That time is outside normal office hours. What day/time works better for you?"

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "time_only_outside_hours",
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Intake time-window capture guard
    # =========================================================
    if in_intake_mode and not time_window_is_complete(getattr(conversation, "lead_time_window", None)):
        canonical_tw = canonicalize_time_window_for_storage(client, user_text)

        if canonical_tw:
            time_issue_reply = build_time_window_issue_reply(client, canonical_tw)

            if time_issue_reply:
                reply_text = time_issue_reply
            else:
                conversation.lead_time_window = canonical_tw

                if hasattr(conversation, "lead_outside_hours_note"):
                    conversation.lead_outside_hours_note = None

                db.add(conversation)
                db.commit()
                db.refresh(conversation)

                reply_text = _next_intake_prompt(client, conversation)

            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
            db.commit()
            return ChatResponse(
                reply=reply_text,
                conversation_id=str(conversation.id),
                meta={
                    "mode": "intake_time_window_capture",
                    "faq_match": False,
                    "saved_time_window": getattr(conversation, "lead_time_window", None),
                    "show_start_over": show_start_over,
                },
            )


    # =========================================================
    # Pending time correction guard
    # =========================================================
    if last_assistant_requested_time_correction(db, conversation.id):
        detected_tw = canonicalize_time_window_for_storage(client, user_text)

        if looks_like_general_hours_request(user_text):
            hours_text = build_office_hours_answer(client) or "Please call the office for current hours."
            reply_text = f"{hours_text}\n\nWhat day/time works better for you?"

            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
            db.commit()
            return ChatResponse(
                reply=reply_text,
                conversation_id=str(conversation.id),
                meta={
                    "mode": "pending_time_hours_answer",
                    "faq_match": False,
                    "show_start_over": show_start_over,
                },
            )

        if detected_tw:
            time_issue_reply = build_time_window_issue_reply(client, detected_tw)

            if time_issue_reply:
                reply_text = time_issue_reply
            else:
                conversation.is_lead = True
                conversation.lead_time_window = detected_tw

                if hasattr(conversation, "lead_outside_hours_note"):
                    conversation.lead_outside_hours_note = None

                db.add(conversation)
                db.commit()
                db.refresh(conversation)

                reply_text = _next_intake_prompt(client, conversation)

            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
            db.commit()
            return ChatResponse(
                reply=reply_text,
                conversation_id=str(conversation.id),
                meta={
                    "mode": "pending_time_correction",
                    "faq_match": False,
                    "show_start_over": show_start_over,
                },
            )


    # =========================================================
    # Office phone request guard
    # =========================================================
    if looks_like_office_phone_request(user_text) and not looks_like_scheduling_intent(user_text):
        reply_text = build_office_phone_reply(client, conversation, office_phone)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "office_phone",
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Insurance request guard
    # =========================================================
    if looks_like_insurance_request(user_text) and not looks_like_scheduling_intent(user_text):
        reply_text = build_insurance_reply(user_text)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "insurance_info",
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Specific-day hours guard
    # =========================================================
    specific_hours_day = detect_specific_hours_day(user_text)
    if specific_hours_day and not looks_like_scheduling_intent(user_text):
        reply_text = build_specific_day_hours_answer(client, specific_hours_day)

        if reply_text:
            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
            db.commit()
            return ChatResponse(
                reply=reply_text,
                conversation_id=str(conversation.id),
                meta={
                    "mode": "specific_day_hours",
                    "faq_match": False,
                    "show_start_over": show_start_over,
                },
            )

    # =========================================================
    # Dangerous dental self-treatment guard
    # =========================================================
    if looks_like_dangerous_dental_instruction(user_text):
        is_true_emergency = looks_like_emergency(user_text) or looks_like_urgent_dental_safety_issue(user_text)

        conversation.is_lead = True
        conversation.lead_is_priority = bool(is_true_emergency)
        conversation.lead_is_emergency = bool(is_true_emergency)

        if not (conversation.lead_reason or "").strip():
            conversation.lead_reason = detect_appointment_reason(user_text) or "tooth pain"
            conversation.lead_reason_source_text = (user_text or "")[:120]

        db.add(conversation)
        db.commit()
        db.refresh(conversation)

        reply_text = build_dangerous_dental_self_treatment_reply(user_text)

        if is_true_emergency:
            reply_text = (
                f"{reply_text}\n\n"
                "If you have trouble breathing or swallowing, uncontrolled bleeding, or rapidly worsening swelling, "
                "please call 911 or go to the ER now.\n\n"
                f"Our office number is {office_phone}."
            )
            next_prompt = _next_emergency_prompt(conversation)
        else:
            next_prompt = _next_intake_prompt(client, conversation)

        if next_prompt and "?" not in reply_text:
            reply_text = f"{reply_text}\n\n{next_prompt}"

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "dangerous_dental_self_treatment_guard",
                "faq_match": False,
                "emergency_mode": bool(is_true_emergency),
                "hide_booking_button": True,
                "show_call_button": True,
                "call_phone": office_phone,
                "call_cta_label": "Call Office Now" if is_true_emergency else "Call Office",
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Urgent trauma / bleeding safety guard
    # =========================================================
    if looks_like_urgent_dental_safety_issue(user_text):
        conversation.is_lead = True
        conversation.lead_is_priority = True
        conversation.lead_is_emergency = True

        if not (conversation.lead_reason or "").strip():
            conversation.lead_reason = detect_appointment_reason(user_text) or "broken tooth/filling"
            conversation.lead_reason_source_text = (user_text or "")[:120]

        db.add(conversation)
        db.commit()
        db.refresh(conversation)

        reply_text = (
            "This may require prompt attention.\n\n"
            "If you have trouble breathing or swallowing, uncontrolled bleeding, or rapidly worsening swelling, "
            "please call 911 or go to the ER now.\n\n"
            f"Our office number is {office_phone}."
        )

        next_prompt = _next_emergency_prompt(conversation)

        if next_prompt:
            reply_text = f"{reply_text}\n\n{next_prompt}"

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "urgent_dental_safety_guard",
                "faq_match": False,
                "emergency_mode": True,
                "hide_booking_button": True,
                "show_call_button": True,
                "call_phone": office_phone,
                "call_cta_label": "Call Office Now",
                "show_start_over": show_start_over,
            },
        )


    # =========================================================
    # Pricing / cost question guard
    # =========================================================
    if looks_like_pricing_request(user_text):
        reply_text = build_pricing_reply(user_text)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "pricing_info",
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Service availability question guard
    # =========================================================
    if looks_like_service_question(user_text) and not has_any_lead_data and not accepted_schedule:
        reply_text = build_service_question_reply(user_text)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "service_question",
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Emergency routing FIRST
    # =========================================================
    if last_assistant_was_emergency(db, conversation.id) and _looks_like_affirmative(user_text):
        reply_text = _next_emergency_prompt(conversation)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "emergency_intake_continue",
                "faq_match": False,
                "emergency_mode": True,
                "show_call_button": True,
                "call_phone": office_phone,
                "call_cta_label": "Call Office Now",
                "show_start_over": show_start_over,
            }
        )

    if looks_like_emergency(user_text):
        default_during, default_after = get_emergency_defaults()

        accepts_emergencies = bool(getattr(client, "accepts_emergencies", True))
        accepts_walkins = bool(getattr(client, "accepts_walkins", False))
        policy = (getattr(client, "after_hours_policy", "") or "voicemail").strip()
        after_phone = (getattr(client, "after_hours_phone", "") or "").strip()
        show_after_phone = bool(getattr(client, "show_after_hours_phone", False))
        custom_emergency = (getattr(client, "custom_emergency_message", "") or "").strip()
        custom_after = (getattr(client, "custom_after_hours_message", "") or "").strip()

        after_hours = False

        if not accepts_emergencies:
            reply_text = (
                "I’m sorry — we may not be able to accommodate emergencies. "
                "If you have trouble breathing or swallowing, uncontrolled bleeding, or rapidly worsening swelling, "
                "call 911 or go to the ER now."
            )
        else:
            base_msg = custom_emergency or default_during

            if after_hours:
                if policy == "on_call" and after_phone:
                    after_msg = custom_after or "It’s after hours — please call our on-call number."
                    if show_after_phone:
                        after_msg += f"\nOn-call: {after_phone}"
                elif policy == "er_only":
                    after_msg = custom_after or (
                        "It’s after hours. If you have trouble breathing or swallowing, uncontrolled bleeding, "
                        "or rapidly worsening swelling, call 911 or go to the ER."
                    )
                elif policy == "custom":
                    after_msg = custom_after or default_after
                else:
                    after_msg = custom_after or default_after

                reply_text = f"{base_msg}\n\n{after_msg}"
            else:
                reply_text = base_msg
                if accepts_walkins:
                    reply_text += "\n\nWalk-ins may be available, but please call first so we can direct you."

            reply_text += "\n\n" + _next_emergency_prompt(conversation)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "emergency_booking_mode",
                "faq_match": False,
                "emergency_mode": True,
                "hide_booking_button": True,
                "show_call_button": True,
                "call_phone": office_phone,
                "call_cta_label": "Call Office Now",
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Urgent priority routing SECOND
    # =========================================================
    if looks_like_urgent_but_not_er(user_text) and not looks_like_emergency(user_text):
        if not getattr(conversation, "lead_is_priority", False):
            conversation.is_lead = True
            conversation.lead_is_priority = True

            if not (conversation.lead_reason or "").strip():
                conversation.lead_reason = "tooth pain"

            if not (getattr(conversation, "lead_time_window", None) or "").strip():
                conversation.lead_time_window = "ASAP"

            db.add(conversation)
            db.commit()
            db.refresh(conversation)

        reply_text = (
            "Got it — I’ll mark this as urgent. "
            "What’s your first name?"
        )

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()

        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "urgent_priority_lead",
                "faq_match": False,
                "show_call_button": True,
                "call_phone": office_phone,
                "call_cta_label": "Call Office Now",
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Emergency follow-up intake
    # =========================================================
    emergency_followup = last_assistant_was_emergency_prompt(db, conversation.id)

    if emergency_followup:
        updated = False

        phone = extract_phone(user_text)
        name = extract_name(user_text) or looks_like_name_only(user_text)
        reason = detect_appointment_reason(user_text)

        if name and not (conversation.lead_name or "").strip():
            conversation.lead_name = name
            conversation.lead_name_source_text = (user_text or "")[:120]
            updated = True

        if phone and not is_valid_phone(phone) and not (conversation.lead_phone or "").strip():
            reply_text = "That phone number doesn’t look valid. Please enter a valid 10-digit phone number."
            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
            db.commit()
            return ChatResponse(
                reply=reply_text,
                conversation_id=str(conversation.id),
                meta={
                    "mode": "invalid_phone",
                    "faq_match": False,
                    "show_start_over": show_start_over,
                },
            )

        if phone and is_valid_phone(phone) and not (conversation.lead_phone or "").strip():
            conversation.lead_phone = phone
            updated = True
            lead_captured_now = True

        if reason and not (conversation.lead_reason or "").strip():
            conversation.lead_reason = reason
            conversation.lead_reason_source_text = (user_text or "")[:120]
            updated = True

        if not (conversation.lead_reason or "").strip():
            conversation.lead_reason = "tooth pain"
            conversation.lead_reason_source_text = "emergency_followup"
            updated = True

        if not conversation.is_lead:
            conversation.is_lead = True
            updated = True

        if not getattr(conversation, "lead_is_priority", False):
            conversation.lead_is_priority = True
            updated = True

        if not getattr(conversation, "lead_is_emergency", False):
            conversation.lead_is_emergency = True
            updated = True

        if updated:
            db.add(conversation)
            db.commit()
            db.refresh(conversation)

        print("DEBUG:",
            conversation.lead_is_emergency,
            conversation.lead_name,
            conversation.lead_phone)

       # If we already have name + phone → STOP intake and send handoff message
        if (conversation.lead_name or "").strip() and (conversation.lead_phone or "").strip():

            print("🔥 EMERGENCY COMPLETION TRIGGERED")

            conversation.lead_status = "completed"
            lead_email_sent, lead_sms_sent, lead_email_error, lead_sms_error = notify_office_of_completed_lead(
            db,
            client,
            conversation,
        )

            staff_summary = build_staff_lead_summary(client, conversation)
            staff_sms = build_staff_lead_sms(client, conversation)

            print("[EMERGENCY_LEAD_SUMMARY]\n" + staff_summary)
            print("[EMERGENCY_LEAD_SMS]\n" + staff_sms)

            office_notify_email = (getattr(client, "notification_email", None) or "").strip()
            office_notify_phone = (getattr(client, "notification_phone", None) or "").strip()
            print("NOTIFY EMAIL:", office_notify_email)
            print("NOTIFY PHONE:", office_notify_phone)
            email_send_error = None
            sms_send_error = None

            try:
                if office_notify_email and not bool(getattr(conversation, "lead_email_sent", False)):
                    send_office_lead_email(
                        to_email=office_notify_email,
                        subject=f"URGENT emergency lead - {getattr(client, 'practice_name', 'Dental Office')}",
                        body_text=staff_summary,
                    )
                    conversation.lead_email_sent = True
                    print("✅ EMERGENCY EMAIL SENT")
            except Exception as e:
                email_send_error = str(e)
                print("[EMERGENCY_EMAIL_ERROR]", email_send_error)

            try:
                if office_notify_phone and not bool(getattr(conversation, "lead_sms_sent", False)):
                    send_office_lead_sms(
                        to_phone=office_notify_phone,
                        body=staff_sms,
                    )
                    conversation.lead_sms_sent = True
                    print("✅ EMERGENCY SMS SENT")
            except Exception as e:
                sms_send_error = str(e)
                print("[EMERGENCY_SMS_ERROR]", sms_send_error)

            db.add(conversation)
            db.commit()
            db.refresh(conversation)

            name = (conversation.lead_name or "").strip()
            name_part = f", {name}" if name else ""

            reply_text = (
                f"Thanks{name_part} — I’ve flagged this as urgent for the team.\n\n"
                "They’ll reach out shortly. If anything gets worse, please call us right away."
            )

            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
            db.commit()

            return ChatResponse(
                reply=reply_text,
                conversation_id=str(conversation.id),
                meta={
                    "mode": "emergency_handoff",
                    "faq_match": False,
                    "emergency_mode": True,
                    "hide_booking_button": True,
                    "show_call_button": True,
                    "call_phone": office_phone,
                    "call_cta_label": "Call Office Now",
                    "lead_email_sent": bool(getattr(conversation, "lead_email_sent", False)),
                    "lead_sms_sent": bool(getattr(conversation, "lead_sms_sent", False)),
                    "lead_email_error": email_send_error,
                    "lead_sms_error": sms_send_error,
                    "show_start_over": show_start_over,
                },
            )
        # Otherwise continue normal emergency intake
        next_prompt = _next_emergency_prompt(conversation)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=next_prompt))
        db.commit()

        return ChatResponse(
            reply=next_prompt,
            conversation_id=str(conversation.id),
            meta={
                "mode": "emergency_followup_intake",
                "faq_match": False,
                "emergency_mode": True,
                "hide_booking_button": True,
                "show_call_button": True,
                "call_phone": office_phone,
                "call_cta_label": "Call Office Now",
                "show_start_over": show_start_over,
            },
        )

    # =========================================================
    # Operational override
    # =========================================================
    faq = None
    t = _norm_text(user_text)
    tokens = set(t.split())

    booking_phrases = [
        "book", "booking", "schedule", "appointment", "consultation",
        "i want to book", "i want to schedule", "make an appointment",
    ]
    looks_like_booking_only = any(p in t for p in booking_phrases)

    is_location_intent = any(p in t for p in [
        "where are you", "where r you", "where are you located", "where is your office",
        "location", "address", "parking", "directions", "located"
    ])

    hours_phrases = [
        "hours", "office hours", "when are you open", "what time do you open",
        "what time do you close", "closing time", "opening time",
        "are you guys open", "are yall open", "r u guys open", "open til",
        "weekend", "weekends",
    ]
    is_hours_intent = any(p in t for p in hours_phrases)

    time_words = {
        "time", "today", "tomorrow",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    }
    if ("open" in tokens or "close" in tokens) and (tokens & time_words):
        is_hours_intent = True

    if in_intake_mode and detect_time_window(user_text):
        is_hours_intent = False

    insurance_words = {"insurance", "ppo", "hmo", "medicaid", "medicare"}
    insurance_phrases = {
        "in network", "in-network",
        "delta", "delta dental", "aetna", "cigna", "metlife", "guardian", "humana",
    }
    is_insurance_intent = bool(tokens & insurance_words) or any(p in t for p in insurance_phrases)

    if looks_like_booking_only:
        is_location_intent = False
        is_hours_intent = False
        is_insurance_intent = False

    is_operational = is_location_intent or is_hours_intent or is_insurance_intent

    if is_operational:
        faq = best_faq_match(db, client.id, user_text)

        if is_hours_intent:
            structured_hours = build_office_hours_answer(client)
            if structured_hours:
                op_reply = structured_hours
                meta = {"faq_match": False, "mode": "hours_structured"}
            elif faq:
                op_reply = (faq.answer or "").strip() or "Please call the office and our team can confirm our office hours."
                meta = {"faq_match": True, "faq_id": str(faq.id), "mode": "faq_operational"}
            else:
                op_reply = "Please call the office and our team can confirm our office hours."
                meta = {"faq_match": False, "mode": "faq_operational_no_match"}

        elif is_location_intent:
            if faq:
                op_reply = (faq.answer or "").strip() or "Please call the office and our team can share our location details."
                meta = {"faq_match": True, "faq_id": str(faq.id), "mode": "faq_operational"}
            else:
                op_reply = "Please call the office and our team can share our address and directions."
                meta = {"faq_match": False, "mode": "faq_operational_no_match"}

        elif is_insurance_intent:
            if faq:
                op_reply = (faq.answer or "").strip() or "Please call the office and our team can confirm insurance details."
                meta = {"faq_match": True, "faq_id": str(faq.id), "mode": "faq_operational"}
            else:
                op_reply = "Please call the office and our team can confirm insurance details."
                meta = {"faq_match": False, "mode": "faq_operational_no_match"}

        else:
            op_reply = "Please call the office and our team can share those details."
            meta = {"faq_match": False, "mode": "faq_operational_no_match"}

        lead_completed = (conversation.lead_status or "").strip().lower() == "completed"
        if (in_intake_mode or resume_intake_after_answer) and not lead_completed:
            op_reply = f"{op_reply}\n\n{_next_intake_prompt(client, conversation)}"

        db.add(Message(conversation_id=conversation.id, role="assistant", content=op_reply))
        db.commit()
        meta["show_start_over"] = show_start_over
        return ChatResponse(
            reply=op_reply,
            conversation_id=str(conversation.id),
            meta=meta,
        )

    # =========================================================
    # Greeting / trivia / off-topic
    # =========================================================
    if looks_like_greeting(user_text) and not in_intake_mode:
        reply_text = "Hi! I’m Mia — the virtual assistant for this dental office. How can I help you today?"
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "normal_reply",
                "show_start_over": show_start_over,
            }
        )

    if looks_like_random_trivia(user_text) and not in_intake_mode and not looks_like_info_intent(user_text):
        reply_text = (
            "I’m here to help with dental appointments and office information. "
            "Do you have a question about services, hours, insurance, or scheduling?"
        )
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "guard_trivia",
                "faq_match": False,
                "show_start_over": show_start_over,
            }
        )

    if not in_intake_mode:
        info_intent_quick = looks_like_info_intent(user_text)
        harmless_short = (
            len(_tokenize(user_text)) <= 2
            and not looks_like_random_trivia(user_text)
            and not looks_like_medical_advice(user_text)
            and not looks_like_emergency(user_text)
        )
        is_attack = is_abusive = is_hate = is_sexual = is_offtopic = False

        if not is_scheduling_now and not info_intent_quick and not harmless_short:
            guard = classify_message_guard_with_ai(user_text)
            is_attack = bool(guard.get("is_attack", False))
            is_abusive = bool(guard.get("is_abusive", False))
            is_hate = bool(guard.get("is_hate", False))
            is_sexual = bool(guard.get("is_sexual", False))
            is_offtopic = bool(guard.get("is_offtopic", False))

        if is_attack:
            record_abuse_strike(db, conversation)
            reply_text = "I can help with scheduling and office info only. Please rephrase your request."
            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
            db.commit()
            return ChatResponse(
                reply=reply_text,
                conversation_id=str(conversation.id),
                meta={"mode": "guard_attack", "faq_match": False, "strikes": int(conversation.abuse_strikes or 0),"show_start_over": show_start_over,},
            )

        if is_abusive or is_hate or is_sexual:
            record_abuse_strike(db, conversation)
            if conversation_is_locked(conversation):
                reply_text = (
                    "Please be respectful. I can’t continue this chat. "
                    f"Please call the office directly at {office_phone}."
                )
                mode = "guard_abuse_locked"
            else:
                reply_text = "Please keep messages respectful. I can help you schedule an appointment or answer office questions."
                mode = "guard_abuse_ai"

            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
            db.commit()
            return ChatResponse(
                reply=reply_text,
                conversation_id=str(conversation.id),
                meta={"mode": mode, "faq_match": False, "strikes": int(conversation.abuse_strikes or 0), "show_start_over": show_start_over,},
            )

        if is_offtopic:
            reply_text = (
                "I’m here to help with dental appointments and office info. "
                "Are you looking to schedule, or do you have a question about services, hours, insurance, or location?"
            )
            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
            db.commit()
            return ChatResponse(
                reply=reply_text,
                conversation_id=str(conversation.id),
                meta={
                    "mode": "guard_offtopic_ai",
                    "faq_match": False,
                    "show_start_over": show_start_over,
                }
            )

    decay_abuse_strikes(db, conversation)

    # =========================================================
    # ASAP yes/no follow-up
    # =========================================================
    if last_assistant_asked_asap_today_vs_tomorrow(db, conversation.id):
        tl = _norm_text(user_text)
     

        if tl in {"yes", "y", "yeah", "yep", "sure", "ok", "okay"} or tl.startswith("yes"):
            conversation.lead_time_window = "ASAP / tomorrow ok"

            name_from_text = extract_name(user_text) or looks_like_name_only(user_text)
            if name_from_text and not (conversation.lead_name or "").strip():
                conversation.lead_name = name_from_text
                conversation.lead_name_source_text = (user_text or "")[:120]

            np_flag = detect_new_patient_flag(user_text)
            if np_flag is not None and getattr(conversation, "lead_is_new_patient", None) is None:
                conversation.lead_is_new_patient = np_flag

            db.add(conversation)
            db.commit()
            db.refresh(conversation)

            next_prompt = _next_intake_prompt(client, conversation)
            reply_text = (
                "Great — we’ll mark you for the earliest available opening, including tomorrow if needed. "
                f"{next_prompt}"
            )

            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
            db.commit()

            return ChatResponse(
                reply=reply_text,
                conversation_id=str(conversation.id),
                meta={
                    "mode": "asap_confirmed",
                    "faq_match": False,
                    "show_start_over": show_start_over,
                }
            )

        if tl in {"no", "nope", "nah"} or tl.startswith("no"):
            reply_text = "No problem — what day or time works better for you?"
            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
            db.commit()
            return ChatResponse(
                reply=reply_text,
                conversation_id=str(conversation.id),
                meta={
                    "mode": "asap_declined_tomorrow",
                    "faq_match": False,
                    "show_start_over": show_start_over,
                }
            )

    # =========================================================
    # Deterministic extraction
    # =========================================================
    email = extract_email(user_text)
    phone = extract_phone(user_text)
    name = extract_name(user_text)

    # If user replied with compact "name + phone" format, capture the leftover text as name
    if not name and phone:
        if last_assistant_asked_for_name(db, conversation.id):
            compact_name = extract_name_from_name_phone_reply(user_text)
            if compact_name:
                name = compact_name

    if service_reason_now:
        question_mode = False

    if question_mode:
        reason = None
        service_reason = None
        np_flag = None
        email_opt_out = False
    else:
        reason = detect_appointment_reason(user_text)
        service_reason = service_reason_now
        np_flag = detect_new_patient_flag(user_text)
        email_opt_out = detect_email_opt_out(user_text)

    updated = False
    lead_captured_now = False
    service_selected_now = False

    if service_reason and service_reason != "other" and not (conversation.lead_reason or "").strip():
        conversation.lead_reason = service_reason
        updated = True
        lead_captured_now = True
        service_selected_now = True

        if not (getattr(conversation, "lead_reason_source_text", "") or "").strip():
            conversation.lead_reason_source_text = (user_text or "")[:120]
            updated = True

    # Once a service is selected, force intake mode to stay active
    if service_selected_now:
        in_intake_mode = True
        question_mode = False

    if email and email_domain_typo_suspected(email) and not (conversation.lead_email or "").strip():
        reply_text = "That email address doesn’t look quite right. Please enter it again, or type ‘skip’ to continue without email."
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "invalid_email",
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )

    if email and not email_domain_typo_suspected(email) and not (conversation.lead_email or "").strip():
        conversation.lead_email = email
        updated = True
        lead_captured_now = True

    raw_phone_digits = re.sub(r"\D", "", user_text or "")

    if (
        raw_phone_digits
        and not phone
        and not (conversation.lead_phone or "").strip()
        and last_assistant_asked_for_phone(db, conversation.id)
    ):
        reply_text = "That phone number doesn’t look valid. Please enter a 10-digit phone number."
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "invalid_phone",
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )

    if phone and not is_valid_phone(phone) and not (conversation.lead_phone or "").strip():
        reply_text = "That phone number doesn’t look valid. Please enter a 10-digit phone number."
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "invalid_phone",
                "faq_match": False,
                "show_start_over": show_start_over,
            },
        )

    if phone and is_valid_phone(phone) and not (conversation.lead_phone or "").strip():
        conversation.lead_phone = phone
        updated = True
        lead_captured_now = True

    if name and not (conversation.lead_name or "").strip():
        conversation.lead_name = name
        updated = True
        if not (getattr(conversation, "lead_name_source_text", "") or "").strip():
            conversation.lead_name_source_text = user_text[:120]
            updated = True

    can_capture_reason = bool(
        is_scheduling_now
        or service_reason_now
        or has_any_lead_data
    )

    if looks_like_info_intent(user_text) and not is_scheduling_now and not has_any_lead_data:
        can_capture_reason = False

    if can_capture_reason and reason and not (conversation.lead_reason or "").strip():
        conversation.lead_reason = reason
        updated = True
        if not (getattr(conversation, "lead_reason_source_text", "") or "").strip():
            conversation.lead_reason_source_text = user_text[:120]
            updated = True

    asked_for_name = last_assistant_asked_for_name(db, conversation.id)
    if (
        in_intake_mode
        and not question_mode
        and asked_for_name
        and not (conversation.lead_name or "").strip()
        and not service_reason_now
    ):
        name_only = looks_like_name_only(user_text)
        if name_only:
            conversation.lead_name = name_only
            updated = True
            if not (getattr(conversation, "lead_name_source_text", "") or "").strip():
                conversation.lead_name_source_text = user_text[:120]
                updated = True

    if np_flag is not None and getattr(conversation, "lead_is_new_patient", None) is None:
        conversation.lead_is_new_patient = np_flag
        updated = True

    last_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    last_text = (last_msg.content or "") if last_msg else ""

    tw_reply = None
    tw_updated = False
    tw_value = None
    outside_hours_reply = None
    bypass_stage = None

    if not asked_for_question:
        tw_reply, tw_updated = handle_time_window_capture(client, conversation, user_text, last_text)
        if tw_updated:
            updated = True
            lead_captured_now = True

    tw_value = (getattr(conversation, "lead_time_window", None) or "").strip()
    if tw_updated and tw_value:
        is_outside, outside_note = check_outside_hours(client, tw_value)

        if is_outside:
            if not bool(getattr(conversation, "lead_is_outside_hours", False)):
                conversation.lead_is_outside_hours = True
                updated = True

            if outside_note and (getattr(conversation, "lead_outside_hours_note", None) or "").strip() != outside_note:
                conversation.lead_outside_hours_note = outside_note
                updated = True

            outside_hours_behavior = get_client_setting(client, "outside_hours_behavior", "soft_note")
            if outside_hours_behavior == "soft_note":
                row = get_office_hours_struct(client).get(_get_day_key_from_time_window(tw_value), {}) or {}
                start = row.get("start")
                end = row.get("end")

                if start and end:
                    outside_hours_reply = (
                        f"Got it — we’re typically open {_format_time_label(start)}–{_format_time_label(end)}, "
                        "but I’ll still send this to the team for review."
                    )
                else:
                    outside_hours_reply = (
                        "Got it — that’s outside our usual office hours, but I’ll still send this to the team for review."
                    )
        else:
            if bool(getattr(conversation, "lead_is_outside_hours", False)):
                conversation.lead_is_outside_hours = False
                updated = True

            if (getattr(conversation, "lead_outside_hours_note", None) or "").strip():
                conversation.lead_outside_hours_note = None
                updated = True

    if email_opt_out and getattr(conversation, "lead_email_opt_out", False) is False:
        conversation.lead_email_opt_out = True
        updated = True

    if lead_captured_now and not conversation.is_lead:
        conversation.is_lead = True
        conversation.last_lead_at = db.execute(text("select now()")).scalar()
        updated = True

    if updated:
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    if tw_reply or outside_hours_reply:
        combined_reply = tw_reply

        if outside_hours_reply:
            if combined_reply:
                combined_reply = f"{combined_reply}\n\n{outside_hours_reply}"
            else:
                combined_reply = outside_hours_reply

        # ONE-QUESTION-PER-MESSAGE RULE:
        # If the time-window handler already asked a question, do not append
        # another intake question underneath it. This prevents messages like:
        # "Are you a new or returning patient?" + "Are you a new patient?"
        next_prompt = _next_intake_prompt(client, conversation)

        if priority_intake_is_complete(conversation):
            next_prompt = None

        if next_prompt:
            combined_norm = (combined_reply or "").strip().lower()
            next_norm = (next_prompt or "").strip().lower()
            combined_has_question = "?" in (combined_reply or "")

            if next_norm and next_norm != combined_norm and not combined_has_question:
                if combined_reply:
                    combined_reply = f"{combined_reply}\n\n{next_prompt}"
                else:
                    combined_reply = next_prompt

        lead_email_error = None
        lead_sms_error = None

        if (
            priority_intake_is_complete(conversation)
            and (conversation.lead_status or "").strip().lower() != "completed"
        ):
            print("✅ PRIORITY TIME WINDOW HANDOFF NOTIFY TRIGGERED")

            conversation.lead_status = "completed"
            db.add(conversation)
            db.commit()
            db.refresh(conversation)

            lead_email_sent, lead_sms_sent, lead_email_error, lead_sms_error = notify_office_of_completed_lead(
                db,
                client,
                conversation,
            )
        else:
            lead_email_sent = bool(getattr(conversation, "lead_email_sent", False))
            lead_sms_sent = bool(getattr(conversation, "lead_sms_sent", False))


        db.add(Message(conversation_id=conversation.id, role="assistant", content=combined_reply))
        db.commit()

        return ChatResponse(
            reply=combined_reply,
            conversation_id=str(conversation.id),
            meta={
                "mode": "time_window_capture",
                "faq_match": False,
                "lead_email_sent": bool(getattr(conversation, "lead_email_sent", False)),
                "lead_sms_sent": bool(getattr(conversation, "lead_sms_sent", False)),
                "lead_email_error": lead_email_error,
                "lead_sms_error": lead_sms_error,
                "show_start_over": show_start_over,
            }
        )

        # =========================================================
    # External booking / calendar handoff
    # =========================================================
    active_service_reason = (
        (conversation.lead_reason or "").strip()
        or service_reason_now
        or reason
    )
    if (
        has_external_booking(client)
        and not bool(getattr(conversation, "booking_link_sent", False))
        and (is_scheduling_now or service_reason_now or active_service_reason)
    ):
        capture_first = should_capture_before_booking_link(
            client=client,
            conversation=conversation,
            user_text=user_text,
            service_reason=active_service_reason,
        )

        if capture_first:
            capture_prompt = next_booking_capture_prompt(
                conversation,
                service_reason=active_service_reason,
            )

            print(
                "[BOOKING_CAPTURE]",
                "lead_name=", repr(conversation.lead_name),
                "lead_phone=", repr(conversation.lead_phone),
                "capture_prompt=", repr(capture_prompt),
            )

            if capture_prompt:
                db.add(Message(conversation_id=conversation.id, role="assistant", content=capture_prompt))
                db.commit()

                return ChatResponse(
                    reply=capture_prompt,
                    conversation_id=str(conversation.id),
                    meta={
                        "mode": "booking_capture_first",
                        "faq_match": False,
                        "show_start_over": show_start_over,
                    },
                )

        handoff_reply = build_booking_handoff_reply(
            client=client,
            conversation=conversation,
            service_reason=active_service_reason,
        )

        conversation.booking_link_sent = True
        db.add(conversation)
        db.add(Message(conversation_id=conversation.id, role="assistant", content=handoff_reply))
        db.commit()
        db.refresh(conversation)

        handoff_meta = build_booking_handoff_meta(client, active_service_reason)
        handoff_meta["show_start_over"] = show_start_over

        return ChatResponse(
            reply=handoff_reply,
            conversation_id=str(conversation.id),
            meta=handoff_meta,
        )

    # =========================================================
    # Pause intake for question request
    # =========================================================
    if in_intake_mode and looks_like_question_request(user_text) and not is_scheduling_now:
        reply_text = "Sure — what’s your question?\n\n(We can finish booking right after.)"
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "mode": "pause_intake",
                "faq_match": False,
                "show_start_over": show_start_over,
            }
        )

    # =========================================================
    # AI extraction
    # =========================================================
    allow_ai_extraction = bool(
        in_intake_mode
        or is_scheduling_now
        or service_reason_now
        or has_any_lead_data
    )

    need_ai_name = allow_ai_extraction and not (conversation.lead_name or "").strip()
    need_ai_reason = allow_ai_extraction and not (conversation.lead_reason or "").strip()

    if need_ai_name or need_ai_reason:
        try:
            data = extract_lead_fields_with_ai(user_text)
            ai_name_raw = data.get("lead_name")
            ai_reason_raw = data.get("lead_reason")
            ai_name_ev_raw = data.get("lead_name_source_text")
            ai_reason_ev_raw = data.get("lead_reason_source_text")

            ai_name_evidence = _safe_substring_evidence(user_text, ai_name_ev_raw)
            ai_reason_evidence = _safe_substring_evidence(user_text, ai_reason_ev_raw)

            ai_name = _validate_lead_name(ai_name_raw) if ai_name_evidence else None
            ai_reason = _validate_lead_reason(ai_reason_raw) if ai_reason_evidence else None

            ai_updated = False
            if ai_name and need_ai_name:
                conversation.lead_name = ai_name
                ai_updated = True
                if not (getattr(conversation, "lead_name_source_text", "") or "").strip():
                    conversation.lead_name_source_text = ai_name_evidence
                    ai_updated = True

            if ai_reason and need_ai_reason:
                conversation.lead_reason = ai_reason
                ai_updated = True
                if not (getattr(conversation, "lead_reason_source_text", "") or "").strip():
                    conversation.lead_reason_source_text = ai_reason_evidence
                    ai_updated = True

            if ai_updated:
                db.add(conversation)
                db.commit()
                db.refresh(conversation)
        except Exception as e:
            print("EXTRACTOR ERROR:", repr(e))
            traceback.print_exc()

    # =========================================================
    # FAQ match
    # =========================================================
    if (not in_intake_mode) and (not is_scheduling_now) and (not looks_like_scheduling_intent(user_text)):
        if faq is None:
            faq = best_faq_match(db, client.id, user_text)
        if faq:
            reply = (faq.answer or "").strip() or "Thanks! Please call the office and our team will confirm that for you."

            try:
                event = FAQEvent(
                    client_id=client.id,
                    faq_id=faq.id,
                    conversation_id=conversation.id,
                    user_text=user_text,
                )
                db.add(event)
                db.commit()
            except Exception as e:
                print("FAQ EVENT ERROR:", repr(e))
                traceback.print_exc()

            db.add(Message(conversation_id=conversation.id, role="assistant", content=reply))
            db.commit()
            return ChatResponse(
                reply=reply,
                conversation_id=str(conversation.id),
                meta={
                    "faq_match": True,
                    "faq_id": str(faq.id),
                    "mode": "faq",
                    "show_start_over": show_start_over,
                }
            )

    # =========================================================
    # Info-intent fallback
    # =========================================================
    if (
        not in_intake_mode
        and looks_like_info_intent(user_text)
        and not looks_like_scheduling_intent(user_text)
    ):
        t = _norm_text(user_text)
        operational_intents = [
            "hours", "open", "close", "what time", "when are you open",
            "insurance", "insurances", "do you take",
            "location", "address", "where are you", "parking",
            "phone", "phone number", "fax", "email", "contact", "website",
            "book online", "zocdoc",
        ]

        if not any(k in t for k in operational_intents):
            if any(p in t for p in ["services", "do you offer", "what services"]):
                reply_text = (
                    "We offer cleanings/exams, fillings, crowns, whitening, braces/Invisalign, "
                    "and extractions/implants. Which service are you interested in?"
                )
                db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
                db.commit()
                return ChatResponse(
                    reply=reply_text,
                    conversation_id=str(conversation.id),
                    meta={"faq_match": False, "mode": "info_services_list", "show_start_over": show_start_over,},
                )

    # =========================================================
    # Medical advice safety guard
    # =========================================================
    if looks_like_medical_advice(user_text):
        reply_text = (
            "I can’t provide medical advice in chat. "
            "If you’re in pain, the safest next step is to call the office so a clinician can guide you. "
            "If symptoms are severe (swelling, fever, trouble breathing/swallowing), please seek urgent care."
        )
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={"mode": "safety_guard", "faq_match": False, "show_start_over": show_start_over,},
        )

    # =========================================================
    # Lead completion + deterministic intake continuation
    # =========================================================
    is_thanks = (
        t_lower in {"thanks", "thank you", "thanx", "thx", "ty", "thanks!"}
        or "thank you" in t_lower
        or t_lower.startswith("thanks")
    )

    lead_capture_complete = (
        bool((conversation.lead_reason or "").strip())
        and bool((conversation.lead_name or "").strip())
        and bool((conversation.lead_phone or "").strip())
        and time_window_is_complete(getattr(conversation, "lead_time_window", None))
        and (getattr(conversation, "lead_is_new_patient", None) is not None)
        and (
            bool((conversation.lead_email or "").strip())
            or bool(getattr(conversation, "lead_email_opt_out", False))
        )
    )

    emergency_lead_complete = (
        bool(getattr(conversation, "lead_is_emergency", False))
        and bool((conversation.lead_name or "").strip())
        and bool((conversation.lead_phone or "").strip())
    )

    

    last_assistant_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    last_assistant_text = (last_assistant_msg.content or "") if last_assistant_msg else ""

    if (conversation.lead_status or "").strip().lower() == "completed":
        post_text = _norm_text(user_text)
        practice_name = getattr(client, "practice_name", None) or "our office"

        if any(x in post_text for x in ["thanks", "thank you", "thx", "ty"]):
            reply_text = f"Thank you for choosing {practice_name}. Have a great day!"
            should_close = True

        elif any(x in post_text for x in ["nothing", "thats all", "that s all", "all set", "all good"]):
            reply_text = f"You’re all set. Thank you for choosing {practice_name}. Have a great day!"
            should_close = True

        elif any(x in post_text for x in ["ok", "okay", "got it", "sounds good"]):
            reply_text = f"Thank you for choosing {practice_name}. Have a great day!"
            should_close = True

        else:
            reply_text = f"Thank you for choosing {practice_name}. Have a great day!"
            should_close = True

        if should_close and not bool(getattr(conversation, "final_closed", False)):
            conversation.final_closed = True
            db.add(conversation)
            db.commit()
            db.refresh(conversation)

        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()

        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "faq_match": False,
                "mode": "post_completion_polite",
                "show_start_over": show_start_over,
            },
        )

    if lead_capture_complete and (conversation.lead_status or "").strip().lower() != "completed":
        conversation.lead_status = "completed"
        lead_email_sent, lead_sms_sent, lead_email_error, lead_sms_error = notify_office_of_completed_lead(
            db,
            client,
            conversation,
        )

        staff_summary = build_staff_lead_summary(client, conversation)
        staff_sms = build_staff_lead_sms(client, conversation)

        print("[LEAD_SUMMARY]\n" + staff_summary)
        print("[LEAD_SMS]\n" + staff_sms)

        office_notify_email = (getattr(client, "notification_email", None) or "").strip()
        office_notify_phone = (getattr(client, "notification_phone", None) or "").strip()

        print("NORMAL NOTIFY EMAIL:", office_notify_email)
        print("NORMAL NOTIFY PHONE:", office_notify_phone)

        email_send_error = None
        sms_send_error = None

        try:
            if office_notify_email and not bool(getattr(conversation, "lead_email_sent", False)):
                send_office_lead_email(
                    to_email=office_notify_email,
                    subject=f"New appointment request - {getattr(client, 'practice_name', 'Dental Office')}",
                    body_text=staff_summary,
                )
                conversation.lead_email_sent = True
                print("✅ NORMAL EMAIL SENT")
        except Exception as e:
            email_send_error = str(e)
            print("[NORMAL_EMAIL_ERROR]", email_send_error)

        try:
            if office_notify_phone and not bool(getattr(conversation, "lead_sms_sent", False)):
                send_office_lead_sms(
                    to_phone=office_notify_phone,
                    body=staff_sms,
                )
                conversation.lead_sms_sent = True
                print("✅ NORMAL SMS SENT")
        except Exception as e:
            sms_send_error = str(e)
            print("[NORMAL_SMS_ERROR]", sms_send_error)

        db.add(conversation)
        db.commit()
        db.refresh(conversation)

        name = (conversation.lead_name or "").strip()
        name_part = f" {name}" if name else ""

        reply_text = f"Thanks{name_part}! We’ve got your request—our team will contact you shortly to confirm the appointment time."
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()

        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={
                "faq_match": False,
                "mode": "lead_complete",
                "lead_email_sent": bool(getattr(conversation, "lead_email_sent", False)),
                "lead_sms_sent": bool(getattr(conversation, "lead_sms_sent", False)),
                "lead_email_error": email_send_error,
                "lead_sms_error": sms_send_error,
                "show_start_over": show_start_over,
            },
        )

    if (
        in_intake_mode
        and not lead_capture_complete
        and not emergency_lead_complete
        and not is_thanks
        and not question_mode
        and not bool(getattr(conversation, "lead_is_emergency", False))
    ):
        if service_reason_now == "other" and not (conversation.lead_reason or "").strip():
            bypass_text = "Got it — can you briefly tell me what you need help with?"
            bypass_stage = "reason_detail"
        else:
            bypass_text, bypass_stage = receptionist_bypass_reply(conversation)

            if not bypass_text:
                bypass_text = (
                    "What can we help you with today—cleaning, tooth pain, fillings, crowns, "
                    "braces/Invisalign, whitening, or something else?"
                )
                bypass_stage = "reason"

        # If we were waiting for a free-text reason after "other", safely map it
        last_assistant_norm = _norm_text(last_assistant_text)
        if (
            bypass_stage == "reason_detail"
            and not (conversation.lead_reason or "").strip()
            and "can you briefly tell me what you need help with" in last_assistant_norm
        ):
            mapped_reason = map_reason_detail_to_enum(user_text)

            if mapped_reason:
                conversation.lead_reason = mapped_reason
                if not (getattr(conversation, "lead_reason_source_text", "") or "").strip():
                    conversation.lead_reason_source_text = user_text[:120]
                db.add(conversation)
                db.commit()
                db.refresh(conversation)

                # Re-enter bypass flow now that reason is safely captured
                bypass_text, bypass_stage = receptionist_bypass_reply(conversation)
            else:
                bypass_text = (
                    "Please briefly describe the issue using plain words only, like "
                    "'chipped tooth', 'tooth pain', or 'consultation'."
                )
                bypass_stage = "reason_detail"

        if bypass_text:
            lead_email_error = None
            lead_sms_error = None

            if (
                bypass_stage == "complete"
                and priority_intake_is_complete(conversation)
                and (conversation.lead_status or "").strip().lower() != "completed"
            ):
                print("✅ PRIORITY BYPASS HANDOFF NOTIFY TRIGGERED")

                conversation.lead_status = "completed"
                db.add(conversation)
                db.commit()
                db.refresh(conversation)

                lead_email_sent, lead_sms_sent, lead_email_error, lead_sms_error = notify_office_of_completed_lead(
                    db,
                    client,
                    conversation,
                )

            db.add(Message(conversation_id=conversation.id, role="assistant", content=bypass_text))
            db.commit()

        meta = {
            "faq_match": False,
            "mode": "bypass",
            "show_start_over": show_start_over,
            "show_service_menu": bypass_stage == "reason",
        }

        if bypass_stage == "time_window":
            hours_text = (
                get_office_hours_hint(db, client.id)
                or "Mon–Fri 9am–5pm, Sat 9am–1pm. Closed Sundays."
            )
            tz = getattr(client, "timezone", None)
            meta["hours_hint"] = f"{hours_text} (Hours shown in {tz})" if tz else hours_text

        return ChatResponse(
            reply=bypass_text,
            conversation_id=str(conversation.id),
            meta=meta,
        )

    if (
        t_lower in {"i have a question", "question"}
        and not is_scheduling_intent(user_text)
        and not looks_like_info_intent(user_text)
        and not in_intake_mode
    ):
        reply_text = "Sure — what’s your question?"
        db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            conversation_id=str(conversation.id),
            meta={"faq_match": False, "mode": "question_guard", "show_start_over": show_start_over,},
        )

    # =========================================================
    # OpenAI fallback
    # =========================================================
    start = time.time()
    try:
        context_messages = build_context_messages(db, conversation.id)
        lead_context = build_lead_context(conversation)

        openai_input: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if lead_context:
            openai_input.append(lead_context)
        openai_input.extend(context_messages[-MAX_CONTEXT_MESSAGES:])

        response = ai.responses.create(
            model=CHAT_MODEL,
            input=openai_input,
            max_output_tokens=120,
        )
        reply_text = (response.output_text or "").strip()
        if not reply_text:
            reply_text = "Sure—what can I help with?"

    except Exception as e:
        print("OPENAI ERROR:", repr(e))  # log the real OpenAI error in Render/local console
        traceback.print_exc()  # print full traceback for debugging

        reply_text = (  # friendly message shown to the user instead of ugly JSON
            "I’m sorry, I had trouble processing that. "
            "I can still help you schedule an appointment. "
            "What phone number should the office use to follow up?"
        )  # safe fallback reply

    db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))
    db.commit()

    elapsed_ms = int((time.time() - start) * 1000)
    print(f"[CHAT] ip={ip} client={client.practice_name} conv={conversation.id} ms={elapsed_ms}")

    return ChatResponse(
        reply=reply_text,
        conversation_id=str(conversation.id),
        meta={"faq_match": False, "mode": "ai","show_start_over": show_start_over,},
    )