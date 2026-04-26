from sqlalchemy import Column, String, Boolean, Text, DateTime, ForeignKey, text, Integer  # SQLAlchemy column types + text()
from sqlalchemy.dialects.postgresql import UUID, JSONB  # PostgreSQL UUID type
from sqlalchemy.orm import relationship  # ORM relationship helper
import uuid  # UUID generator for primary keys
from app.database import Base  # Declarative Base for SQLAlchemy models


# -------------------------
# CLIENT (Dental Office)
# -------------------------
class Client(Base):
    __tablename__ = "clients"  # Table name

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # PK UUID
    practice_name = Column(String, nullable=False)  # Practice name (display)
    api_key = Column(String, unique=True, nullable=False)  # Key used by website widget
    active = Column(Boolean, default=True)  # Enable/disable practice
    created_at = Column(DateTime(timezone=True), server_default=text("now()"))  # DB timestamp
    office_hours = Column(JSONB, nullable=True)
    settings = Column(JSONB, nullable=True)

    # Relationships
    conversations = relationship("Conversation", back_populates="client")  # Client -> many conversations
    faqs = relationship("ClientFAQ", back_populates="client")  # Client -> many FAQs
    


# -------------------------
# CONVERSATION (Chat session)
# -------------------------
class Conversation(Base):
    __tablename__ = "conversations"  # Table name

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # Conversation UUID
    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False)  # Owning client
    visitor_id = Column(String)  # Visitor/session id from frontend (optional)
    lead_email_sent = Column(Boolean, default=False)
    lead_sms_sent = Column(Boolean, default=False)
    # -----------------------------
    # LEAD TRACKING FIELDS (Week 2)
    # -----------------------------
    is_lead = Column(Boolean, default=False)  # True if phone/email captured
    last_lead_at = Column(DateTime(timezone=True))  # When lead captured
    lead_email = Column(String)  # Captured email
    lead_phone = Column(String)  # Captured phone
    lead_name = Column(String)  # Captured name
    lead_reason = Column(String)  # Reason category
    lead_status = Column(String, nullable=False, server_default="new", default="new")  # Workflow status
    lead_is_outside_hours = Column(Boolean, nullable=False, server_default="false", default=False)
    lead_outside_hours_note = Column(Text, nullable=True)
    
    # Evidence (audit/debug) — exact substring from user message
    lead_name_source_text = Column(String, nullable=True)
    lead_reason_source_text = Column(String, nullable=True)

     # NEW fields (scheduling plus opt out)
    lead_is_new_patient = Column(Boolean, nullable=True)  # None = unknown, True = new, False = returning
    lead_time_window = Column(String, nullable=True)  # e.g. "Tue morning", "next week afternoons"
    lead_email_opt_out = Column(Boolean, nullable=False, server_default="false", default=False)  # user refused email
    lead_is_priority = Column(Boolean, nullable=False, server_default="false", default=False)
    lead_is_emergency = Column(Boolean, nullable=False, server_default="false", default=False)
    final_closed = Column(Boolean, nullable=False, server_default="false", default=False)
    booking_link_sent = Column(Boolean, nullable=False, server_default="false", default=False)
    # -----------------------------  # comment
    # ABUSE / SPAM GUARD RAILS  # comment
    # -----------------------------  # comment
    abuse_strikes = Column(Integer, nullable=False, server_default="0", default=0)  # Counts abusive/offensive messages
    abuse_locked_until = Column(DateTime(timezone=True), nullable=True)  # Temporary lockout timestamp


    created_at = Column(DateTime(timezone=True), server_default=text("now()"))  # DB timestamp

    # Relationships
    client = relationship("Client", back_populates="conversations")  # Conversation -> client
    messages = relationship("Message", back_populates="conversation")  # Conversation -> many messages
    faq_events = relationship("FAQEvent", back_populates="conversation")  # Conversation -> FAQ hit events


# -------------------------
# MESSAGE (Individual chat line)
# -------------------------
class Message(Base):
    __tablename__ = "messages"  # Table name

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # PK
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False)  # Parent conversation
    role = Column(String, nullable=False)  # "user" or "assistant"
    content = Column(String, nullable=False)  # Message text
    created_at = Column(DateTime(timezone=True), server_default=text("now()"))  # DB timestamp

    # Relationship
    conversation = relationship("Conversation", back_populates="messages")  # Message -> conversation


# -------------------------
# CLIENT FAQ (Per-office FAQ entries)
# -------------------------
class ClientFAQ(Base):
    __tablename__ = "client_faqs"  # Table name

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # PK
    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False)  # Owning client
    question = Column(String, nullable=False)  # Canonical question
    answer = Column(String, nullable=False)  # Answer to return
    keywords = Column(String)  # Optional comma-separated keywords
    enabled = Column(Boolean, default=True)  # Can disable without deleting
    created_at = Column(DateTime(timezone=True), server_default=text("now()"))  # DB timestamp

    # Relationships
    client = relationship("Client", back_populates="faqs")  # FAQ -> client
    events = relationship("FAQEvent", back_populates="faq")  # FAQ -> many hit events


# -------------------------
# FAQ EVENT (Analytics: when a FAQ was used)
# -------------------------
class FAQEvent(Base):
    __tablename__ = "faq_events"  # Table name

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # PK

    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False)  # Which client/office
    faq_id = Column(UUID(as_uuid=True), ForeignKey("client_faqs.id"), nullable=False)  # Which FAQ matched
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True)  # Which conversation (optional)

    user_text = Column(Text, nullable=False)  # The user message that triggered FAQ
    created_at = Column(DateTime(timezone=True), server_default=text("now()"), nullable=False)  # When event happened

    # Relationships (optional but useful)
    client = relationship("Client")  # Event -> client
    faq = relationship("ClientFAQ", back_populates="events")  # Event -> faq
    conversation = relationship("Conversation", back_populates="faq_events")  # Event -> conversation
