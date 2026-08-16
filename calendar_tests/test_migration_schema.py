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


# ---------------------------------------------------------------------------
# P2 - migration 007 (office_users portal tenant binding). Same self-
# contained pattern as 003..006: each test applies 007 itself from the
# fixture-guaranteed baseline (001+002 only - 007 depends only on clients)
# and removes it in cleanup.
# ---------------------------------------------------------------------------

UP_007 = "007_office_users_up.sql"
DOWN_007 = "007_office_users_down.sql"

OFFICE_USERS_UNIQUE_INDEX = "uq_office_users_auth_user"
OFFICE_USERS_CLIENT_INDEX = "ix_office_users_client_id"


def _office_users_columns(connection) -> dict:
    rows = connection.exec_driver_sql(
        "SELECT column_name, data_type, is_nullable"
        " FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = 'office_users'",
        (SCHEMA,),
    ).fetchall()
    return {name: (data_type, nullable) for name, data_type, nullable in rows}


def test_007_creates_office_users_with_approved_shape(migrated_connection):
    """007 creates office_users with the approved columns, the unique
    auth-user binding index, the client listing index, the closed role
    vocabulary CHECK, and the active/deactivated consistency CHECK.

    All row data uses fresh gen_random_uuid() values and is cleaned up by
    exact id: the module-scoped schema may already hold rows left by earlier
    self-contained blocks, and this test must neither collide with them nor
    delete them (their FK chains are not this test's to break)."""
    assert _office_users_columns(migrated_connection) == {}   # clean baseline

    def _new_uuid():
        return migrated_connection.exec_driver_sql(
            "SELECT gen_random_uuid()").scalar()

    client_id = None
    try:
        _run_migration_file(migrated_connection, UP_007)
        columns = _office_users_columns(migrated_connection)
        assert columns["auth_user_id"] == ("uuid", "NO")
        assert columns["client_id"] == ("uuid", "NO")
        assert columns["role"] == ("text", "NO")
        assert columns["active"] == ("boolean", "NO")
        assert columns["deactivated_at"] == ("timestamp with time zone", "YES")

        definitions = _index_definitions(migrated_connection)
        assert OFFICE_USERS_UNIQUE_INDEX in definitions
        assert "UNIQUE" in definitions[OFFICE_USERS_UNIQUE_INDEX].upper()
        assert OFFICE_USERS_CLIENT_INDEX in definitions

        # F-P2-1: row level security ENABLED (not FORCED - the owning
        # backend role must stay exempt) with ZERO policies: default deny
        # for every non-owner, non-BYPASSRLS role such as anon/authenticated.
        rls = migrated_connection.exec_driver_sql(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
            " WHERE oid = %s::regclass",
            (f"{SCHEMA}.office_users",),
        ).fetchone()
        assert rls == (True, False)
        policy_count = migrated_connection.exec_driver_sql(
            "SELECT count(*) FROM pg_policies"
            " WHERE schemaname = %s AND tablename = 'office_users'",
            (SCHEMA,),
        ).scalar()
        assert policy_count == 0

        client_id = _new_uuid()
        auth_id = _new_uuid()
        migrated_connection.exec_driver_sql(
            "INSERT INTO clients (id) VALUES (%s)", (client_id,))
        migrated_connection.exec_driver_sql(
            "INSERT INTO office_users (auth_user_id, client_id)"
            " VALUES (%s, %s)", (auth_id, client_id))

        # Closed role vocabulary: unknown roles are impossible to persist.
        with pytest.raises(Exception):
            migrated_connection.exec_driver_sql(
                "INSERT INTO office_users (auth_user_id, client_id, role)"
                " VALUES (%s, %s, 'super_admin')", (_new_uuid(), client_id))
        migrated_connection.exec_driver_sql("ROLLBACK")

        # V1 binding rule: one office per auth user (unique index).
        with pytest.raises(Exception):
            migrated_connection.exec_driver_sql(
                "INSERT INTO office_users (auth_user_id, client_id)"
                " VALUES (%s, %s)", (auth_id, client_id))
        migrated_connection.exec_driver_sql("ROLLBACK")

        # Consistency CHECK: an ACTIVE binding cannot carry deactivated_at.
        with pytest.raises(Exception):
            migrated_connection.exec_driver_sql(
                "INSERT INTO office_users"
                " (auth_user_id, client_id, active, deactivated_at)"
                " VALUES (%s, %s, true, now())", (_new_uuid(), client_id))
        migrated_connection.exec_driver_sql("ROLLBACK")
    finally:
        _run_migration_file(migrated_connection, DOWN_007)
        if client_id is not None:
            migrated_connection.exec_driver_sql(
                "DELETE FROM clients WHERE id = %s", (client_id,))
        assert _office_users_columns(migrated_connection) == {}


