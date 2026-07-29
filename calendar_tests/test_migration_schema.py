# calendar_tests/test_migration_schema.py
#
# Runs the ACTUAL SQL migration files (001 then 002) against a disposable
# local PostgreSQL — Base.metadata.create_all() builds the schema from the
# ORM and never executes these files, so without this test, migration/ORM
# drift is invisible (Senior Audit Critical #9, second half).
#
# HOW THE SQL IS EXECUTED (approved Patch 1 decision #4):
#   Each migration file is sent to PostgreSQL AS ONE COMPLETE SCRIPT through
#   the existing SQLAlchemy engine's DBAPI connection (exec_driver_sql).
#   psycopg2's simple-query protocol executes multi-statement scripts, and
#   the file's own BEGIN/COMMIT controls the transaction (the connection is
#   AUTOCOMMIT so nothing wraps or splits the script). No naive semicolon
#   splitting is performed anywhere.
#
# ISOLATION:
#   Everything happens inside a dedicated throwaway SCHEMA
#   (calendar_migration_test) in the disposable test database, created fresh
#   and dropped with CASCADE afterward. The same destructive-test safeguards
#   as conftest.py apply (localhost + 'test' in db name + explicit flag).
#
# REQUIREMENTS: PostgreSQL 13+ (gen_random_uuid() built in).

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import (  # noqa: E402
    TEST_DB_URL,
    requires_db,
    sanitized_db_target,
    validate_disposable_test_db,
)

pytestmark = requires_db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
SCHEMA = "calendar_migration_test"

UP_001 = "001_calendar_mvp_up.sql"
UP_002 = "002_calendar_integrity_hardening_up.sql"
DOWN_002 = "002_calendar_integrity_hardening_down.sql"

CONVERSATION_INDEX = "uq_active_appointment_per_conversation"
SLOT_INDEX = "uq_active_appointment_per_slot"


def _run_migration_file(connection, filename: str) -> None:
    """
    Purpose: Execute one migration file EXACTLY as written, as a single
             complete script (no splitting, no rewriting).
    Database effects: whatever the migration file states — that is the point.
    Possible failures: any SQL error propagates loudly (numbered migrations
        must fail visibly, per the approved plan).
    """
    sql = (MIGRATIONS_DIR / filename).read_text()
    connection.exec_driver_sql(sql)


