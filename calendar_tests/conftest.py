# calendar_tests/conftest.py
#
# Shared fixtures for the DATABASE tests (test_booking_db.py).
#
# Prerequisites (documented, not assumed — Rule 4):
#   1. `pip install pytest sqlalchemy psycopg2-binary` in your venv.
#   2. A THROWAWAY Postgres database (never Supabase production!) whose NAME
#      contains "test", reachable on localhost, e.g.:
#        docker run -d -p 5433:5432 -e POSTGRES_PASSWORD=test \
#            -e POSTGRES_DB=mia_calendar_test postgres:16
#   3. The models.py patch from docs/INTEGRATION.md applied (booking_* columns).
#   4. Run:
#        ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes \
#        TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/mia_calendar_test \
#        pytest calendar_tests/ -v
#
# Without TEST_DATABASE_URL every DB test SKIPS (visibly — never silently
# passes). Tables are created fresh with Base.metadata.create_all from the
# ORM models. NOTE: create_all does NOT execute the SQL migration files —
# migration/ORM schema agreement is proven separately by
# test_migration_schema.py, which runs the actual migration SQL.
#
# DESTRUCTIVE-TEST SAFETY (Patch 1 — Senior Audit Critical #9):
# The engine fixture DROPS ALL TABLES when the session ends. To make it
# impossible for one mistyped environment variable to point that at a real
# database, the fixture refuses to run unless ALL of the following hold:
#   - the host is localhost / 127.0.0.1 / ::1
#   - the database NAME contains "test"
#   - ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes is set
# Violations FAIL loudly with the reason — never a silent pass, never a drop.

import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

# Hosts we accept as provably local. Anything else could be Supabase or some
# other shared server, so it is rejected outright.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def validate_disposable_test_db(url: str) -> "str | None":
    """
    Purpose: Decide whether a database URL is safe for DESTRUCTIVE test use
             (create_all / drop_all / raw migration runs).
    Inputs:  the TEST_DATABASE_URL string.
    Returns: None when the URL passes every safeguard; otherwise a human-
             readable explanation of exactly which safeguard failed.
    Database effects: none (string inspection only).
    Possible failures: malformed URLs simply fail the host check.

    Also imported by test_migration_schema.py so the migration runner uses
    the SAME safeguards (Rule 3 — one owner for this rule).
    """
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    database_name = parsed.path.lstrip("/")
    if host not in _LOCAL_HOSTS:
        return (
            f"host {host!r} is not localhost — destructive tests only run "
            "against a local throwaway PostgreSQL, never a shared server"
        )
    if "test" not in database_name.lower():
        return (
            f"database name {database_name!r} does not contain 'test' — "
            "name the throwaway database e.g. mia_calendar_test"
        )
    if os.environ.get("ALLOW_DESTRUCTIVE_CALENDAR_TESTS", "").strip().lower() != "yes":
        return (
            "ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes is not set — set it "
            "explicitly to confirm this database may be created and dropped"
        )
    return None

# app.config refuses to start without these; give it harmless values BEFORE
# any app import. The OpenAI key is never used by calendar code.
os.environ.setdefault("OPENAI_API_KEY", "test-not-used")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
if TEST_DB_URL:
    os.environ["DATABASE_URL"] = TEST_DB_URL

requires_db = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — DB tests skipped"
)


def sanitized_db_target(url: str) -> str:
    """
    Purpose: Describe a database URL for error messages WITHOUT exposing
             credentials — TEST_DATABASE_URL contains a password, and full
             URLs printed by pytest end up in logs.
    Returns: "host=<host> database=<name>" only.
    """
    parsed = urlsplit(url)
    return f"host={parsed.hostname!r} database={parsed.path.lstrip('/')!r}"


@pytest.fixture(scope="session")
def engine():
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    # SAFETY GATE: this fixture ends by DROPPING EVERY TABLE. Refuse to touch
    # any database that is not provably a local, explicitly-approved throwaway
    # (Senior Audit Critical #9). Failing (not skipping silently past setup)
    # makes the misconfiguration impossible to miss.
    unsafe_reason = validate_disposable_test_db(TEST_DB_URL)
    if unsafe_reason is not None:
        pytest.fail(
            f"REFUSING destructive database tests: {unsafe_reason}. "
            f"({sanitized_db_target(TEST_DB_URL)})",
            pytrace=False,
        )
    from app.database import Base, engine as app_engine
    import app.models  # noqa: F401  registers clients/conversations/etc.
    import app.calendar_models  # noqa: F401  registers calendar tables
    Base.metadata.create_all(bind=app_engine)
    yield app_engine
    Base.metadata.drop_all(bind=app_engine)


@pytest.fixture()
def db(engine):
    from app.database import SessionLocal
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def client_row(db):
    """One dental office with booking enabled and staff-confirmation ON."""
    from app.models import Client
    client = Client(
        id=uuid.uuid4(),
        practice_name="Test Dental",
        api_key=f"key-{uuid.uuid4()}",
        active=True,
        settings={
            "timezone": "America/New_York",
            "calendar": {
                "booking_enabled": True,
                "hold_minutes": 5,
                "minimum_notice_minutes": 60,
                "max_offered_slots": 3,
                "max_booking_days": 30,
                "require_staff_confirmation": True,
            },
        },
        notification_email=None,   # notification channels intentionally
        notification_phone=None,   # unconfigured; outcomes still recorded
    )
    db.add(client)
    db.commit()
    return client


@pytest.fixture()
def conversation_row(db, client_row):
    """A conversation whose intake is already complete (name + phone)."""
    from app.models import Conversation
    conversation = Conversation(
        id=uuid.uuid4(),
        client_id=client_row.id,
        lead_name="Kevin Alvarado",
        lead_phone="516-555-1234",
        lead_reason="cleaning/checkup",
        is_lead=True,
    )
    db.add(conversation)
    db.commit()
    return conversation


def make_conversation(db, client_row, name="Second Patient", phone="516-555-9999"):
    """Helper for two-patient race tests."""
    from app.models import Conversation
    conversation = Conversation(
        id=uuid.uuid4(),
        client_id=client_row.id,
        lead_name=name,
        lead_phone=phone,
        is_lead=True,
    )
    db.add(conversation)
    db.commit()
    return conversation