def test_007_rls_denies_browser_data_api_roles(migrated_connection):
    """F-P2-1 posture proof, simulating Supabase faithfully: the Data API
    roles (anon / authenticated) receive table grants automatically via
    ALTER DEFAULT PRIVILEGES, so this test creates those roles, installs the
    same default-privilege grant, THEN applies 007 and proves:

      1. the migration's conditional REVOKE stripped the auto-granted table
         privileges (defense in depth);
      2. even when SELECT/INSERT are granted back, enabled-RLS-with-no-policy
         hides every row and blocks every write (default deny);
      3. the table owner keeps full access with no policy (backend path).

    Self-contained: every role, grant, default-privilege change, and row it
    creates is removed in the finally block (roles are cluster-global even
    in a throwaway database, and a grantee role cannot be dropped while
    grants reference it)."""
    conn = migrated_connection
    created_roles = []
    applied_007 = False
    client_id = None
    try:
        for role in ("anon", "authenticated"):
            exists = conn.exec_driver_sql(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            if exists is None:
                conn.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
                created_roles.append(role)
        conn.exec_driver_sql(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA}"
            " GRANT ALL ON TABLES TO anon, authenticated")
        conn.exec_driver_sql(
            f"GRANT USAGE ON SCHEMA {SCHEMA} TO anon, authenticated")

        _run_migration_file(conn, UP_007)
        applied_007 = True

        # 1) The REVOKE branch executed: the Supabase-style default grant
        #    is GONE for both browser roles.
        for role in ("anon", "authenticated"):
            has_select = conn.exec_driver_sql(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                (role, f"{SCHEMA}.office_users"),
            ).scalar()
            assert has_select is False

        # 2) Even with privileges granted back, RLS default-deny holds.
        client_id = conn.exec_driver_sql(
            "SELECT gen_random_uuid()").scalar()
        conn.exec_driver_sql(
            "INSERT INTO clients (id) VALUES (%s)", (client_id,))
        conn.exec_driver_sql(
            "INSERT INTO office_users (auth_user_id, client_id)"
            " VALUES (gen_random_uuid(), %s)", (client_id,))
        conn.exec_driver_sql(
            "GRANT SELECT, INSERT ON office_users TO anon")
        conn.exec_driver_sql("SET ROLE anon")
        visible = conn.exec_driver_sql(
            "SELECT count(*) FROM office_users").scalar()
        assert visible == 0                      # rows hidden by RLS
        with pytest.raises(Exception):
            conn.exec_driver_sql(
                "INSERT INTO office_users (auth_user_id, client_id)"
                " VALUES (gen_random_uuid(), %s)", (client_id,))
        conn.exec_driver_sql("RESET ROLE")

        # 3) Owner path (the backend's situation): full access, no policy.
        owner_count = conn.exec_driver_sql(
            "SELECT count(*) FROM office_users").scalar()
        assert owner_count == 1
    finally:
        conn.exec_driver_sql("RESET ROLE")
        if applied_007:
            _run_migration_file(conn, DOWN_007)
        conn.exec_driver_sql(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA}"
            " REVOKE ALL ON TABLES FROM anon, authenticated")
        conn.exec_driver_sql(
            f"REVOKE USAGE ON SCHEMA {SCHEMA} FROM anon, authenticated")
        if client_id is not None:
            conn.exec_driver_sql(
                "DELETE FROM clients WHERE id = %s", (client_id,))
        for role in created_roles:
            conn.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")


def test_reapplying_007_fails_loudly(migrated_connection):
    """007 has NO 'IF NOT EXISTS' on purpose (002..006 convention)."""
    from sqlalchemy.exc import ProgrammingError

    _run_migration_file(migrated_connection, UP_007)
    try:
        try:
            with pytest.raises(ProgrammingError):
                _run_migration_file(migrated_connection, UP_007)
        finally:
            migrated_connection.exec_driver_sql("ROLLBACK")
        migrated_connection.exec_driver_sql("SELECT 1")   # connection usable
    finally:
        _run_migration_file(migrated_connection, DOWN_007)


def test_007_down_removes_table_and_preserves_others(migrated_connection):
    """Round trip up -> down -> up; the down removes exactly office_users
    (IF EXISTS: repeat run is a no-op) and leaves the 001/002 tables and
    indexes untouched."""
    appointments_before = _table_columns(migrated_connection, "appointments")
    slots_before = _table_columns(migrated_connection, "appointment_slots")

    _run_migration_file(migrated_connection, UP_007)
    _run_migration_file(migrated_connection, DOWN_007)
    assert _office_users_columns(migrated_connection) == {}
    _run_migration_file(migrated_connection, DOWN_007)     # no-op repeat
    _run_migration_file(migrated_connection, UP_007)
    try:
        assert OFFICE_USERS_UNIQUE_INDEX in _index_definitions(
            migrated_connection)
    finally:
        _run_migration_file(migrated_connection, DOWN_007)

    assert _table_columns(migrated_connection, "appointments") == appointments_before
    assert _table_columns(migrated_connection, "appointment_slots") == slots_before
    definitions = _index_definitions(migrated_connection)
    for index_name in (CONVERSATION_INDEX, SLOT_INDEX):
        assert index_name in definitions


# ---------------------------------------------------------------------------
# P3-B2-S1 - migration 008 (office workflow columns on conversations). Same
# standard as the 005/006/007 sections: each test applies 008 itself from
# the fixture-guaranteed baseline (001+002 plus the minimal stand-in
# conversations table) and removes 008 in cleanup. 008's DOWN uses
# DROP COLUMN IF EXISTS, so cleanup is safe even after a partial failure.
#
# The stand-in conversations table deliberately has only an id column: 008
# must apply cleanly against ANY conversations shape because it is purely
# additive (no existing column is read, rewritten, or constrained).
# ---------------------------------------------------------------------------