@pytest.fixture(scope="module")
def migrated_connection():
    """One AUTOCOMMIT connection with the throwaway schema fully migrated.

    Module-scoped and single-connection ON PURPOSE: search_path is a session
    setting, so every statement must run on this same connection to stay
    inside the isolated schema.
    """
    unsafe_reason = validate_disposable_test_db(TEST_DB_URL)
    if unsafe_reason is not None:
        pytest.fail(
            f"REFUSING destructive migration test: {unsafe_reason}. "
            f"({sanitized_db_target(TEST_DB_URL)})",
            pytrace=False,
        )

    import sqlalchemy

    engine = sqlalchemy.create_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
    connection = engine.connect()
    try:
        connection.exec_driver_sql(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        connection.exec_driver_sql(f"CREATE SCHEMA {SCHEMA}")
        connection.exec_driver_sql(f"SET search_path TO {SCHEMA}")

        # Minimal stand-ins for the MAIN app's tables that the calendar
        # migrations reference by foreign key. Those tables belong to the
        # main Mia schema, not to the calendar migrations under test.
        connection.exec_driver_sql(
            "CREATE TABLE clients (id UUID PRIMARY KEY);"
            "CREATE TABLE conversations (id UUID PRIMARY KEY);"
        )

        _run_migration_file(connection, UP_001)
        _run_migration_file(connection, UP_002)
        yield connection
    finally:
        connection.exec_driver_sql(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        connection.close()
        engine.dispose()


def _index_definitions(connection) -> dict:
    """Read {indexname: indexdef} for the throwaway schema from pg_indexes."""
    rows = connection.exec_driver_sql(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s",
        (SCHEMA,),
    ).fetchall()
    return {name: definition for name, definition in rows}


def _seed_ids(connection):
    """Insert one client, two conversations, two slots; return their ids."""
    client_id = uuid.uuid4()
    conversation_a, conversation_b = uuid.uuid4(), uuid.uuid4()
    slot_1, slot_2 = uuid.uuid4(), uuid.uuid4()
    connection.exec_driver_sql(
        "INSERT INTO clients (id) VALUES (%s)", (str(client_id),)
    )
    connection.exec_driver_sql(
        "INSERT INTO conversations (id) VALUES (%s), (%s)",
        (str(conversation_a), str(conversation_b)),
    )
    for slot_id, day in ((slot_1, "2026-08-03"), (slot_2, "2026-08-04")):
        connection.exec_driver_sql(
            "INSERT INTO appointment_slots"
            " (id, client_id, start_datetime, end_datetime, status)"
            " VALUES (%s, %s, %s::timestamptz, %s::timestamptz, 'available')",
            (str(slot_id), str(client_id),
             f"{day} 14:00:00+00", f"{day} 14:45:00+00"),
        )
    return client_id, conversation_a, conversation_b, slot_1, slot_2


def _insert_appointment(connection, appointment_id, client_id, slot_id,
                        conversation_id, status="pending"):
    """Raw INSERT into appointments — deliberately below every application
    layer, so only the database's own constraints are being tested."""
    connection.exec_driver_sql(
        "INSERT INTO appointments"
        " (id, client_id, slot_id, conversation_id, patient_name,"
        "  patient_phone, start_datetime, end_datetime, status)"
        " VALUES (%s, %s, %s, %s, 'Kevin', '516-555-1234',"
        "  '2026-08-03 14:00:00+00'::timestamptz,"
        "  '2026-08-03 14:45:00+00'::timestamptz, %s)",
        (str(appointment_id), str(client_id), str(slot_id),
         str(conversation_id), status),
    )


def _assert_unique_violation(excinfo, expected_constraint: str) -> None:
    """The refusal must be SQLSTATE 23505 naming exactly our index."""
    driver_error = excinfo.value.orig
    assert getattr(driver_error, "pgcode", None) == "23505"
    assert getattr(driver_error.diag, "constraint_name", None) == expected_constraint


def test_migration_creates_partial_unique_indexes(migrated_connection):
    """After running the REAL 001 + 002 SQL, both indexes exist, are UNIQUE,
    and carry the partial predicates (the exact properties the ORM mirrors)."""
    definitions = _index_definitions(migrated_connection)
    assert CONVERSATION_INDEX in definitions, definitions.keys()
    assert SLOT_INDEX in definitions, definitions.keys()

    conversation_def = definitions[CONVERSATION_INDEX]
    assert "UNIQUE" in conversation_def
    assert "cancelled" in conversation_def          # partial: excludes cancelled
    assert "IS NOT NULL" in conversation_def        # partial: excludes staff rows

    slot_def = definitions[SLOT_INDEX]
    assert "UNIQUE" in slot_def
    assert "cancelled" in slot_def


def test_migrated_schema_enforces_and_allows_rebooking(migrated_connection):
    """Raw INSERTs against the MIGRATED schema (not the ORM-built one):
    duplicates are refused with the exact constraint names, and cancelling
    the blocking row makes the same inserts legal again."""
    from sqlalchemy.exc import IntegrityError

    conn = migrated_connection
    client_id, conv_a, conv_b, slot_1, slot_2 = _seed_ids(conn)

    first_appointment = uuid.uuid4()
    _insert_appointment(conn, first_appointment, client_id, slot_1, conv_a)

    # Same conversation, DIFFERENT slot -> conversation index refuses.
    with pytest.raises(IntegrityError) as excinfo:
        _insert_appointment(conn, uuid.uuid4(), client_id, slot_2, conv_a)
    _assert_unique_violation(excinfo, CONVERSATION_INDEX)

    # Different conversation, SAME slot -> slot index refuses.
    with pytest.raises(IntegrityError) as excinfo:
        _insert_appointment(conn, uuid.uuid4(), client_id, slot_1, conv_b)
    _assert_unique_violation(excinfo, SLOT_INDEX)

    # Cancel the blocking row: BOTH previously refused inserts become legal —
    # proving the predicates exclude cancelled rows (rebooking stays possible).
    conn.exec_driver_sql(
        "UPDATE appointments SET status = 'cancelled' WHERE id = %s",
        (str(first_appointment),),
    )
    _insert_appointment(conn, uuid.uuid4(), client_id, slot_2, conv_a)
    _insert_appointment(conn, uuid.uuid4(), client_id, slot_1, conv_b)


def test_reapplying_002_fails_loudly(migrated_connection):
    """002 has NO 'IF NOT EXISTS' on purpose: applying it twice must fail
    loudly (duplicate object), never half-apply silently.

    TRANSACTION HYGIENE: the migration script opens an explicit BEGIN, so
    when the expected error fires, PostgreSQL leaves this SHARED module-
    scoped connection inside an aborted transaction. The finally block
    ROLLs BACK unconditionally, and SELECT 1 then PROVES the connection is
    usable before any later test receives it.
    """
    from sqlalchemy.exc import ProgrammingError

    try:
        with pytest.raises(ProgrammingError):
            _run_migration_file(migrated_connection, UP_002)
    finally:
        migrated_connection.exec_driver_sql("ROLLBACK")
    migrated_connection.exec_driver_sql("SELECT 1")


def test_down_migration_removes_indexes_and_up_reapplies(migrated_connection):
    """002's down migration removes exactly the two indexes (reversibility),
    and the up migration applies cleanly again afterward."""
    conn = migrated_connection

    _run_migration_file(conn, DOWN_002)
    definitions = _index_definitions(conn)
    assert CONVERSATION_INDEX not in definitions
    assert SLOT_INDEX not in definitions

    # Restore: also proves rollback -> re-apply round-trips on real data
    # (the rows left by the enforcement test contain no active duplicates).
    _run_migration_file(conn, UP_002)
    definitions = _index_definitions(conn)
    assert CONVERSATION_INDEX in definitions
    assert SLOT_INDEX in definitions


# ---------------------------------------------------------------------------
# PATCH 2C — migration 003 (offer expiration columns). Each test below is
# SELF-CONTAINED and individually runnable: it applies 003 itself from the
# fixture-guaranteed baseline (001+002 only) and removes 003 in cleanup, so
# no test depends on schema state left behind by another.
# ---------------------------------------------------------------------------

UP_003 = "003_offer_expiration_up.sql"
DOWN_003 = "003_offer_expiration_down.sql"

OFFER_EXPIRES_COLUMN = "booking_offer_expires_at"
EFFECTIVE_PREFERENCE_COLUMN = "booking_effective_time_preference"


def _conversation_columns(connection) -> dict:
    """{column_name: (data_type, is_nullable)} for the throwaway schema's
    conversations table."""
    rows = connection.exec_driver_sql(
        "SELECT column_name, data_type, is_nullable"
        " FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = 'conversations'",
        (SCHEMA,),
    ).fetchall()
    return {name: (data_type, nullable) for name, data_type, nullable in rows}


def _table_columns(connection, table) -> set:
    rows = connection.exec_driver_sql(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = %s",
        (SCHEMA, table),
    ).fetchall()
    return {r[0] for r in rows}


def test_003_adds_offer_columns_with_correct_types(migrated_connection):
    """003 adds EXACTLY the two nullable columns with the approved types:
    timestamptz for the expiry, varchar/text for the effective preference."""
    before = _conversation_columns(migrated_connection)
    assert OFFER_EXPIRES_COLUMN not in before          # clean baseline
    assert EFFECTIVE_PREFERENCE_COLUMN not in before
    try:
        _run_migration_file(migrated_connection, UP_003)
        after = _conversation_columns(migrated_connection)
        added = set(after) - set(before)
        assert added == {OFFER_EXPIRES_COLUMN, EFFECTIVE_PREFERENCE_COLUMN}
        assert after[OFFER_EXPIRES_COLUMN] == ("timestamp with time zone", "YES")
        data_type, nullable = after[EFFECTIVE_PREFERENCE_COLUMN]
        assert data_type in ("character varying", "text")
        assert nullable == "YES"
    finally:
        _run_migration_file(migrated_connection, DOWN_003)
        assert _conversation_columns(migrated_connection) == before


def test_reapplying_003_fails_loudly(migrated_connection):
    """003 has NO 'IF NOT EXISTS' on purpose: this test applies it once
    ITSELF, applies it a second time asserting loud failure, rolls back the
    aborted transaction, proves the shared connection is usable, and removes
    its own application in cleanup."""
    from sqlalchemy.exc import ProgrammingError

    _run_migration_file(migrated_connection, UP_003)
    try:
        try:
            with pytest.raises(ProgrammingError):
                _run_migration_file(migrated_connection, UP_003)
        finally:
            migrated_connection.exec_driver_sql("ROLLBACK")
        migrated_connection.exec_driver_sql("SELECT 1")   # connection usable
    finally:
        _run_migration_file(migrated_connection, DOWN_003)


def test_003_down_removes_columns_and_preserves_001_002(migrated_connection):
    """003 down removes exactly its two columns and NOTHING else: the
    conversations column set returns to this test's own pre-003 snapshot,
    both Patch 1 unique indexes keep their predicates, and the 001 tables'
    column sets are unchanged — behavioral proof migrations 001 and 002
    remain unmodified."""
    conversations_before = _conversation_columns(migrated_connection)
    appointments_before = _table_columns(migrated_connection, "appointments")
    slots_before = _table_columns(migrated_connection, "appointment_slots")

    _run_migration_file(migrated_connection, UP_003)
    _run_migration_file(migrated_connection, DOWN_003)

    assert _conversation_columns(migrated_connection) == conversations_before
    assert _table_columns(migrated_connection, "appointments") == appointments_before
    assert _table_columns(migrated_connection, "appointment_slots") == slots_before

    definitions = _index_definitions(migrated_connection)
    for index_name in (CONVERSATION_INDEX, SLOT_INDEX):
        assert index_name in definitions
        assert "UNIQUE" in definitions[index_name]
        assert "cancelled" in definitions[index_name]
    # Down is idempotent; a second run in cleanup is a harmless no-op.
    _run_migration_file(migrated_connection, DOWN_003)


def test_003_up_reapplies_after_down(migrated_connection):
    """Round-trip: up -> down -> up succeeds again with the correct types
    (rollback then re-deploy is a real operational path)."""
    try:
        _run_migration_file(migrated_connection, UP_003)
        _run_migration_file(migrated_connection, DOWN_003)
        _run_migration_file(migrated_connection, UP_003)
        columns = _conversation_columns(migrated_connection)
        assert columns[OFFER_EXPIRES_COLUMN] == ("timestamp with time zone", "YES")
        assert EFFECTIVE_PREFERENCE_COLUMN in columns
    finally:
        _run_migration_file(migrated_connection, DOWN_003)


# ---------------------------------------------------------------------------
# PATCH 4 — migration 004 (staff-confirmation confirmed_at column). Same
# standard as the 003 section: each test is SELF-CONTAINED and individually
# runnable — it applies 004 itself from the fixture-guaranteed baseline
# (001+002 only) and removes 004 in cleanup, so no test depends on schema
# state left behind by another. 004's DOWN uses DROP COLUMN IF EXISTS
# (approved safe-rollback semantics), so a second down run is a no-op; the
# UP has no such guard and must fail loudly.
# ---------------------------------------------------------------------------

UP_004 = "004_staff_confirmation_up.sql"
DOWN_004 = "004_staff_confirmation_down.sql"

CONFIRMED_AT_COLUMN = "confirmed_at"


def _appointment_columns(connection) -> dict:
    """{column_name: (data_type, is_nullable)} for the throwaway schema's
    appointments table."""
    rows = connection.exec_driver_sql(
        "SELECT column_name, data_type, is_nullable"
        " FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = 'appointments'",
        (SCHEMA,),
    ).fetchall()
    return {name: (data_type, nullable) for name, data_type, nullable in rows}


def test_004_adds_confirmed_at_nullable_timestamptz(migrated_connection):
    """004 adds EXACTLY the one nullable timestamptz column — the properties
    the ORM's Appointment.confirmed_at mirrors."""
    before = _appointment_columns(migrated_connection)
    assert CONFIRMED_AT_COLUMN not in before               # clean baseline
    try:
        _run_migration_file(migrated_connection, UP_004)
        after = _appointment_columns(migrated_connection)
        assert set(after) - set(before) == {CONFIRMED_AT_COLUMN}
        assert after[CONFIRMED_AT_COLUMN] == ("timestamp with time zone", "YES")
    finally:
        _run_migration_file(migrated_connection, DOWN_004)
        assert _appointment_columns(migrated_connection) == before


def test_reapplying_004_fails_loudly(migrated_connection):
    """004's UP has NO 'IF NOT EXISTS' on purpose: this test applies it once
    ITSELF, applies it a second time asserting loud failure (duplicate
    column), rolls back the aborted transaction, proves the shared
    connection is usable, and removes its own application in cleanup."""
    from sqlalchemy.exc import ProgrammingError

    _run_migration_file(migrated_connection, UP_004)
    try:
        try:
            with pytest.raises(ProgrammingError):
                _run_migration_file(migrated_connection, UP_004)
        finally:
            migrated_connection.exec_driver_sql("ROLLBACK")
        migrated_connection.exec_driver_sql("SELECT 1")    # connection usable
    finally:
        _run_migration_file(migrated_connection, DOWN_004)


def test_004_down_removes_column_and_preserves_001_002_003(migrated_connection):
    """004 down removes exactly confirmed_at and NOTHING else: the
    appointments column set returns to this test's own pre-004 snapshot, the
    001 tables' column sets are unchanged, both Patch 1 unique indexes keep
    their predicates, and 003's conversation columns (applied here precisely
    to prove it) survive untouched. Also proves the approved IF EXISTS down
    semantics: a second down run is a harmless no-op."""
    appointments_before = _appointment_columns(migrated_connection)
    slots_before = _table_columns(migrated_connection, "appointment_slots")
    try:
        _run_migration_file(migrated_connection, UP_003)
        conversations_with_003 = _conversation_columns(migrated_connection)
        assert OFFER_EXPIRES_COLUMN in conversations_with_003   # 003 is live

        _run_migration_file(migrated_connection, UP_004)
        _run_migration_file(migrated_connection, DOWN_004)

        assert _appointment_columns(migrated_connection) == appointments_before
        assert _table_columns(migrated_connection, "appointment_slots") == slots_before
        assert _conversation_columns(migrated_connection) == conversations_with_003

        definitions = _index_definitions(migrated_connection)
        for index_name in (CONVERSATION_INDEX, SLOT_INDEX):
            assert index_name in definitions
            assert "UNIQUE" in definitions[index_name]
            assert "cancelled" in definitions[index_name]

        # Approved DOWN semantics: IF EXISTS makes a repeat run a no-op.
        _run_migration_file(migrated_connection, DOWN_004)
        assert _appointment_columns(migrated_connection) == appointments_before
    finally:
        _run_migration_file(migrated_connection, DOWN_003)


def test_004_up_reapplies_after_down(migrated_connection):
    """Round-trip: up -> down -> up succeeds again with the correct type
    (rollback then re-deploy is a real operational path)."""
    try:
        _run_migration_file(migrated_connection, UP_004)
        _run_migration_file(migrated_connection, DOWN_004)
        _run_migration_file(migrated_connection, UP_004)
        columns = _appointment_columns(migrated_connection)
        assert columns[CONFIRMED_AT_COLUMN] == ("timestamp with time zone", "YES")
    finally:
        _run_migration_file(migrated_connection, DOWN_004)


# ---------------------------------------------------------------------------
# PATCH 5 — migration 005 (calendar_admin_credentials table). Same standard
# as the 003/004 sections: each test is SELF-CONTAINED and individually
# runnable — it applies 005 itself from the fixture-guaranteed baseline
# (001+002 only) and removes 005 in cleanup. 005's DOWN uses DROP TABLE IF
# EXISTS (approved safe-rollback semantics), so a second down run is a
# no-op; the UP has no such guard and must fail loudly.
#
# SECRET HANDLING: the key_hash values inserted below are throwaway SHA-256
# digests of public test phrases — no real credential exists or is printed.
# ---------------------------------------------------------------------------

UP_005 = "005_calendar_admin_credentials_up.sql"
DOWN_005 = "005_calendar_admin_credentials_down.sql"

CREDENTIALS_TABLE = "calendar_admin_credentials"
CREDENTIALS_UNIQUE_INDEX = "uq_cal_admin_cred_key_hash"
CREDENTIALS_CLIENT_INDEX = "ix_cal_admin_cred_client_id"


def _credential_columns(connection) -> dict:
    """{column_name: (data_type, is_nullable, character_maximum_length)} for
    the throwaway schema's calendar_admin_credentials table."""
    rows = connection.exec_driver_sql(
        "SELECT column_name, data_type, is_nullable, character_maximum_length"
        " FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = %s",
        (SCHEMA, CREDENTIALS_TABLE),
    ).fetchall()
    return {name: (data_type, nullable, max_length)
            for name, data_type, nullable, max_length in rows}


def _credentials_table_exists(connection) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM information_schema.tables"
        " WHERE table_schema = %s AND table_name = %s",
        (SCHEMA, CREDENTIALS_TABLE),
    ).fetchone()
    return row is not None


def _seed_client_row(connection):
    """One row in the stand-in clients table for FK targets."""
    client_id = uuid.uuid4()
    connection.exec_driver_sql(
        "INSERT INTO clients (id) VALUES (%s)", (str(client_id),)
    )
    return client_id


def _insert_credential(connection, client_id, key_hash,
                       active=True, revoked_at=None):
    """One raw-SQL credential insert (the real provisioning path is raw
    operator SQL, so the DB-side defaults must carry id/created_at)."""
    connection.exec_driver_sql(
        "INSERT INTO calendar_admin_credentials"
        " (client_id, key_hash, label, active, revoked_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (str(client_id), key_hash, "migration test", active, revoked_at),
    )


def test_005_creates_credential_table_with_constraints(migrated_connection):
    """005 creates the table with the EXACT approved shape and every
    defensive rule is ENFORCED, not just declared: varchar(64) (not bpchar),
    the lowercase-hex CHECK (rejects a raw-key-shaped value — the dangerous
    operator mistake), the active/revoked consistency CHECK, the unique
    key-hash index, ON DELETE RESTRICT, DB-side defaults for raw-SQL
    provisioning, and multiple credentials per client for rotation."""
    import hashlib
    from sqlalchemy.exc import IntegrityError

    assert not _credentials_table_exists(migrated_connection)  # clean baseline
    try:
        _run_migration_file(migrated_connection, UP_005)

        columns = _credential_columns(migrated_connection)
        assert set(columns) == {"id", "client_id", "key_hash", "label",
                                "active", "created_at", "revoked_at"}
        assert columns["id"] == ("uuid", "NO", None)
        assert columns["client_id"] == ("uuid", "NO", None)
        # VARCHAR(64) exactly — "character varying", never "character"/bpchar.
        assert columns["key_hash"] == ("character varying", "NO", 64)
        assert columns["label"] == ("text", "NO", None)
        assert columns["active"] == ("boolean", "NO", None)
        assert columns["created_at"] == ("timestamp with time zone", "NO", None)
        assert columns["revoked_at"] == ("timestamp with time zone", "YES", None)

        definitions = _index_definitions(migrated_connection)
        assert CREDENTIALS_UNIQUE_INDEX in definitions
        assert "UNIQUE" in definitions[CREDENTIALS_UNIQUE_INDEX]
        assert CREDENTIALS_CLIENT_INDEX in definitions

        client_id = _seed_client_row(migrated_connection)
        digest_1 = hashlib.sha256(b"migration-test-credential-1").hexdigest()
        digest_2 = hashlib.sha256(b"migration-test-credential-2").hexdigest()

        # Valid insert works with DB-side defaults filling id/created_at,
        # and a SECOND credential for the SAME client is allowed (rotation).
        _insert_credential(migrated_connection, client_id, digest_1)
        _insert_credential(migrated_connection, client_id, digest_2)
        count = migrated_connection.exec_driver_sql(
            "SELECT count(*) FROM calendar_admin_credentials"
        ).scalar()
        assert count == 2

        # Duplicate digest -> unique index rejects (single-statement failures
        # on this AUTOCOMMIT connection leave no transaction open).
        with pytest.raises(IntegrityError):
            _insert_credential(migrated_connection, client_id, digest_1)

        # Raw-key-shaped value ('_' and uppercase; 51 chars fits varchar(64))
        # -> the hex CHECK rejects persisting a secret.
        with pytest.raises(IntegrityError):
            _insert_credential(migrated_connection, client_id,
                               "mia_cal_" + "A" * 43)

        # Uppercase hex -> rejected (lowercase is the canonical stored form).
        with pytest.raises(IntegrityError):
            _insert_credential(migrated_connection, client_id,
                               digest_1.upper())

        # ACTIVE credential carrying a revocation instant -> consistency
        # CHECK rejects.
        with pytest.raises(IntegrityError):
            _insert_credential(
                migrated_connection, client_id,
                hashlib.sha256(b"migration-test-credential-3").hexdigest(),
                active=True, revoked_at="2026-07-13T12:00:00+00:00",
            )

        # Inactive WITHOUT a revocation instant is allowed (one-directional
        # CHECK by design: temporary disable).
        _insert_credential(
            migrated_connection, client_id,
            hashlib.sha256(b"migration-test-credential-4").hexdigest(),
            active=False, revoked_at=None,
        )

        # ON DELETE RESTRICT: deleting a client with credentials must fail.
        with pytest.raises(IntegrityError):
            migrated_connection.exec_driver_sql(
                "DELETE FROM clients WHERE id = %s", (str(client_id),)
            )
        migrated_connection.exec_driver_sql("SELECT 1")   # connection usable
    finally:
        _run_migration_file(migrated_connection, DOWN_005)
        assert not _credentials_table_exists(migrated_connection)


def test_reapplying_005_fails_loudly(migrated_connection):
    """005's UP has NO 'IF NOT EXISTS' on purpose: this test applies it once
    ITSELF, applies it a second time asserting loud failure (duplicate
    table), rolls back the aborted transaction, proves the shared connection
    is usable, and removes its own application in cleanup."""
    from sqlalchemy.exc import ProgrammingError

    _run_migration_file(migrated_connection, UP_005)
    try:
        try:
            with pytest.raises(ProgrammingError):
                _run_migration_file(migrated_connection, UP_005)
        finally:
            migrated_connection.exec_driver_sql("ROLLBACK")
        migrated_connection.exec_driver_sql("SELECT 1")    # connection usable
    finally:
        _run_migration_file(migrated_connection, DOWN_005)


def test_005_down_removes_table_and_preserves_001_through_004(migrated_connection):
    """005 down removes exactly its own table and NOTHING else: the 001
    tables' column sets are unchanged, both Patch 1 unique indexes keep
    their predicates, and 003's conversation columns and 004's confirmed_at
    (applied here precisely to prove it) survive untouched. Also proves the
    approved DOWN semantics: a second down run is a harmless no-op."""
    appointments_baseline = _table_columns(migrated_connection, "appointments")
    slots_baseline = _table_columns(migrated_connection, "appointment_slots")
    try:
        _run_migration_file(migrated_connection, UP_003)
        _run_migration_file(migrated_connection, UP_004)
        conversations_with_003 = _conversation_columns(migrated_connection)
        appointments_with_004 = _appointment_columns(migrated_connection)
        assert OFFER_EXPIRES_COLUMN in conversations_with_003    # 003 is live
        assert CONFIRMED_AT_COLUMN in appointments_with_004      # 004 is live

        _run_migration_file(migrated_connection, UP_005)
        assert _credentials_table_exists(migrated_connection)
        _run_migration_file(migrated_connection, DOWN_005)

        assert not _credentials_table_exists(migrated_connection)
        assert _conversation_columns(migrated_connection) == conversations_with_003
        assert _appointment_columns(migrated_connection) == appointments_with_004
        assert _table_columns(migrated_connection, "appointment_slots") == slots_baseline

        definitions = _index_definitions(migrated_connection)
        for index_name in (CONVERSATION_INDEX, SLOT_INDEX):
            assert index_name in definitions
            assert "UNIQUE" in definitions[index_name]
            assert "cancelled" in definitions[index_name]
        # 005's indexes are gone WITH its table.
        assert CREDENTIALS_UNIQUE_INDEX not in definitions
        assert CREDENTIALS_CLIENT_INDEX not in definitions

        # Approved DOWN semantics: IF EXISTS makes a repeat run a no-op.
        _run_migration_file(migrated_connection, DOWN_005)
        assert not _credentials_table_exists(migrated_connection)
    finally:
        _run_migration_file(migrated_connection, DOWN_004)
        _run_migration_file(migrated_connection, DOWN_003)
    assert _table_columns(migrated_connection, "appointments") == appointments_baseline


def test_005_up_reapplies_after_down(migrated_connection):
    """Round-trip: up -> down -> up succeeds again with the correct key_hash
    type (rollback then re-deploy is a real operational path — and after a
    rollback every credential hash is gone, so re-provisioning is expected)."""
    try:
        _run_migration_file(migrated_connection, UP_005)
        _run_migration_file(migrated_connection, DOWN_005)
        _run_migration_file(migrated_connection, UP_005)
        columns = _credential_columns(migrated_connection)
        assert columns["key_hash"] == ("character varying", "NO", 64)
        assert columns["revoked_at"] == ("timestamp with time zone", "YES", None)
    finally:
        _run_migration_file(migrated_connection, DOWN_005)
        assert not _credentials_table_exists(migrated_connection)

# ---------------------------------------------------------------------------
# PATCH 9A — migration 006 (notification_attempts ledger). Same standard as
# the 003/004/005 sections: each test is SELF-CONTAINED and individually
# runnable — it applies 006 itself from the fixture-guaranteed baseline
# (001+002 only) and removes 006 in cleanup. 006's DOWN uses DROP TABLE IF
# EXISTS (approved 004/005 safe-rollback semantics), so a second down run is
# a no-op; the UP has no such guard and must fail loudly.
# ---------------------------------------------------------------------------

UP_006 = "006_notification_attempts_up.sql"
DOWN_006 = "006_notification_attempts_down.sql"

ATTEMPTS_TABLE = "notification_attempts"
ATTEMPTS_UNIQUE_INDEX = "uq_notification_attempt_per_channel"


def _attempt_columns(connection) -> dict:
    """{column_name: (data_type, is_nullable, column_default_prefix)} for
    the throwaway schema's notification_attempts table (the default is
    truncated to its function name — enough to prove DB-side defaults
    exist without coupling to formatting)."""
    rows = connection.exec_driver_sql(
        "SELECT column_name, data_type, is_nullable, column_default"
        " FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = %s",
        (SCHEMA, ATTEMPTS_TABLE),
    ).fetchall()
    return {
        name: (data_type, nullable,
               (default or "").split("(")[0] or None)
        for name, data_type, nullable, default in rows
    }


def _attempts_table_exists(connection) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM information_schema.tables"
        " WHERE table_schema = %s AND table_name = %s",
        (SCHEMA, ATTEMPTS_TABLE),
    ).fetchone()
    return row is not None