UP_008 = "008_office_lead_workflow_up.sql"
DOWN_008 = "008_office_lead_workflow_down.sql"

OFFICE_STATUS_VOCAB_CK = "ck_conversations_office_status_vocab"
OFFICE_STATUS_TS_CK = "ck_conversations_office_status_has_ts"
OFFICE_NOTE_SHAPE_CK = "ck_conversations_office_note_shape"
OFFICE_NOTE_TS_CK = "ck_conversations_office_note_has_ts"

OFFICE_WORKFLOW_COLUMNS = (
    "office_status",
    "office_status_updated_at",
    "office_note",
    "office_note_updated_at",
)

OFFICE_WORKFLOW_CHECKS = (
    OFFICE_STATUS_VOCAB_CK,
    OFFICE_STATUS_TS_CK,
    OFFICE_NOTE_SHAPE_CK,
    OFFICE_NOTE_TS_CK,
)

# One reusable non-NULL token value for raw INSERTs below.
_TOKEN = "2026-08-11 12:00:00+00"


def _conversations_columns(connection) -> dict:
    """Read {column_name: {data_type, is_nullable, default}} for the
    stand-in conversations table inside the throwaway schema."""
    rows = connection.exec_driver_sql(
        "SELECT column_name, data_type, is_nullable, column_default"
        " FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = 'conversations'",
        (SCHEMA,),
    ).fetchall()
    return {
        name: {"data_type": data_type, "is_nullable": is_nullable,
               "default": default}
        for name, data_type, is_nullable, default in rows
    }


def _conversations_check_names(connection) -> set:
    """The set of CHECK constraint names on the throwaway conversations
    table (pg_constraint contype 'c')."""
    rows = connection.exec_driver_sql(
        "SELECT con.conname FROM pg_constraint con"
        " JOIN pg_class rel ON rel.oid = con.conrelid"
        " JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace"
        " WHERE nsp.nspname = %s AND rel.relname = 'conversations'"
        " AND con.contype = 'c'",
        (SCHEMA,),
    ).fetchall()
    return {row[0] for row in rows}


def _assert_check_violation(excinfo, expected_constraint: str) -> None:
    """The refusal must be SQLSTATE 23514 naming exactly our constraint."""
    driver_error = excinfo.value.orig
    assert getattr(driver_error, "pgcode", None) == "23514"
    assert getattr(driver_error.diag, "constraint_name", None) == expected_constraint


def _insert_conversation(connection, **office_fields):
    """Raw INSERT into the stand-in conversations table - deliberately below
    every application layer, so only the database's own 008 constraints are
    being tested. Returns the new row id (caller deletes it in cleanup)."""
    row_id = uuid.uuid4()
    columns = ["id"] + list(office_fields.keys())
    placeholders = ", ".join(["%s"] * len(columns))
    values = [str(row_id)] + list(office_fields.values())
    connection.exec_driver_sql(
        "INSERT INTO conversations (" + ", ".join(columns) + ")"
        " VALUES (" + placeholders + ")",
        tuple(values),
    )
    return row_id


def _delete_conversations(connection, row_ids) -> None:
    """Remove exactly the rows a test created (module-scoped fixture: rows
    would otherwise leak across tests)."""
    for row_id in row_ids:
        connection.exec_driver_sql(
            "DELETE FROM conversations WHERE id = %s", (str(row_id),)
        )


def test_008_adds_nullable_default_free_columns(migrated_connection):
    """008 adds EXACTLY the four approved columns - TEXT/TIMESTAMPTZ, all
    nullable, all default-free - plus the four named CHECKs, and a row that
    existed BEFORE the migration ends up all-NULL (no default may make an
    existing row appear office-modified)."""
    conn = migrated_connection
    before = _conversations_columns(conn)
    for column in OFFICE_WORKFLOW_COLUMNS:
        assert column not in before, f"{column} must not pre-exist"

    pre_existing = _insert_conversation(conn)
    applied = False
    try:
        _run_migration_file(conn, UP_008)
        applied = True

        columns = _conversations_columns(conn)
        expected_types = {
            "office_status": "text",
            "office_status_updated_at": "timestamp with time zone",
            "office_note": "text",
            "office_note_updated_at": "timestamp with time zone",
        }
        for column, expected_type in expected_types.items():
            assert column in columns, columns.keys()
            assert columns[column]["data_type"] == expected_type
            assert columns[column]["is_nullable"] == "YES"
            assert columns[column]["default"] is None

        checks = _conversations_check_names(conn)
        for check_name in OFFICE_WORKFLOW_CHECKS:
            assert check_name in checks, checks

        row = conn.exec_driver_sql(
            "SELECT office_status, office_status_updated_at,"
            " office_note, office_note_updated_at"
            " FROM conversations WHERE id = %s", (str(pre_existing),)
        ).fetchone()
        assert row == (None, None, None, None)
    finally:
        if applied:
            _run_migration_file(conn, DOWN_008)
        _delete_conversations(conn, [pre_existing])