def _insert_attempt(connection, appointment_id, channel, status,
                    resolved_at=None):
    """Raw INSERT into notification_attempts — deliberately below every
    application layer, so only the database's own constraints are tested.
    id/created_at come from the DB-side defaults."""
    connection.exec_driver_sql(
        "INSERT INTO notification_attempts"
        " (appointment_id, channel, status, resolved_at)"
        " VALUES (%s, %s, %s, %s)",
        (str(appointment_id), channel, status, resolved_at),
    )


RESOLVED = "2026-07-14 12:00:00+00"


def test_006_creates_ledger_with_enforced_constraints(migrated_connection):
    """006 creates the table with the EXACT approved shape and every rule
    is ENFORCED, not just declared: the exact six columns with DB-side
    id/created_at defaults; the two-office-channel CHECK (a patient channel
    is unrepresentable); the three-status CHECK; the resolution CHECK
    pairing state and timestamp in BOTH directions; the per-channel unique
    index as the claim arbiter (rejecting a duplicate by exactly that
    index name while allowing the OTHER channel and OTHER appointments);
    and ON DELETE RESTRICT on the appointment FK."""
    from sqlalchemy.exc import IntegrityError

    conn = migrated_connection
    assert not _attempts_table_exists(conn)          # clean baseline
    try:
        _run_migration_file(conn, UP_006)

        columns = _attempt_columns(conn)
        assert set(columns) == {"id", "appointment_id", "channel", "status",
                                "created_at", "resolved_at"}
        assert columns["id"] == ("uuid", "NO", "gen_random_uuid")
        assert columns["appointment_id"] == ("uuid", "NO", None)
        assert columns["channel"] == ("text", "NO", None)
        assert columns["status"] == ("text", "NO", None)   # NO default
        assert columns["created_at"] == ("timestamp with time zone", "NO",
                                         "now")
        assert columns["resolved_at"] == ("timestamp with time zone", "YES",
                                          None)

        definitions = _index_definitions(conn)
        assert ATTEMPTS_UNIQUE_INDEX in definitions
        assert "UNIQUE" in definitions[ATTEMPTS_UNIQUE_INDEX]

        client_id, _conv_a, _conv_b, slot_1, slot_2 = _seed_ids(conn)
        appointment_a, appointment_b = uuid.uuid4(), uuid.uuid4()
        _insert_appointment(conn, appointment_a, client_id, slot_1, _conv_a)
        _insert_appointment(conn, appointment_b, client_id, slot_2, _conv_b)

        # Valid rows: DB defaults fill id/created_at; all three statuses
        # insert with their CORRECT resolution pairing; the same channel is
        # reusable on a DIFFERENT appointment.
        _insert_attempt(conn, appointment_a, "office_sms", "sending")
        _insert_attempt(conn, appointment_a, "office_email", "sent",
                        RESOLVED)
        _insert_attempt(conn, appointment_b, "office_sms", "unknown",
                        RESOLVED)
        count = conn.exec_driver_sql(
            "SELECT count(*) FROM notification_attempts").scalar()
        assert count == 3

        # Duplicate (appointment, channel) -> exactly OUR unique index.
        with pytest.raises(IntegrityError) as excinfo:
            _insert_attempt(conn, appointment_a, "office_sms", "sending")
        _assert_unique_violation(excinfo, ATTEMPTS_UNIQUE_INDEX)

        # Patient channel is unrepresentable (Patch 2D, structural).
        with pytest.raises(IntegrityError):
            _insert_attempt(conn, appointment_b, "patient_sms", "sending")

        # Unknown status rejected.
        with pytest.raises(IntegrityError):
            _insert_attempt(conn, appointment_b, "office_email", "failed",
                            RESOLVED)

        # Resolution CHECK, both directions: sending must NOT carry
        # resolved_at; terminal states MUST carry it.
        with pytest.raises(IntegrityError):
            _insert_attempt(conn, appointment_b, "office_email", "sending",
                            RESOLVED)
        with pytest.raises(IntegrityError):
            _insert_attempt(conn, appointment_b, "office_email", "sent")

        # ON DELETE RESTRICT: an appointment with ledger rows cannot go.
        with pytest.raises(IntegrityError):
            conn.exec_driver_sql(
                "DELETE FROM appointments WHERE id = %s",
                (str(appointment_a),))
        conn.exec_driver_sql("SELECT 1")             # connection usable
    finally:
        _run_migration_file(conn, DOWN_006)
        assert not _attempts_table_exists(conn)


def test_006_matches_orm_model_exactly(migrated_connection):
    """Migration/ORM parity (the drift test 006 must not escape): the live
    006 schema and app.calendar_models.NotificationAttempt agree on column
    names, nullability, the ACTUAL DATABASE TYPE of every column (uuid /
    text / timestamptz — compiled from the ORM type on the PostgreSQL
    dialect, so a Text->String/VARCHAR drift fails here), and the exact
    named constraints/index — proven against information_schema and
    pg_constraint/pg_indexes, not by reading the SQL file."""
    from sqlalchemy.dialects import postgresql
    from app.calendar_models import NotificationAttempt

    conn = migrated_connection
    try:
        _run_migration_file(conn, UP_006)

        db_columns = _attempt_columns(conn)
        orm_columns = {c.name: c for c in NotificationAttempt.__table__.columns}
        assert set(db_columns) == set(orm_columns)

        pg = postgresql.dialect()
        # information_schema.data_type spelling for each compiled ORM type.
        compiled_to_information_schema = {
            "UUID": "uuid",
            "TEXT": "text",
            "TIMESTAMP WITH TIME ZONE": "timestamp with time zone",
        }
        for name, column in orm_columns.items():
            db_type, db_nullable, _default = db_columns[name]
            assert db_nullable == ("YES" if column.nullable else "NO"), name
            compiled = column.type.compile(dialect=pg)
            assert compiled in compiled_to_information_schema, (
                f"{name}: ORM type compiles to {compiled!r}, which is not a "
                f"migration-006 type — TEXT/VARCHAR (or similar) drift")
            assert db_type == compiled_to_information_schema[compiled], (
                f"{name}: database says {db_type!r}, ORM compiles to "
                f"{compiled!r}")

        constraint_names = {
            row[0] for row in conn.exec_driver_sql(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = %s::regclass",
                (f"{SCHEMA}.{ATTEMPTS_TABLE}",),
            ).fetchall()
        }
        for expected in ("fk_notification_attempts_appointment",
                         "ck_notification_attempt_channel",
                         "ck_notification_attempt_status",
                         "ck_notification_attempt_resolution"):
            assert expected in constraint_names

        orm_names = {c.name for c in
                     NotificationAttempt.__table__.constraints
                     if c.name} | {i.name for i in
                                   NotificationAttempt.__table__.indexes}
        for expected in ("ck_notification_attempt_channel",
                         "ck_notification_attempt_status",
                         "ck_notification_attempt_resolution",
                         ATTEMPTS_UNIQUE_INDEX):
            assert expected in orm_names
        assert ATTEMPTS_UNIQUE_INDEX in _index_definitions(conn)
    finally:
        _run_migration_file(conn, DOWN_006)