def test_008_status_constraints_bite(migrated_connection):
    """Raw INSERTs against the MIGRATED schema: every approved status value
    (with its token) is accepted; an unknown status is refused by name; a
    status without its token is refused by name; and the approved cleared
    shape (NULL status + non-NULL token) is legal."""
    from sqlalchemy.exc import IntegrityError

    conn = migrated_connection
    created = []
    applied = False
    try:
        _run_migration_file(conn, UP_008)
        applied = True

        # Every approved value, WITH its token, is accepted.
        for status in ("contacted", "booked", "closed"):
            created.append(_insert_conversation(
                conn, office_status=status,
                office_status_updated_at=_TOKEN))

        # 'new' is NOT an office value (clearing to NULL replaces it), and
        # arbitrary strings are refused the same way. Token present so only
        # the vocabulary CHECK can be the refusing constraint.
        for bad_status in ("new", "completed", "followup", "CONTACTED"):
            with pytest.raises(IntegrityError) as excinfo:
                _insert_conversation(
                    conn, office_status=bad_status,
                    office_status_updated_at=_TOKEN)
            _assert_check_violation(excinfo, OFFICE_STATUS_VOCAB_CK)
            conn.exec_driver_sql("ROLLBACK")

        # A present status without its version token is refused by name.
        with pytest.raises(IntegrityError) as excinfo:
            _insert_conversation(conn, office_status="contacted")
        _assert_check_violation(excinfo, OFFICE_STATUS_TS_CK)
        conn.exec_driver_sql("ROLLBACK")

        # Cleared shape: NULL status keeping its last token is LEGAL (the
        # one-directional CHECK is the approved timestamp contract).
        created.append(_insert_conversation(
            conn, office_status_updated_at=_TOKEN))
    finally:
        if applied:
            _delete_conversations(conn, created)
            _run_migration_file(conn, DOWN_008)


def test_008_note_constraints_bite(migrated_connection):
    """Raw INSERTs against the MIGRATED schema: a trimmed non-empty note of
    at most 2000 characters (with its token) is accepted; empty,
    space-only, and over-length notes are refused by name (btrim's
    default trims spaces; broader whitespace is the application
    layer's duty in P3-B2); a note
    without its token is refused by name; and NULL note + non-NULL token
    (cleared shape) is legal."""
    from sqlalchemy.exc import IntegrityError

    conn = migrated_connection
    created = []
    applied = False
    try:
        _run_migration_file(conn, UP_008)
        applied = True

        # Valid note, and the exact 2000-character boundary, are accepted.
        created.append(_insert_conversation(
            conn, office_note="Called patient, left voicemail.",
            office_note_updated_at=_TOKEN))
        created.append(_insert_conversation(
            conn, office_note="x" * 2000,
            office_note_updated_at=_TOKEN))

        # Empty, whitespace-only, and 2001-character notes are refused by
        # the shape CHECK by name (token present so only shape can refuse).
        for bad_note in ("", "   ", "x" * 2001):
            with pytest.raises(IntegrityError) as excinfo:
                _insert_conversation(
                    conn, office_note=bad_note,
                    office_note_updated_at=_TOKEN)
            _assert_check_violation(excinfo, OFFICE_NOTE_SHAPE_CK)
            conn.exec_driver_sql("ROLLBACK")

        # A present note without its version token is refused by name.
        with pytest.raises(IntegrityError) as excinfo:
            _insert_conversation(conn, office_note="valid note")
        _assert_check_violation(excinfo, OFFICE_NOTE_TS_CK)
        conn.exec_driver_sql("ROLLBACK")

        # Cleared shape: NULL note keeping its last token is LEGAL.
        created.append(_insert_conversation(
            conn, office_note_updated_at=_TOKEN))
    finally:
        if applied:
            _delete_conversations(conn, created)
            _run_migration_file(conn, DOWN_008)


def test_008_orm_parity(migrated_connection):
    """Migration/ORM parity (the drift test 008 must not escape): the live
    008 schema and app.models.Conversation agree on the four office
    workflow columns - name, nullability, and type family."""
    import sqlalchemy as sa

    from app.models import Conversation

    conn = migrated_connection
    applied = False
    try:
        _run_migration_file(conn, UP_008)
        applied = True
        db_columns = _conversations_columns(conn)
        orm_columns = Conversation.__table__.columns

        for column in OFFICE_WORKFLOW_COLUMNS:
            assert column in orm_columns, f"ORM missing {column}"
            assert column in db_columns, f"migration missing {column}"
            orm_column = orm_columns[column]
            assert orm_column.nullable is True
            db_type = db_columns[column]["data_type"]
            if column.endswith("_updated_at"):
                assert isinstance(orm_column.type, sa.DateTime)
                assert orm_column.type.timezone is True
                assert db_type == "timestamp with time zone"
            else:
                assert isinstance(orm_column.type, (sa.String, sa.Text)), (
                    f"{column}: ORM type {orm_column.type!r} is not in the "
                    f"migration-008 type family - TEXT/VARCHAR drift")
                assert db_type == "text"
            # No server default on either side: an existing row must never
            # appear office-modified after the migration.
            assert orm_column.server_default is None
            assert db_columns[column]["default"] is None
    finally:
        if applied:
            _run_migration_file(conn, DOWN_008)