def test_reapplying_006_fails_loudly(migrated_connection):
    """006's UP has NO 'IF NOT EXISTS' on purpose: a second application
    fails loudly (duplicate table), the aborted transaction is rolled back,
    and the shared connection stays usable."""
    from sqlalchemy.exc import ProgrammingError

    _run_migration_file(migrated_connection, UP_006)
    try:
        try:
            with pytest.raises(ProgrammingError):
                _run_migration_file(migrated_connection, UP_006)
        finally:
            migrated_connection.exec_driver_sql("ROLLBACK")
        migrated_connection.exec_driver_sql("SELECT 1")   # connection usable
    finally:
        _run_migration_file(migrated_connection, DOWN_006)


def test_006_down_removes_table_and_preserves_001_through_005(
        migrated_connection):
    """006 down removes exactly its own table and NOTHING else: the 001
    tables' column sets, the Patch 1 partial unique indexes, 003's
    conversation columns, 004's confirmed_at, and 005's credentials table
    (each applied here precisely to prove it) all survive untouched. A
    second down run is a harmless no-op (approved DOWN semantics)."""
    appointments_baseline = _table_columns(migrated_connection, "appointments")
    slots_baseline = _table_columns(migrated_connection, "appointment_slots")
    try:
        _run_migration_file(migrated_connection, UP_003)
        _run_migration_file(migrated_connection, UP_004)
        _run_migration_file(migrated_connection, UP_005)
        conversations_with_003 = _conversation_columns(migrated_connection)
        appointments_with_004 = _appointment_columns(migrated_connection)
        assert _credentials_table_exists(migrated_connection)   # 005 is live

        _run_migration_file(migrated_connection, UP_006)
        assert _attempts_table_exists(migrated_connection)
        _run_migration_file(migrated_connection, DOWN_006)

        assert not _attempts_table_exists(migrated_connection)
        assert _conversation_columns(migrated_connection) == conversations_with_003
        assert _appointment_columns(migrated_connection) == appointments_with_004
        assert _table_columns(migrated_connection, "appointment_slots") == slots_baseline
        assert _credentials_table_exists(migrated_connection)

        definitions = _index_definitions(migrated_connection)
        for index_name in (CONVERSATION_INDEX, SLOT_INDEX,
                           CREDENTIALS_UNIQUE_INDEX):
            assert index_name in definitions
        assert ATTEMPTS_UNIQUE_INDEX not in definitions

        # Approved DOWN semantics: a repeat run is a no-op.
        _run_migration_file(migrated_connection, DOWN_006)
        assert not _attempts_table_exists(migrated_connection)
    finally:
        _run_migration_file(migrated_connection, DOWN_005)
        _run_migration_file(migrated_connection, DOWN_004)
        _run_migration_file(migrated_connection, DOWN_003)
    assert _table_columns(migrated_connection, "appointments") == appointments_baseline


def test_006_up_reapplies_after_down(migrated_connection):
    """Round-trip: up -> down -> up succeeds again with the correct shape
    (rollback then re-deploy is a real operational path — after a rollback
    the ledger is empty by design, and runtime legacy suppression keeps
    already-notified appointments protected via their projection flags)."""
    try:
        _run_migration_file(migrated_connection, UP_006)
        _run_migration_file(migrated_connection, DOWN_006)
        _run_migration_file(migrated_connection, UP_006)
        columns = _attempt_columns(migrated_connection)
        assert columns["status"] == ("text", "NO", None)
        assert columns["resolved_at"] == ("timestamp with time zone", "YES",
                                          None)
        assert ATTEMPTS_UNIQUE_INDEX in _index_definitions(migrated_connection)
    finally:
        _run_migration_file(migrated_connection, DOWN_006)
        assert not _attempts_table_exists(migrated_connection)