def test_reapplying_008_fails_loudly(migrated_connection):
    """008 has NO 'IF NOT EXISTS' on purpose (002..007 convention): the
    second application must fail loudly (duplicate column), and the
    connection must remain usable afterwards."""
    from sqlalchemy.exc import ProgrammingError

    _run_migration_file(migrated_connection, UP_008)
    try:
        try:
            with pytest.raises(ProgrammingError):
                _run_migration_file(migrated_connection, UP_008)
        finally:
            migrated_connection.exec_driver_sql("ROLLBACK")
        migrated_connection.exec_driver_sql("SELECT 1")   # connection usable
    finally:
        _run_migration_file(migrated_connection, DOWN_008)


def test_008_down_removes_only_new_columns_and_up_reapplies(migrated_connection):
    """Round trip up -> down -> down -> up: the down removes EXACTLY the
    four 008 columns and their CHECKs (IF EXISTS: repeat run is a no-op),
    every pre-008 conversations column survives byte-for-byte, the 001/002
    tables and indexes are untouched, and the up applies cleanly again."""
    conn = migrated_connection
    conversations_before = _conversations_columns(conn)
    appointments_before = _table_columns(conn, "appointments")
    slots_before = _table_columns(conn, "appointment_slots")

    _run_migration_file(conn, UP_008)
    _run_migration_file(conn, DOWN_008)
    assert _conversations_columns(conn) == conversations_before
    remaining_checks = _conversations_check_names(conn)
    for check_name in OFFICE_WORKFLOW_CHECKS:
        assert check_name not in remaining_checks

    _run_migration_file(conn, DOWN_008)     # no-op repeat (IF EXISTS)
    assert _conversations_columns(conn) == conversations_before

    _run_migration_file(conn, UP_008)
    try:
        columns = _conversations_columns(conn)
        for column in OFFICE_WORKFLOW_COLUMNS:
            assert column in columns
        checks = _conversations_check_names(conn)
        for check_name in OFFICE_WORKFLOW_CHECKS:
            assert check_name in checks
    finally:
        _run_migration_file(conn, DOWN_008)

    assert _table_columns(conn, "appointments") == appointments_before
    assert _table_columns(conn, "appointment_slots") == slots_before
    definitions = _index_definitions(conn)
    for index_name in (CONVERSATION_INDEX, SLOT_INDEX):
        assert index_name in definitions


# ---------------------------------------------------------------------------
# Migration 009 - P6-A notification-settings concurrency token (clients)
# ---------------------------------------------------------------------------

UP_009 = "009_notification_settings_token_up.sql"
DOWN_009 = "009_notification_settings_token_down.sql"

NOTIFICATION_TOKEN_COLUMN = "notification_settings_updated_at"


def _clients_columns(connection) -> dict:
    """{column_name: (data_type, is_nullable)} for the throwaway schema's
    clients table."""
    rows = connection.exec_driver_sql(
        "SELECT column_name, data_type, is_nullable"
        " FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = 'clients'",
        (SCHEMA,),
    ).fetchall()
    return {name: (data_type, nullable) for name, data_type, nullable in rows}


def test_009_adds_exactly_the_nullable_token_column(migrated_connection):
    """009 adds EXACTLY one nullable timestamptz token column to clients and
    nothing else; down removes exactly it."""
    before = _clients_columns(migrated_connection)
    assert NOTIFICATION_TOKEN_COLUMN not in before      # clean baseline
    try:
        _run_migration_file(migrated_connection, UP_009)
        after = _clients_columns(migrated_connection)
        added = set(after) - set(before)
        assert added == {NOTIFICATION_TOKEN_COLUMN}
        assert after[NOTIFICATION_TOKEN_COLUMN] == (
            "timestamp with time zone", "YES")
    finally:
        _run_migration_file(migrated_connection, DOWN_009)
        assert _clients_columns(migrated_connection) == before


def test_reapplying_009_fails_loudly(migrated_connection):
    """009 up has NO 'IF NOT EXISTS' on purpose: a second application must
    fail loudly. This test applies it once itself, proves the second attempt
    raises, rolls back the aborted transaction, proves the shared connection
    is still usable, and removes its own application in cleanup."""
    from sqlalchemy.exc import ProgrammingError

    _run_migration_file(migrated_connection, UP_009)
    try:
        try:
            with pytest.raises(ProgrammingError):
                _run_migration_file(migrated_connection, UP_009)
        finally:
            migrated_connection.exec_driver_sql("ROLLBACK")
        migrated_connection.exec_driver_sql("SELECT 1")   # connection usable
    finally:
        _run_migration_file(migrated_connection, DOWN_009)


def test_009_down_removes_only_token_and_preserves_prior(migrated_connection):
    """009 down removes exactly its one column and NOTHING else: the clients
    column set returns to this test's own pre-009 snapshot and the 001 tables'
    column sets are unchanged. Down is also safe to run when 009 was never
    applied (IF EXISTS)."""
    clients_before = _clients_columns(migrated_connection)
    appointments_before = _table_columns(migrated_connection, "appointments")
    slots_before = _table_columns(migrated_connection, "appointment_slots")

    _run_migration_file(migrated_connection, UP_009)
    _run_migration_file(migrated_connection, DOWN_009)

    assert _clients_columns(migrated_connection) == clients_before
    assert _table_columns(migrated_connection, "appointments") == \
        appointments_before
    assert _table_columns(migrated_connection, "appointment_slots") == \
        slots_before

    # Down is idempotent when the column is already gone (IF EXISTS).
    _run_migration_file(migrated_connection, DOWN_009)
    assert _clients_columns(migrated_connection) == clients_before


def test_009_preserves_existing_destination_bytes(migrated_connection):
    """The destination columns are the SOURCE OF TRUTH (owner decisions
    D2/D3): 009 must not move, rename, or disturb them. This models the real
    clients row by adding the two destination columns to the throwaway clients
    table, seeds a row with known values, and proves those values survive both
    up and down unchanged while the token column arrives NULL and then
    departs. It restores the throwaway clients table to its (id) shape in
    cleanup so no other migration test sees the modeled columns."""
    seed_id = uuid.uuid4()
    migrated_connection.exec_driver_sql(
        "ALTER TABLE clients ADD COLUMN notification_email TEXT;"
        "ALTER TABLE clients ADD COLUMN notification_phone TEXT;"
    )

    def _destinations():
        """The two destination values only (always present in this test)."""
        return migrated_connection.exec_driver_sql(
            "SELECT notification_email, notification_phone"
            " FROM clients WHERE id = %s",
            (str(seed_id),),
        ).fetchone()

    def _token_value():
        return migrated_connection.exec_driver_sql(
            f"SELECT {NOTIFICATION_TOKEN_COLUMN} FROM clients WHERE id = %s",
            (str(seed_id),),
        ).fetchone()[0]

    try:
        migrated_connection.exec_driver_sql(
            "INSERT INTO clients"
            " (id, notification_email, notification_phone)"
            " VALUES (%s, %s, %s)",
            (str(seed_id), "front-desk@example.com", "516-555-7777"),
        )

        _run_migration_file(migrated_connection, UP_009)
        assert NOTIFICATION_TOKEN_COLUMN in _clients_columns(
            migrated_connection)
        assert _destinations() == ("front-desk@example.com", "516-555-7777")
        assert _token_value() is None                # new token starts NULL

        _run_migration_file(migrated_connection, DOWN_009)
        assert NOTIFICATION_TOKEN_COLUMN not in _clients_columns(
            migrated_connection)
        assert _destinations() == ("front-desk@example.com", "516-555-7777")
    finally:
        # Restore the throwaway clients table to its (id) shape so no other
        # migration test sees the modeled destination or token columns.
        migrated_connection.exec_driver_sql(
            "ALTER TABLE clients DROP COLUMN IF EXISTS "
            + NOTIFICATION_TOKEN_COLUMN + ";"
            "ALTER TABLE clients DROP COLUMN IF EXISTS notification_email;"
            "ALTER TABLE clients DROP COLUMN IF EXISTS notification_phone;"
        )


# ---------------------------------------------------------------------------
# PATCH P4-B - migration 010 (schedule_config_updated_at token column). One
# nullable TIMESTAMPTZ column, no default/backfill/CHECK; mirrors 009's
# reversibility and preservation proofs. The recurring config lives in the
# EXISTING office_hours/settings JSONB columns - 010 must not disturb them.
# ---------------------------------------------------------------------------

UP_010 = "010_schedule_config_token_up.sql"
DOWN_010 = "010_schedule_config_token_down.sql"

SCHEDULE_TOKEN_COLUMN = "schedule_config_updated_at"


def test_010_adds_exactly_the_nullable_token_column(migrated_connection):
    """010 adds EXACTLY one nullable timestamptz token column to clients and
    nothing else; down removes exactly it."""
    before = _clients_columns(migrated_connection)
    assert SCHEDULE_TOKEN_COLUMN not in before          # clean baseline
    try:
        _run_migration_file(migrated_connection, UP_010)
        after = _clients_columns(migrated_connection)
        added = set(after) - set(before)
        assert added == {SCHEDULE_TOKEN_COLUMN}
        assert after[SCHEDULE_TOKEN_COLUMN] == (
            "timestamp with time zone", "YES")
    finally:
        _run_migration_file(migrated_connection, DOWN_010)
        assert _clients_columns(migrated_connection) == before


def test_reapplying_010_fails_loudly(migrated_connection):
    """010 up has NO 'IF NOT EXISTS' on the ADD (contract 2): a second
    application must fail loudly. Applies once, proves the second attempt
    raises, rolls back the aborted transaction, proves the shared connection
    is still usable, and removes its own application in cleanup."""
    from sqlalchemy.exc import ProgrammingError

    _run_migration_file(migrated_connection, UP_010)
    try:
        try:
            with pytest.raises(ProgrammingError):
                _run_migration_file(migrated_connection, UP_010)
        finally:
            migrated_connection.exec_driver_sql("ROLLBACK")
        migrated_connection.exec_driver_sql("SELECT 1")   # connection usable
    finally:
        _run_migration_file(migrated_connection, DOWN_010)


def test_010_down_removes_only_token_and_preserves_prior(migrated_connection):
    """010 down removes exactly its one column and NOTHING else: the clients
    column set returns to this test's own pre-010 snapshot and the 001 tables'
    column sets are unchanged. Down is also safe to run when 010 was never
    applied (IF EXISTS)."""
    clients_before = _clients_columns(migrated_connection)
    appointments_before = _table_columns(migrated_connection, "appointments")
    slots_before = _table_columns(migrated_connection, "appointment_slots")

    _run_migration_file(migrated_connection, UP_010)
    _run_migration_file(migrated_connection, DOWN_010)

    assert _clients_columns(migrated_connection) == clients_before
    assert _table_columns(migrated_connection, "appointments") == \
        appointments_before
    assert _table_columns(migrated_connection, "appointment_slots") == \
        slots_before

    # Down is idempotent when the column is already gone (IF EXISTS).
    _run_migration_file(migrated_connection, DOWN_010)
    assert _clients_columns(migrated_connection) == clients_before


def test_010_preserves_existing_office_hours_and_settings_bytes(
        migrated_connection):
    """F7: the recurring config is stored in the EXISTING office_hours and
    settings JSONB columns; 010 adds ONLY the token column and must not move,
    rewrite, or default those columns. This models the real clients row by
    adding the two JSONB columns to the throwaway clients table, seeds known
    JSON, and proves both the parsed value AND the ::text serialization
    survive up and down unchanged while the token arrives NULL then departs.
    It restores the throwaway clients table to its (id) shape in cleanup."""
    seed_id = uuid.uuid4()
    office_hours = {"mon": {"open": True, "start": "09:00", "end": "17:00"},
                    "sun": {"open": False, "start": None, "end": None}}
    settings = {"calendar": {"recurring": {"slot_minutes": 30,
                "closures": [{"date": "2026-12-25"}]}}}

    def _jsonb(colname):
        return migrated_connection.exec_driver_sql(
            f"SELECT {colname}, {colname}::text FROM clients WHERE id = %s",
            (str(seed_id),),
        ).fetchone()

    def _token_value():
        return migrated_connection.exec_driver_sql(
            f"SELECT {SCHEDULE_TOKEN_COLUMN} FROM clients WHERE id = %s",
            (str(seed_id),),
        ).fetchone()[0]

    import json
    try:
        migrated_connection.exec_driver_sql(
            "ALTER TABLE clients ADD COLUMN office_hours JSONB;"
            "ALTER TABLE clients ADD COLUMN settings JSONB;")
        migrated_connection.exec_driver_sql(
            "INSERT INTO clients (id, office_hours, settings)"
            " VALUES (%s, %s, %s)",
            (str(seed_id), json.dumps(office_hours), json.dumps(settings)))
        before = _jsonb("office_hours")
        settings_before = _jsonb("settings")

        _run_migration_file(migrated_connection, UP_010)
        assert SCHEDULE_TOKEN_COLUMN in _clients_columns(migrated_connection)
        assert _token_value() is None                    # token starts NULL
        assert _jsonb("office_hours") == before          # value + ::text bytes
        assert _jsonb("settings") == settings_before

        _run_migration_file(migrated_connection, DOWN_010)
        assert SCHEDULE_TOKEN_COLUMN not in _clients_columns(
            migrated_connection)
        assert _jsonb("office_hours") == before
        assert _jsonb("settings") == settings_before
    finally:
        migrated_connection.exec_driver_sql(
            "ALTER TABLE clients DROP COLUMN IF EXISTS "
            + SCHEDULE_TOKEN_COLUMN + ";"
            "ALTER TABLE clients DROP COLUMN IF EXISTS office_hours;"
            "ALTER TABLE clients DROP COLUMN IF EXISTS settings;")


# ---------------------------------------------------------------------------
# 011: appointments.internal_note (PHASE 3A Slice 4B1)
# ---------------------------------------------------------------------------

UP_011 = "011_appointment_internal_note_up.sql"
DOWN_011 = "011_appointment_internal_note_down.sql"

NOTE_CHECK = "ck_appointments_internal_note_len"


def _appointments_columns(connection) -> dict:
    """{column_name: (data_type, is_nullable)} for the throwaway schema's
    appointments table (the _clients_columns convention)."""
    rows = connection.exec_driver_sql(
        "SELECT column_name, data_type, is_nullable"
        " FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = 'appointments'",
        (SCHEMA,),
    ).fetchall()
    return {name: (data_type, nullable) for name, data_type, nullable in rows}


def test_011_adds_nullable_internal_note_and_existing_rows_stay_valid(
    migrated_connection,
):
    """011 adds EXACTLY one nullable text column to appointments; a row that
    already existed BEFORE the migration remains valid and reads back with
    internal_note = NULL; down removes exactly the column."""
    client_id, conversation_a, _, slot_1, _ = _seed_ids(
        migrated_connection)
    pre_existing = uuid.uuid4()
    _insert_appointment(migrated_connection, pre_existing, client_id,
                        slot_1, conversation_a)

    before = _appointments_columns(migrated_connection)
    assert "internal_note" not in before          # clean baseline
    try:
        _run_migration_file(migrated_connection, UP_011)
        after = _appointments_columns(migrated_connection)
        added = set(after) - set(before)
        assert added == {"internal_note"}
        assert after["internal_note"] == ("text", "YES")
        row = migrated_connection.exec_driver_sql(
            "SELECT internal_note FROM appointments WHERE id = %s",
            (str(pre_existing),),
        ).fetchone()
        assert row is not None and row[0] is None
    finally:
        _run_migration_file(migrated_connection, DOWN_011)
        assert _appointments_columns(migrated_connection) == before


def test_011_check_bites_below_the_application(migrated_connection):
    """With the application bypassed entirely (raw SQL), the database itself
    refuses a 2001-character note with SQLSTATE 23514 naming exactly our
    constraint, and accepts exactly 2000."""
    from sqlalchemy.exc import IntegrityError

    client_id, conversation_a, _, slot_1, _ = _seed_ids(
        migrated_connection)
    appointment_id = uuid.uuid4()
    _insert_appointment(migrated_connection, appointment_id, client_id,
                        slot_1, conversation_a)
    try:
        _run_migration_file(migrated_connection, UP_011)

        try:
            with pytest.raises(IntegrityError) as excinfo:
                migrated_connection.exec_driver_sql(
                    "UPDATE appointments SET internal_note = %s WHERE id = %s",
                    ("x" * 2001, str(appointment_id)),
                )
        finally:
            migrated_connection.exec_driver_sql("ROLLBACK")
        driver_error = excinfo.value.orig
        assert getattr(driver_error, "pgcode", None) == "23514"
        assert getattr(driver_error.diag, "constraint_name", None) == NOTE_CHECK

        migrated_connection.exec_driver_sql(
            "UPDATE appointments SET internal_note = %s WHERE id = %s",
            ("y" * 2000, str(appointment_id)),
        )
        stored = migrated_connection.exec_driver_sql(
            "SELECT char_length(internal_note) FROM appointments"
            " WHERE id = %s", (str(appointment_id),),
        ).fetchone()
        assert stored[0] == 2000
    finally:
        _run_migration_file(migrated_connection, DOWN_011)


def test_reapplying_011_fails_loudly(migrated_connection):
    """No IF NOT EXISTS: a second application must be a loud error, never a
    silent no-op (the 003..010 convention)."""
    from sqlalchemy.exc import ProgrammingError

    _run_migration_file(migrated_connection, UP_011)
    try:
        try:
            with pytest.raises(ProgrammingError):
                _run_migration_file(migrated_connection, UP_011)
        finally:
            migrated_connection.exec_driver_sql("ROLLBACK")
        migrated_connection.exec_driver_sql("SELECT 1")   # connection usable
    finally:
        _run_migration_file(migrated_connection, DOWN_011)


def test_011_down_preserves_every_other_appointment_byte(migrated_connection):
    """Down removes ONLY internal_note (destroying notes is what reversal
    means - stated, not hidden); every other column value on an existing row
    survives byte-for-byte, and up reapplies cleanly afterward."""
    client_id, conversation_a, _, slot_1, _ = _seed_ids(
        migrated_connection)
    appointment_id = uuid.uuid4()
    _insert_appointment(migrated_connection, appointment_id, client_id,
                        slot_1, conversation_a, status="confirmed")

    _run_migration_file(migrated_connection, UP_011)
    migrated_connection.exec_driver_sql(
        "UPDATE appointments SET internal_note = 'will be destroyed'"
        " WHERE id = %s", (str(appointment_id),))
    before = migrated_connection.exec_driver_sql(
        "SELECT id, client_id, slot_id, conversation_id, patient_name,"
        " patient_phone, status FROM appointments WHERE id = %s",
        (str(appointment_id),),
    ).fetchone()

    _run_migration_file(migrated_connection, DOWN_011)
    after = migrated_connection.exec_driver_sql(
        "SELECT id, client_id, slot_id, conversation_id, patient_name,"
        " patient_phone, status FROM appointments WHERE id = %s",
        (str(appointment_id),),
    ).fetchone()
    assert after == before
    assert "internal_note" not in _appointments_columns(migrated_connection)

    _run_migration_file(migrated_connection, UP_011)          # reapplies
    try:
        assert "internal_note" in _appointments_columns(migrated_connection)
    finally:
        _run_migration_file(migrated_connection, DOWN_011)


def test_011_orm_parity(migrated_connection):
    """The ORM column and the migrated column agree (Rule 3: the migration is
    the production authority; the mapping must mirror it exactly)."""
    from app.calendar_models import Appointment

    orm_column = Appointment.__table__.columns["internal_note"]
    assert orm_column.nullable is True
    assert str(orm_column.type).upper() == "TEXT"
    try:
        _run_migration_file(migrated_connection, UP_011)
        after = _appointments_columns(migrated_connection)
        assert after["internal_note"] == ("text", "YES")
    finally:
        _run_migration_file(migrated_connection, DOWN_011)
