# CHANGE REPORT — Mia Calendar MVP (Version 2: controlled slots)

Per Constitution Rule 13. No vague claims.

## Goal
Give Mia real appointment booking against staff-published slots: show
availability, hold a slot for 5 minutes, confirm, save to Supabase, and
notify the office — with double-booking defenses and every failure visible.
(Patient notification is NOT a goal of the current phase: patient SMS is an
open Critical finding — see the PATCH 1 section below. An earlier draft also
claimed "double-booking impossible"; that claim was withdrawn pending the
Patch 1 PostgreSQL concurrency tests, which have since run and passed
locally on 2026-07-12 — see the PATCH 1 verification status.)

## Scope decisions made (and why)
- Built **Version 2 (controlled slots)** exactly as the roadmap's "Most
  Realistic First Build". Phases 13–16 (external sync, multi-provider,
  multi-location, analytics) are NOT built — Rule 17 forbids them until this
  phase is stable and approved.
- **Removed the `appointment_holds` table** from the roadmap's schema: hold
  state lives only on the slot row (status/held_until/held_by). Two owners
  for "is this slot held?" violates Rule 3 and can drift.
- **No cron job for expired holds**: expiry is lazy (expired holds are
  treated as available everywhere and taken over safely). Documented in
  appointment_hold_service; removes hidden background behavior (Rule 4).
- **Intake is NOT re-implemented**: patient name/phone/reason come from
  Mia's existing lead capture (Rule 3 — intake has one owner). The booking
  flow starts only after intake completes.
- **No providers/services tables yet**: slots carry an optional display
  provider_name and optional service_key string (Rule 17).
- **Calendar models live at `app/calendar_models.py`** (flat module): the
  real project has `app/models.py` as a flat module, so an `app/models/`
  package would collide. Found by inspecting the actual repo, not the
  proposed tree.

## Files ADDED (no existing file is modified by this package)
- app/calendar_models.py — tables, statuses, booking states + full transition table
- app/repositories/appointment_repository.py — ALL calendar DB access; client_id on every query; FOR UPDATE lockers
- app/services/calendar_settings_service.py — named settings + defaults, timezone resolution, ensure_utc
- app/services/appointment_intent.py — date / time-preference / slot-choice / yes-no parsing (pure)
- app/services/availability_rules.py — pure availability filter (unit-tested without a DB)
- app/services/availability_service.py — fetch-then-filter wrapper; local-day → UTC window
- app/services/appointment_hold_service.py — place_hold / release_hold, atomic, lazy expiry
- app/services/booking_service.py — finalize_booking (in-lock recheck), cancel_appointment
- app/services/notification_service.py — office SMS/email; per-channel outcome recorded on the appointment. NOTE: the generated code also contains a patient-SMS path — Patch 1 did not modify it, it is NOT approved for production, and a later controlled patch must disable it for the current MVP (Senior Audit Critical #3). Approved notification behavior is office SMS and office email only.
- app/services/booking_conversation.py — the state machine; the ONLY thing chat.py calls
- app/routes/calendar.py — admin endpoints (publish/list/block slots; list/cancel appointments), X-Admin-Key protected
- migrations/001_calendar_mvp_up.sql / 001_calendar_mvp_down.sql
- calendar_tests/ — test_appointment_intent.py (10 pure tests), test_availability_rules.py (17 pure tests incl. the Patch 2A strict-boolean matrix and the Patch 2B DST-window/horizon/notice tests), conftest.py + test_booking_db.py (19 Postgres tests: the original 11 incl. a real threaded double-booking race, plus 6 Patch 1 concurrency/integrity tests, plus 2 Patch 2B DST-window tests) + test_migration_schema.py (4 tests running the actual migration SQL). Full suite: 50 tests, all passing locally as of 2026-07-12 (Patch 2B verification run).
- docs/INTEGRATION.md — the exact ~40-line additions to models.py, main.py, chat.py

## Files that REQUIRE your small manual edits (exact text in docs/INTEGRATION.md)
- app/models.py — 5 new Conversation columns (booking_state, booking_preferred_date, booking_time_preference, booking_offered_slot_ids, booking_selected_slot_id)
- app/main.py — mount calendar router; import calendar models
- app/routes/chat.py — ONE delegation block placed after the safety guard, before the lead-complete reply

## Database changes
- NEW tables: appointment_slots, appointments (with CHECK constraints on statuses and time order; indexes on (client_id, start_datetime) and conversation_id)
- conversations: 5 additive columns (nullable/defaulted — existing rows untouched)
- Migration is additive-only; down-script provided with export-first backup instructions

## Behavior added
Only for clients with `settings.calendar.booking_enabled = true`:
scheduling intent after completed intake enters the booking dialog
(day → morning/afternoon → up to 3 numbered slots → 5-minute hold →
yes/no confirm → appointment saved as PENDING → office SMS + office email —
the only approved notification channels).
NOTE: the generated code additionally contains a patient-SMS path ("request
received" wording). Patch 1 did not modify that path; it is NOT approved for
production and must be disabled by a later controlled patch before this MVP
serves real patients (Senior Audit Critical #3).

## Behavior intentionally unchanged
- Every office WITHOUT booking_enabled: bit-for-bit today's behavior (handler returns handled=False before touching anything).
- Emergency and safety flows: they run BEFORE the booking hook; the booking module additionally refuses emergency-flagged conversations and wipes its state.
- Lead capture, FAQ, info-intent, one-question-per-message, abuse guard, OpenAI fallback: untouched.
- Existing lead notifications in chat.py: untouched (migrating them into notification_service is a future, separate patch).

## Risks
1. Date-language ambiguity ("next thursday") — mitigated: Mia echoes the resolved full date twice before booking.
2. chat.py insertion point — chat.py is ~7,800 lines; the block's placement (after safety, before lead-complete reply) must be verified in your editor. If `accepted_schedule` isn't in scope there, drop that clause as documented.
3. Unapproved patient-SMS path — the generated notification code still contains a patient-SMS send that Patch 1 did not modify. It is not approved for production (no stored consent, no messaging compliance) and must be disabled by a later controlled patch before real patient traffic. Until then, only office SMS and office email are approved.
4. SQLAlchemy version — calendar models mirror your existing postgresql.UUID/JSONB style; if your installed SQLAlchemy differs from the one models.py runs on today, nothing new is required.
5. Hold takeover after expiry means a slow patient can lose a slot at exactly 5 minutes — by design; tune hold_minutes per client.

## Tests to perform (Rule 11 regression checklist)
Automated (run these):
- [x] Pure suites: parsing, availability rules, settings bounds (passing)
- [x] Full automated PostgreSQL suite (`pytest calendar_tests/ -v` with the safeguarded TEST_DATABASE_URL) — COMPLETED 2026-07-12 on a disposable PostgreSQL 16 container: 39 collected, 39 passed, 0 failed, 0 skipped, 0 errors. Covers holds, threaded double-booking races (same-conversation and same-slot), expired-hold takeover, finalize recheck, one-appointment-per-conversation, unique-index backstop, cancellation/rebooking both directions, client isolation, full conversation with failing notifications, emergency refusal, slot-sniped re-offer, and the real migration SQL (apply, enforce, re-apply fails loudly, down/up round-trip)
Manual (after wiring, on a staging client):
- [ ] New feature happy path: publish 3 slots, book one end-to-end in the widget
- [ ] Previous feature: office WITHOUT calendar settings behaves exactly as before (lead capture ending unchanged)
- [ ] Emergency flow: "difficulty breathing" mid-booking → emergency reply, booking state cleared
- [ ] Urgent flow: priority lead books; appointment.urgency == "priority"
- [ ] One-question-per-message: every booking reply asks at most one question (read each)
- [ ] Answer-first: "my gums are swollen, can I come in?" → safety/answer first, then flow
- [ ] Intake interruption: start booking, ask "what are your hours?" mid-flow → FAQ path still answers (booking_state resumes on next scheduling message)
- [ ] Notification behavior: confirm office SMS + office email arrive once, exactly once (the only approved channels); verify NO patient SMS reaches a real patient — the unapproved patient-SMS path in the generated code must stay out of production until a later patch disables it; unplug Twilio creds → appointment still books, notify_error populated, admin list shows it
- [ ] Client isolation: office B's admin calls with office A's ids → 404s
- [ ] Duplicate prevention: send "yes" twice fast → one appointment; "book again" → restated

## Rollback method
1. Remove the chat.py delegation block and the import (or set every client's booking_enabled=false — instant behavioral rollback with zero deploys).
2. Remove the router mount + models import from main.py.
3. Revert the 5-column models.py addition.
4. Export appointments/appointment_slots, then run migrations/001_calendar_mvp_down.sql.

## Stop point (Rule 18)
This is the checkpoint. Automated Patch 1 testing is COMPLETE (full suite:
39 passed, 0 failed, 0 skipped, 0 errors — verified locally 2026-07-12).
The MANUAL staging/widget regression checklist above remains PENDING and
must be completed on a staging client after wiring, before production.
Cancellation-via-chat, computed availability from office hours, and external
calendar sync all WAIT for your approval as separate phases.

---

# PATCH 1 — DATABASE INTEGRITY (Senior Audit Critical #1 and #9)

Per Constitution Rule 13. No vague claims.

## Goal
Make "one active appointment per conversation" and "one active appointment
per slot" true at the DATABASE level; turn the racing unique violation into
a deterministic booking outcome instead of a 500; make the destructive test
fixture incapable of dropping a non-test database; and test the ACTUAL SQL
migration instead of only ORM create_all().

## Files changed
- migrations/002_calendar_integrity_hardening_up.sql   (NEW)
- migrations/002_calendar_integrity_hardening_down.sql (NEW)
- app/calendar_models.py                                (Appointment.__table_args__ added)
- app/services/booking_service.py                       (IntegrityError classification)
- calendar_tests/conftest.py                            (destructive-test safety gate; corrected false migration-coverage comment)
- calendar_tests/test_booking_db.py                     (6 new tests appended; existing tests untouched)
- calendar_tests/test_migration_schema.py               (NEW — runs the real migration SQL)
- CHANGE_REPORT.md                                      (this section; withdrew the unverified "impossible" claim)

## Functions changed
- booking_service.finalize_booking — added `except IntegrityError` branch
  (before the existing generic handler) mapping ONLY PostgreSQL SQLSTATE
  23505 on the two named indexes; everything else re-raises.
- booking_service._classify_booking_unique_violation — NEW pure helper.
- conftest.validate_disposable_test_db — NEW safety validator (also used by
  test_migration_schema.py).
- conftest.engine fixture — now refuses non-local / non-"test" / unflagged
  databases before create_all/drop_all.

## Database changes
Two PARTIAL UNIQUE indexes on appointments (migration 002, additive only,
no IF NOT EXISTS so drift fails loudly; reversible via 002 down):
- uq_active_appointment_per_conversation ON (conversation_id)
  WHERE conversation_id IS NOT NULL AND status <> 'cancelled'
- uq_active_appointment_per_slot ON (slot_id)
  WHERE status <> 'cancelled'
Mirrored in Appointment.__table_args__ with BOTH postgresql_where and
sqlite_where so no dialect silently degrades to a full unique index.

## Behavior added
- A concurrent duplicate finalize for the SAME conversation (two different
  slots) now loses at the database and receives reason
  `already_booked_by_conversation` carrying the winning appointment.
- A concurrent insert for an already-taken slot that somehow bypasses the
  slot lock now loses at the database; through finalize_booking it maps to
  the existing `hold_lost` reason.
- SQLite IntegrityErrors and any unknown constraint/SQLSTATE RE-RAISE
  (approved decision: no SQLite message parsing; PostgreSQL is the
  concurrency source of truth).
- Database tests refuse to run against anything that is not
  localhost + a database whose name contains "test" +
  ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes.

## Behavior intentionally unchanged
- chat.py, notifications, patient consent language, widget UI, availability
  rules, date parsing, routes/tenant auth, staff-confirmation behavior.
- The BookingResult failure vocabulary (no new reason strings), so
  booking_conversation.py needed no changes.
- Migration 001 (untouched).
- Patient-SMS behavior was not modified during Patch 1. Patient SMS remains
  an open Critical finding (Senior Audit #3) and must not be enabled in
  production until explicit consent storage and messaging compliance are
  implemented.

## Risks
- If a production database already contains violating rows, migration 002
  fails at CREATE UNIQUE INDEX (by design). Resolve duplicates, re-run.
- The per-slot index treats completed/no_show as still consuming the slot;
  if that product rule changes, the predicate must change with it.
- CREATE UNIQUE INDEX takes a table lock briefly; run 002 off-peak.

## Tests to perform
    pip install pytest sqlalchemy psycopg2-binary
    docker run -d -p 5433:5432 -e POSTGRES_PASSWORD=test \
        -e POSTGRES_DB=mia_calendar_test postgres:16
    # pure unit tests (no DB needed):
    pytest calendar_tests/test_appointment_intent.py \
           calendar_tests/test_availability_rules.py -v
    # database + concurrency + migration tests:
    ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes \
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/mia_calendar_test \
    pytest calendar_tests/ -v

## Rollback method
1. Revert the code changes (calendar_models.py, booking_service.py,
   conftest.py, tests) to the pre-patch versions.
2. Run migrations/002_calendar_integrity_hardening_down.sql (idempotent).
No data is modified by either direction.

## Verification status (honest, per Rule 19)
- Executed in the patch-authoring environment: Python syntax compilation of
  every changed/added .py file. (That environment had no network access, so
  no tests could run there.)
- VERIFIED LOCALLY by the project owner on 2026-07-12:
    Environment: disposable PostgreSQL 16 Docker container, local virtual
    environment, no production Supabase credentials or patient data used.
    Results: 39 tests collected; 18 pure unit tests passed; complete
    Calendar suite 39 passed in 1.84s; 0 failed, 0 skipped, 0 errors.
    The PostgreSQL concurrency, uniqueness, cancellation/rebooking, and
    migration tests all ran and passed.
- Status: Patch 1 is VERIFIED LOCALLY. The two database invariants are now
  test-proven on PostgreSQL: the same-conversation double-finalize race
  loses deterministically at uq_active_appointment_per_conversation, and
  the per-slot index holds even when application logic is bypassed.
  "Verified locally" is the precise claim — production rollout still
  requires running migration 002 against the production database (off-peak;
  it fails loudly if violating rows pre-exist) and the Rule 11 regression
  checklist above.
- CHECKPOINT (Rule 18): Patch 1 closed 2026-07-12 with owner approval.
  Rollback point: pre-patch file versions + migrations/002 down-script.
  Patch 2 NOT started — awaiting explicit approval and scope.

---

# PATCH 2A — STRICT CALENDAR BOOLEAN SETTINGS (Senior Audit Critical #6)

Per Constitution Rule 13.

## Goal
Make the calendar opt-in impossible to enable by accident: booking_enabled
and require_staff_confirmation accept ONLY real JSON booleans. Truthiness
parsing (bool(value)) treated the string "false" as True, which could
silently turn on patient booking for an office that never opted in.

## Files changed
- app/services/calendar_settings_service.py  (strict-bool helper + 2 call sites + header contract note)
- calendar_tests/test_availability_rules.py   (17-case matrix appended; existing tests untouched)
- CHANGE_REPORT.md                            (this section)

## Functions changed
- NEW: _read_strict_bool(raw, key, default) — owned by
  calendar_settings_service.py. Returns the value only if
  isinstance(value, bool); missing key or any other type returns the flag's
  fail-safe default. isinstance is required (not equality/membership):
  1 == True in Python, so looser checks would accept the integers 1/0.
- load_calendar_settings — the two boolean fields now use _read_strict_bool
  instead of bool(raw.get(...)). bool() no longer appears anywhere in
  configuration parsing. Nothing else in the function changed.

## Database changes
None. No migration, no model change.

## Behavior added
- booking_enabled: JSON true -> True; JSON false / missing / "true" /
  "false" / "yes" / "no" / 1 / 0 / null / any other type -> False.
- require_staff_confirmation: JSON false -> False (the only way to disable);
  JSON true / missing / "true" / "false" / 0 / null / any other type -> True.
- The two flags fall back in OPPOSITE directions on malformed input because
  their safe directions are opposite: garbage cannot open booking, and falsy
  garbage cannot switch off the pending-confirmation safety.
- Malformed values fall back SILENTLY to the documented default — no logging
  was added, by explicit approval decision for this isolated patch; logging
  may be considered separately later.

## Behavior intentionally unchanged
- Integer settings (_read_int), timezone resolution, client_now, ensure_utc,
  the CalendarSettings dataclass, and every documented default value.
- chat.py, booking_service.py, booking_conversation.py, availability rules,
  migrations, models, routes, notifications, patient SMS, consent, widget,
  tenant authentication, and ALL Patch 1 files/behavior (verified untouched
  by checksum against the Patch 1 archive).

## Risks
1. Offices currently "enabled" only via the bug (booking_enabled stored as
   "true", 1, "yes", ...) become DISABLED until staff writes JSON true —
   the intended outcome of the fix, but a visible change for misconfigured
   rows. Run the read-only audit query below BEFORE deploying and correct
   any rows it returns.
2. Offices whose require_staff_confirmation was silently OFF via falsy
   garbage (0, "") flip back ON (appointments save as pending) — the
   fail-safe direction. The same query detects these rows.

## Rollout audit query (READ-ONLY, documentation only — do not run from
## this patch; detects malformed calendar settings of BOTH flags and a
## non-object calendar key)
    SELECT
        id,
        settings->'calendar' AS calendar_settings
    FROM clients
    WHERE
        settings ? 'calendar'
        AND (
            jsonb_typeof(settings->'calendar') IS DISTINCT FROM 'object'
            OR (
                jsonb_typeof(settings->'calendar') = 'object'
                AND (
                    (
                        (settings->'calendar') ? 'booking_enabled'
                        AND jsonb_typeof(
                            settings->'calendar'->'booking_enabled'
                        ) IS DISTINCT FROM 'boolean'
                    )
                    OR
                    (
                        (settings->'calendar') ? 'require_staff_confirmation'
                        AND jsonb_typeof(
                            settings->'calendar'->'require_staff_confirmation'
                        ) IS DISTINCT FROM 'boolean'
                    )
                )
            )
        );

## Tests added (17 cases/assertions in 3 test functions,
## calendar_tests/test_availability_rules.py)
- test_booking_enabled_strict_boolean_matrix — 9 cases:
  True->True; False->False; missing->False; "false"->False; "true"->False;
  1->False; 0->False; None->False; "yes"->False (the audit's example).
- test_require_staff_confirmation_strict_boolean_matrix — 7 cases:
  True->True; False->False; missing->True; "false"->True; "true"->True;
  0->True; None->True.
- test_consumer_contract_malformed_opt_in_is_refused — 1 assertion
  (consumer-contract, NOT end-to-end: booking_conversation.py is not
  executed): proves the gate expression `not settings.booking_enabled`
  refuses booking_enabled="true".

## Tests to perform
    # pure suites (no DB):
    pytest calendar_tests/test_appointment_intent.py \
           calendar_tests/test_availability_rules.py -v
    # full regression incl. Patch 1 PostgreSQL + migration suites:
    ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes \
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/mia_calendar_test \
    pytest calendar_tests/ -v

## Rollback method
Revert calendar_settings_service.py and test_availability_rules.py to their
pre-2A versions. No migration to roll back; no data touched.

## Verification status (honest, per Rule 19)
- Executed in the patch-authoring environment (pure Python, no external
  packages needed): calendar_tests/test_availability_rules.py via its
  built-in runner — 11/11 test functions PASS, including the 3 new ones
  containing all 17 matrix assertions; calendar_tests/
  test_appointment_intent.py — 10/10 PASS. Compile checks pass.
- VERIFIED LOCALLY by the project owner on 2026-07-12:
    Environment: disposable PostgreSQL 16 Docker container, fresh local
    virtual environment, no production Supabase credentials or patient
    data used.
    Results: 42 tests collected; 21 pure tests passed; complete Calendar
    suite 42 passed in 1.95s; 0 failed, 0 skipped, 0 errors.
    The Patch 1 PostgreSQL concurrency, uniqueness, cancellation/rebooking,
    and migration tests all re-ran and passed as regression alongside the
    17 new strict-boolean assertions.
- Status: Patch 2A is VERIFIED LOCALLY. Strict JSON-boolean parsing of
  booking_enabled and require_staff_confirmation is test-proven; truthy
  strings can no longer silently enable booking (Critical #6 closed at the
  code level). "Verified locally" is the precise claim — before production
  rollout, run the read-only rollout audit query above and correct any
  malformed rows it returns, since offices enabled only via the old bug
  become disabled.
- CHECKPOINT (Rule 18): Patch 2A closed 2026-07-12 with owner approval.
  Rollback point: pre-2A calendar_settings_service.py and
  test_availability_rules.py (no migration involved).
- Patch 2B and all other audit findings: NOT started — awaiting explicit
  approval and scope.

---

# PATCH 2B — DST-SAFE LOCAL-DAY BOUNDARIES AND BOOKING-HORIZON CONSISTENCY
# (Senior Audit Critical #7 + Recommended #4)

Per Constitution Rule 13.

## Goal
1) Local-day database windows must reflect the TRUE length of a local
calendar day: local dates containing an offset transition in the configured
timezone are 23 or 25 hours, so end_utc must come from the NEXT local
midnight converted independently — never from start_utc + 24 hours.
2) The availability filter's booking horizon must use the same local-
calendar-date arithmetic the booking conversation already uses to accept a
date, so a date Mia accepts can never come back empty for horizon reasons.

## Files changed
- app/services/calendar_settings_service.py  (NEW helper local_day_utc_window;
  max_booking_days floor 1 -> 0; imports widened)
- app/services/availability_service.py       (window from the helper)
- app/services/availability_rules.py         (local-date horizon; aware-UTC
  normalization of now; local_start computed once)
- app/routes/calendar.py                     (both admin listings use the
  helper; orphaned time/timedelta imports removed)
- calendar_tests/test_availability_rules.py  (6 new pure tests + 1 loader
  assertion inside the existing settings test)
- calendar_tests/test_booking_db.py          (2 new PostgreSQL tests)
- CHANGE_REPORT.md                           (this section)

## Functions changed
- NEW: calendar_settings_service.local_day_utc_window(day, timezone_name)
  -> (start_utc, end_utc). THE single owner of local-day UTC boundaries
  (Rule 3): both local midnights constructed independently, each converted
  to UTC independently, half-open contract start <= t < end.
- calendar_settings_service.load_calendar_settings — max_booking_days floor
  changed from 1 to 0 ("today only" is now a real configurable value). The
  Patch 2A strict-boolean behavior is untouched.
- availability_service.get_available_slots — window now from the helper.
  find_days_with_availability inherits the fix transitively (it delegates);
  its behavior on transition days improves with no separate change.
- availability_rules.filter_bookable_slots — (a) normalized_now =
  ensure_utc(now_utc); min_start, today_local, and every derived value come
  only from normalized values; (b) horizon rule is now: slot's LOCAL date
  <= today_local + max_booking_days — identical arithmetic to
  booking_conversation._validate_and_store_date(), which is NOT modified
  and serves as the contract; (c) local_start computed once and shared by
  the horizon and time-preference checks. Minimum notice remains an exact
  elapsed-time rule and still rejects all past slots.
- routes/calendar.list_slots — daily window from the helper.
- routes/calendar.list_appointments — start of start_day and end of end_day
  each from the helper (the old form added 24h AFTER converting end_day's
  midnight, wrong whenever end_day -> end_day+1 crossed a transition).

## Database changes
None. No migration, no model change, no repository change —
list_slots_between / list_appointments_between already implement
start_utc <= t < end_utc, so the existing interfaces accept the corrected
boundaries as-is.

## Behavior added
- The three local-day query boundaries (availability fetch, admin slot
  listing, admin appointment listing) no longer derive end_utc by adding
  24 hours to start_utc; they use the helper's independently-converted
  midnights. On local dates containing an offset transition in the
  configured timezone: the 25-hour day's final hour becomes visible
  (previously silently lost), and the 23-hour day stops listing the next
  local date's first hour (previously double-listed).
- Horizon: the ENTIRE final allowed local date is bookable. Under the old
  exact-instant rule (now + N days), slots on the final date LATER than the
  current clock time were wrongly rejected after the conversation had
  accepted the date. Slots on the following local date remain rejected —
  the old rule also rejected the tested next-day-morning case (08:00 local
  < the 09:00 boundary instant), so that case is proof of the boundary, not
  a behavior change.
- max_booking_days=0 is now configurable and means today's local date only.

## Behavior intentionally unchanged
- booking_conversation.py (its accepted-date rule IS the contract),
  booking_service.py, appointment_hold_service.py, appointment_repository.py,
  models, migrations, chat.py, notifications, patient SMS, consent, tenant
  authentication, widget files, service identifiers, stale-slot validation,
  staff confirmation.
- Patch 1 constraints/tests and Patch 2A strict-boolean parsing (verified
  by checksum/diff at delivery).
- Time-preference buckets, service filtering, sorting, max_offered_slots
  capping, hold logic, and minimum-notice semantics.

## Risks
1. Slot visibility changes on local dates containing an offset transition
   in the configured timezone — the intended fix; staff and patient views
   shift together, staying consistent.
2. The final horizon day becomes fully bookable (up to ~15 additional
   bookable hours on that date). This matches what Mia already tells
   patients; it is a visible behavior change and is deliberate.
3. Offices configured (or later configured) with max_booking_days=0 now get
   "today only" instead of being silently clamped to 1 day.
4. The route tests invoke the endpoint functions directly with a session,
   bypassing FastAPI transport auth; window logic is identical either way
   and auth coverage is unchanged.

## Tests added (8 new test functions; expected collection 42 -> 50)
Pure (test_availability_rules.py):
- test_local_day_window_normal_day — 24h NY window with exact UTC
  timestamps; PLUS Los Angeles assertions proving timezone_name is honored.
- test_local_day_window_spring_forward_is_23_hours — 2026-03-08:
  05:00Z -> 04:00Z next day, exactly 23h.
- test_local_day_window_fall_back_is_25_hours — 2026-11-01:
  04:00Z -> 05:00Z next day, exactly 25h.
- test_horizon_full_final_local_date_allowed — final local date 2026-08-10:
  8:00 AM local (earlier than now's 9:00 clock time) accepted; 7:30 PM local
  accepted (the case the old instant rule broke); 2026-08-11 8:00 AM
  rejected (rejected under the old rule too — proves the next local date is
  out).
- test_horizon_zero_days_allows_today_only — later-today accepted, tomorrow
  rejected with max_booking_days=0.
- test_minimum_notice_is_exact_elapsed_minutes — 59 min rejected, exactly
  60 accepted, 61 accepted, past rejected.
- (inside existing test_settings_defaults_and_bounds): configured JSON 0
  survives the loader as 0.
Database (test_booking_db.py, PostgreSQL):
- test_availability_window_covers_full_fallback_local_day — 23:59 local on
  the 25-hour day (immediately before end_utc) offered; a slot exactly at
  end_utc excluded.
- test_admin_routes_use_dst_safe_windows — list_slots on 2026-03-08
  includes 23:00 local, excludes local-midnight Mar 9; list_appointments
  over Mar 7..Mar 8 (range crossing the spring transition) includes an
  appointment at 23:59 local on end_day and excludes one exactly at the
  following local midnight.

## Tests to perform
    pytest calendar_tests/test_appointment_intent.py \
           calendar_tests/test_availability_rules.py -v
    ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes \
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/mia_calendar_test \
    pytest calendar_tests/ -v
Expected: 50 collected, 50 passed, 0 failed, 0 skipped (PostgreSQL required
— no database test may be skipped for verification to count).

## Rollback method
Revert the four app files and two test files to their Patch 2A checkpoint
versions. No migration involved; no data touched.

## Verification status (honest, per Rule 19)
- Executed in the patch-authoring environment: the pure suites via their
  built-in runners — test_availability_rules.py 17/17 PASS (incl. all 6 new
  Patch 2B tests, with exact DST timestamps verified against real zoneinfo
  data) and test_appointment_intent.py 10/10 PASS; compile checks on all
  changed files. (That environment has no pytest/PostgreSQL, so the full
  suite could not run there.)
- VERIFIED LOCALLY by the project owner on 2026-07-12:
    Environment: disposable PostgreSQL 16 Docker container, fresh local
    virtual environment, no production Supabase credentials or patient
    data used.
    Results: 50 tests collected; 27 pure tests passed; complete Calendar
    suite 50 passed in 2.20s; 0 failed, 0 skipped, 0 errors.
    The DST-safe local-day boundary tests, maximum booking-horizon tests,
    minimum-notice boundary tests, PostgreSQL tests, migration tests, and
    all Patch 1 and Patch 2A regressions ran and passed; no database test
    was skipped.
- Status: Patch 2B is VERIFIED LOCALLY. The three local-day query
  boundaries are test-proven DST-safe (23h/25h days query their true
  boundaries, half-open), and the booking horizon now matches the
  conversation's local-date contract — a date Mia accepts can no longer
  come back empty for horizon reasons. Critical #7 and Recommended #4 are
  closed at the code level. "Verified locally" is the precise claim; the
  manual staging/widget regression checklist earlier in this report remains
  the pre-production gate.
- CHECKPOINT (Rule 18): Patch 2B closed 2026-07-12 with owner approval.
  Rollback point: Patch 2A checkpoint versions of the four app files and
  two test files (no migration involved).
- Patch 2C and all other audit findings: NOT started — awaiting explicit
  approval and scope.

---

# PATCH 2C — STALE OFFERED-SLOT REVALIDATION AND OFFER EXPIRATION
# (Senior Audit Critical #8)

Per Constitution Rule 13.

## Goal
A slot judged eligible when DISPLAYED could be selected or finalized hours
later after becoming ineligible (notice crossed, horizon shortened, service
or preference no longer matching), and the pre-hold offer itself had no
lifetime. Patch 2C: one pure policy owner re-judges every slot UNDER the
existing slot-row lock at hold creation and at final booking, and the offer
gets an explicit bounded lifetime.

## Files changed
- app/services/availability_rules.py    (SlotPolicyResult + evaluate_slot_policy;
  filter_bookable_slots delegates its four policy rules to it)
- app/services/appointment_hold_service.py (keyword-only policy context;
  under-lock revalidation; slot_ineligible + detail; ineligible slot never
  mutated)
- app/services/booking_service.py       (keyword-only policy context;
  under-lock revalidation after the hold recheck; ineligible -> the owned
  hold is released and COMMITTED in the same transaction, no appointment
  inserted; slot_ineligible + detail)
- app/services/booking_conversation.py  (BOOKING_OFFER_TTL_MINUTES = 30;
  offer expiry set/check/clear/replace; effective-preference lifecycle;
  keyword call sites; approved accurate wording)
- app/models.py                         (+2 nullable Conversation columns)
- migrations/003_offer_expiration_up.sql / _down.sql (NEW)
- calendar_tests/test_availability_rules.py (2 new pure tests)
- calendar_tests/test_booking_db.py     (12 new PostgreSQL tests; existing
  call sites converted to the keyword-only signatures — syntax only)
- calendar_tests/test_migration_schema.py (4 new self-contained 003 tests)
- CHANGE_REPORT.md                      (this section)

## Functions changed
- NEW availability_rules.evaluate_slot_policy(slot, *, now_utc, settings,
  time_preference, service_key) -> SlotPolicyResult(eligible, reason) —
  THE single pure owner of the notice / horizon / preference / service
  rules (semantics unchanged from Patch 2B; only ownership moved). Reasons:
  ok / too_soon / beyond_horizon / preference_mismatch / service_mismatch.
- availability_rules.filter_bookable_slots — status/hold checks, ordering,
  and max_offered_slots cap unchanged; the four policy rules now delegated.
- appointment_hold_service.place_hold(db, client_id, slot_id,
  conversation_id, *, settings, time_preference, service_key, now_utc) —
  keyword-only, no permissive defaults; revalidates under the lock; new
  reason slot_ineligible with detail; HoldResult gains detail.
- booking_service.finalize_booking(db, client_id, slot_id, conversation_id,
  *, settings, now_utc, time_preference, service_key, ...patient fields) —
  keyword-only; revalidates under the lock after the hold recheck; on
  ineligibility releases this conversation's verified hold (available/
  NULL/NULL) and commits in the same transaction, inserts nothing, returns
  slot_ineligible + detail; BookingResult gains detail. Patch 1 duplicate
  pre-check and IntegrityError classification untouched.
- booking_conversation: BOOKING_OFFER_TTL_MINUTES = 30 (fixed MVP value,
  owned here); _offer_is_expired (ensure_utc on BOTH sides; now >= expires
  -> expired; NULL expiry with offered IDs -> expired);
  _revalidation_preference (effective preference reader, one owner);
  _offer_slots sets booking_offer_expires_at = ensure_utc(now) + TTL and
  booking_effective_time_preference (PREF_ANY when relaxed);
  _handle_slot_selection gains the expiry gate (clears ALL THREE stale
  values, generates a replacement offer, meta reason offer_expired);
  hold success clears offered ids + expiry (held_until becomes the only
  expiration authority) and PRESERVES the effective preference;
  _reoffer_after_conflict gains the approved accurate sentence
  "I'm sorry — that time is no longer available." for slot_ineligible
  (no channel claim); the "no"/abandonment branch and _clear_booking_state
  and _suggest_other_days clear both new fields; successful booking clears
  the effective preference via _clear_booking_state.

## Database changes
Migration 003 (additive, reversible): conversations gains
booking_offer_expires_at (timestamptz NULL) and
booking_effective_time_preference (varchar NULL). Strict up (no IF NOT
EXISTS); idempotent down. Migrations 001 and 002 untouched (test-proven).
No repository change: get_slot_for_update and existing queries sufficed.

## Behavior added
- Current booking policy is revalidated under the slot-row lock at hold
  creation and again at final booking; a stale offered slot can no longer
  bypass notice, horizon, service, or preference rules (Critical #8).
- Finalize-time ineligibility creates NO appointment and releases the
  conversation-owned hold atomically instead of leaving it to time out.
- The pre-hold offer expires 30 minutes after display (boundary: now <
  expires -> usable; now >= expires -> expired; NULL with offered IDs ->
  expired). Expired offers are cleared and replaced with current times;
  no slot can be held from a stale menu.
- Relaxed offers are honored end-to-end: the EFFECTIVE preference (PREF_ANY)
  is recorded with the offer, survives the hold, and is what finalization
  revalidates against; it is cleared after booking/reset/abandonment or
  replacement.
- Settings visibility, documented precisely: settings are loaded as a fresh
  request-level snapshot at the beginning of each patient message. Patch 2C
  does not lock the client row or guarantee visibility of an admin edit
  occurring after that read but before the slot-row lock. Settings and slot
  state are NOT one atomic database snapshot.

## Behavior intentionally unchanged
- Rule DEFINITIONS: minimum-notice exact-hours semantics, Patch 2B
  local-date horizon, preference buckets, service-filter equality, result
  ordering and cap, current service-key strategy.
- Patch 1 unique indexes/migration 002 and concurrency handling; Patch 2A
  strict booleans (calendar_settings_service.py byte-identical to 2B);
  Patch 2B DST windows (availability_service.py and routes byte-identical
  to 2B); appointment_repository.py; chat.py; tenant auth; admin
  authorization; staff confirmation; notification behavior; patient SMS
  (still unapproved); consent; external booking precedence; widget files;
  date parsing; cancellation lifecycle.

## Risks
1. Long-idle conversations now get one forced menu refresh (offer TTL) —
   visible, truthful, by design. Pre-2C in-flight offers (NULL expiry)
   self-heal the same way exactly once.
2. Genuinely stale selections that previously booked will now re-offer —
   the intended fix; patients see accurate wording, never a false success.
3. New required keyword-only parameters are a breaking API change for any
   external caller of place_hold/finalize_booking; all in-repo callers are
   updated, and the loud TypeError is preferred over silent defaults.
4. Hold-release-on-ineligibility commits inside finalize_booking; the
   IntegrityError handler cannot be reached on that path (no insert), so
   Patch 1 semantics are unaffected.

## Tests added (18 new test functions; collection 50 -> 68)
Pure: test_policy_owner_reason_matrix;
test_display_filter_delegates_to_policy_owner.
PostgreSQL: test_hold_rejects_slot_past_minimum_notice;
test_hold_rejects_after_horizon_shrunk; test_hold_rejects_service_mismatch;
test_hold_rejects_preference_mismatch; test_hold_succeeds_when_still_eligible;
test_finalize_rejects_when_notice_crossed_after_hold;
test_finalize_rejects_after_settings_change (approved condition 5, all six
assertions); test_finalize_succeeds_when_still_eligible;
test_relaxed_offer_holds_and_finalizes (approved condition 1 assertions,
notification fake called exactly once);
test_finalize_rejection_recovers_without_notifying (approved condition 6,
fake called zero times); test_offer_valid_immediately_before_expiry;
test_offer_expired_at_boundary_after_and_null (AT boundary, after, and the
approved condition-2 NULL-expiry regression — no stale slot ever held).
Migration: test_003_adds_offer_columns_with_correct_types;
test_reapplying_003_fails_loudly (self-applying, ROLLBACK + SELECT 1);
test_003_down_removes_columns_and_preserves_001_002;
test_003_up_reapplies_after_down. Each 003 test is self-contained,
individually runnable, and removes 003 in cleanup.

## Tests to perform
    pytest calendar_tests/test_appointment_intent.py \
           calendar_tests/test_availability_rules.py -v
    ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes \
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/mia_calendar_test \
    pytest calendar_tests/ -v
Expected: 68 collected, 68 passed, 0 failed, 0 skipped, 0 errors
(PostgreSQL required; no database test may be skipped).
Production rollout order: run migrations/003_offer_expiration_up.sql BEFORE
deploying the code (the code writes the new columns on every offer).

## Rollback method
1. Revert to the Patch 2B checkpoint versions: availability_rules.py,
   appointment_hold_service.py, booking_service.py, booking_conversation.py,
   models.py, test_availability_rules.py, test_booking_db.py,
   test_migration_schema.py.
2. Run migrations/003_offer_expiration_down.sql (idempotent DROP COLUMN IF
   EXISTS; no data transformation; 001/002 untouched both directions).
3. Order-independence: pre-2C code never reads the new columns, so a
   code-only rollback with columns left in place is also safe.

## Verification status (honest, per Rule 19)
- Executed in the patch-authoring environment: pure suites via built-in
  runners — test_availability_rules.py 19/19 PASS (incl. both new policy
  tests) and test_appointment_intent.py 10/10 PASS; compile checks on every
  changed file; byte-identity checks proving calendar_settings_service.py,
  availability_service.py, routes/calendar.py match the 2B checkpoint and
  appointment_repository.py / migrations 001-002 / calendar_models.py are
  untouched; live re-checks of 2A strict booleans and 2B DST windows.
- NOT executed in the patch-authoring environment (no pytest/PostgreSQL
  available there): the full 68-test pytest run. It has since been executed
  locally by the project owner — see below.
- VERIFIED LOCALLY by the project owner on 2026-07-12:
    Environment: fresh local Python virtual environment, disposable
    PostgreSQL 16 Docker container, no production Supabase credentials,
    no production patient data, no external SMS or email provider executed.
    Final results: 68 tests collected; 29 pure tests passed; complete
    Calendar suite 68 passed in 3.06s; 0 failed, 0 skipped, 0 errors.
    No PostgreSQL test was skipped.
- Failure-correction pass (ONE, test-only, inside Patch 2C): the initial
  complete-suite run collected 68 and finished 66 passed, 2 failed,
  0 skipped, 0 collection errors. Both failures were test setup/call-site
  mistakes in calendar_tests/test_booking_db.py, NOT production-code
  defects:
    1. test_slot_taken_between_display_and_selection — the pre-2C test
       manually stored booking_offered_slot_ids without the new Patch 2C
       offer metadata, so the NULL expiration was correctly treated as
       expired (the intended safety contract). Corrected the SETUP to
       represent a valid unexpired offer: booking_offer_expires_at set to
       an aware-UTC timestamp 30 minutes in the future, and
       booking_effective_time_preference = "any". The original "just taken"
       hold-conflict assertions were preserved unchanged.
    2. test_slot_unique_index_enforced_when_lock_bypassed_sequential — the
       repository-direct bypass call incorrectly passed _finalize_kwargs(),
       which now carries the Patch 2C service-layer arguments
       time_preference and service_key that the repository does not accept
       (TypeError). Corrected the CALL to pass only the repository-supported
       appointment arguments explicitly. The repository signature,
       production code, and the unique-index assertions were preserved
       unchanged.
  During this pass NO production file changed (the only modified file was
  calendar_tests/test_booking_db.py) and NO existing assertion was
  weakened. After the corrections the two targeted tests passed and the
  complete suite passed 68/68 in 3.06s.
- No external SMS/email provider can run during tests: office notifications
  are replaced by counting fakes via test-side monkeypatching only.
- Status: Patch 2C is VERIFIED LOCALLY. Under-lock policy revalidation at
  hold creation and finalization, atomic hold release on finalize-time
  ineligibility, and the 30-minute offer lifetime (including the
  NULL-expiry safety contract) are test-proven on PostgreSQL alongside all
  Patch 1 / 2A / 2B regressions — Critical #8 closed at the code level.
  "Verified locally" is the precise claim; before production rollout:
  (a) migrations/003_offer_expiration_up.sql MUST still be applied BEFORE
  deploying the Patch 2C code (the code writes the new columns on every
  offer), and (b) the manual staging/widget regression checklist earlier in
  this report remains a pre-production requirement.
- CHECKPOINT (Rule 18): Patch 2C closed 2026-07-12 with owner approval.
  Rollback point: the verified Patch 2B checkpoint file versions, plus
  running migrations/003_offer_expiration_down.sql if migration 003 has
  been applied (idempotent; migrations 001/002 untouched in both
  directions).
- Patch 2D and all other audit findings: NOT started — awaiting explicit
  approval and scope.

---

# PATCH 2D — DISABLE PATIENT SMS; PRESERVE OFFICE NOTIFICATIONS
# (Senior Audit Critical #3)

Per Constitution Rule 13. No vague claims. No legal or regulatory compliance
is claimed anywhere in this section — only implemented technical behavior
and current product policy.

## Goal
Senior Audit Critical #3: "Patient SMS is sent without a stored patient SMS
opt-in." send_booking_notifications unconditionally texted
appointment.patient_phone after every successful booking (PENDING and
CONFIRMED wordings). Current product policy: SMS is for authorized
dental-office staff notifications only; Mia collects the patient's phone
number for office follow-up, which is not consent for automated patient
texting. Patch 2D removes the patient-SMS send entirely, preserves office
SMS + office email exactly, and persists an honest patient-SMS outcome.
No patient consent feature is introduced.

## Files changed
- app/services/notification_service.py          (patient-SMS send removed;
  documentation corrected to match; build_patient_sms marked FUTURE-ONLY)
- calendar_tests/test_notification_policy.py    (NEW — 9-test policy matrix)
- calendar_tests/test_booking_db.py             (ONE existing test amended:
  test_full_booking_conversation's stale patient-SMS expectation)
- docs/INTEGRATION.md                           (new "Notification policy
  (current MVP)" section; "Publish slots" renumbered 5 -> 6)
- CHANGE_REPORT.md                              (this section)

## Files inspected and deliberately NOT changed
- app/services/booking_conversation.py — inspected the verified Patch 2C
  version for any claim that a patient SMS was sent: none exists. "The
  office will contact you to confirm" is a call-back statement; every
  confirmation is delivered inside the widget with no channel claim. Zero
  changes.
- chat.py, widget files, consent language, app/calendar_models.py (the
  patient_sms_sent column REMAINS — always False now), models.py,
  migrations 001/002/003, booking_service.py, appointment_hold_service.py,
  availability_*, repositories, routes, admin auth, tenant authorization.

## Functions changed (all in app/services/notification_service.py)
- send_booking_notifications — the unconditional patient-SMS try/except
  block (formerly the third send, using appointment.patient_phone) is
  DELETED, replaced by a teaching comment stating the policy. Office SMS
  and office email blocks are byte-identical to Patch 2C. The docstring is
  corrected ("all three booking messages" / "up to 2 Twilio SMS" were now
  false). Signature unchanged — the single production caller
  (booking_conversation._finalize_and_reply) needs no change.
- build_patient_sms — RETAINED as clearly documented FUTURE-ONLY code with
  no reachable production call site (the smallest safe choice; deleting
  correct future wording gains nothing). Its docstring now states it must
  not be re-wired without the separately approved consent-enabled feature.
- NotificationOutcome — unchanged fields; patient_sms_sent documented as
  always False under current policy.
- Module header — owner description corrected to office channels; Patch 2D
  policy note added. _send_sms, _send_email, build_office_sms,
  build_office_email_body, _format_local, _record_outcome: byte-identical.

## Database changes
None. No migration. The patient_sms_sent column and admin view field are
unchanged and now always read False (an honest "disabled", not a failure).

## Behavior removed
The ONLY production patient-message path in the entire calendar codebase:
after a successful booking, an automated SMS to appointment.patient_phone
("request received" wording for PENDING, "confirmed" wording for
CONFIRMED). This fired on 100% of successful bookings because intake
guarantees a phone number. It no longer exists: no code path in
send_booking_notifications can call _send_sms with the patient's number,
regardless of appointment status, patient phone/email presence, office
contact presence or absence, office-channel success or failure, or
require_staff_confirmation.

## Behavior preserved (office notifications, exactly as Patch 2C)
- Office SMS to client.notification_phone; office email to
  client.notification_email; identical formatting; missing contacts still
  recorded as skipped channels; per-channel failures still isolated,
  recorded in outcome.errors and appointment.notify_error; booking success
  still never affected; outcome persistence via _record_outcome unchanged.
- Honest patient outcome: NotificationOutcome.patient_sms_sent and
  appointment.patient_sms_sent remain False; the intentional disablement
  adds NOTHING to notify_error (disabled is not a delivery failure).

## Risks
1. test_full_booking_conversation could not pass unchanged — its assertion
   `"patient_sms" in appointment.notify_error` encoded the removed
   behavior. Amended to assert the office channels' recorded outcomes and
   the ABSENCE of any patient_sms entry. No other assertion in that test
   was touched; no other existing test changed.
2. Admin-view semantics: patient_sms_sent=False now means "policy-disabled"
   rather than "attempted and failed" — documented in INTEGRATION.md §5.
3. Zero risk to booking: notifications run strictly after the booking
   commit; removing a post-commit send cannot affect finalize, holds, or
   Patch 2C revalidation.
4. Any external caller that relied on the patient text being sent would be
   affected — none exists in the repo; the widget already told patients
   "Mia and Dos Tiris do not send SMS messages to patients", so the code
   now matches the widget's existing wording.

## Tests added (9 new test functions; collection 68 -> 77)
calendar_tests/test_notification_policy.py — external providers replaced by
test-side recording fakes at the _send_sms/_send_email boundary (the real
office-channel code executes; no Twilio, Telnyx, Resend, email, or real SMS
provider can run):
- test_only_office_sms_attempted_when_both_phones_exist — exactly one SMS
  attempt, destination is the office phone, patient phone never used, flags
  honest; PLUS the approved formatter-unreachability condition:
  build_patient_sms is monkeypatched with a trap that raises AssertionError
  if invoked — the flow completes with the trap count at 0 and no swallowed
  AssertionError in outcome.errors.
- test_office_email_attempted_and_recorded — office email attempted and
  reflected in office_email_sent; a present patient_email is never a
  destination (no patient email behavior introduced).
- test_missing_office_phone_zero_sms_attempts — zero SMS attempts; patient
  phone never used as a fallback office destination; skip recorded as
  before.
- test_office_sms_failure_still_no_patient_sms — office failure recorded
  honestly; patient SMS still not attempted; office email proceeds
  independently.
- test_office_email_failure_sms_independent — email failure recorded
  honestly; office SMS succeeds independently; no patient attempt.
- test_pending_appointment_no_patient_sms — PENDING: no patient SMS.
- test_confirmed_appointment_no_patient_sms — CONFIRMED: no patient SMS.
- test_persisted_flags_honest_no_fake_patient_error — patient_sms_sent
  False; office flags accurate; notify_error is None when both office
  channels succeed (no fake patient-SMS failure from the intentional
  disablement).
- test_repeated_invocation_patient_sms_stays_disabled — repeated
  send_booking_notifications invocations never touch the patient phone.
  Deliberately does NOT assert office-channel idempotency (separate
  Recommended finding; out of scope).

## Tests to perform
    pytest calendar_tests/test_appointment_intent.py \
           calendar_tests/test_availability_rules.py -v
    ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes \
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/mia_calendar_test \
    pytest calendar_tests/ -v
Expected: 77 collected, 77 passed, 0 failed, 0 skipped, 0 errors
(PostgreSQL required; no database test may be skipped). 67 of the existing
68 tests run byte-unchanged; test_full_booking_conversation runs with only
its stale patient-SMS expectation amended.

## Rollback method
1. Revert app/services/notification_service.py,
   calendar_tests/test_booking_db.py, docs/INTEGRATION.md, and
   CHANGE_REPORT.md to their verified Patch 2C checkpoint versions.
2. Delete calendar_tests/test_notification_policy.py.
3. No migration to reverse; no data touched; no configuration change.
NOTE: rollback RESTORES the unapproved patient-SMS sending. Roll back only
to the complete 2C checkpoint, never partially, and do not deploy the
rolled-back state to real patient traffic.

## Verification status (honest, per Rule 19)
- Executed in the patch-authoring environment on 2026-07-12:
  (a) python3 -m py_compile on all three changed/new .py files — pass;
  (b) a stub-module smoke harness driving the EDITED
      send_booking_notifications end-to-end through all 9 scenarios of the
      test matrix (recording fakes, fake session, real function body,
      including the build_patient_sms trap and the repeated-invocation
      case) — 38 assertions passed, 0 failed;
  (c) diff review confirming the office-channel blocks and _record_outcome
      are byte-identical to the attached Patch 2C checkpoint.
- NOT executed in the patch-authoring environment (no PostgreSQL/full repo
  there): the real 77-test pytest suite. It has since been executed locally
  by the project owner — see below.
- VERIFIED LOCALLY by the project owner on 2026-07-12:
    Environment: fresh local Python virtual environment, disposable
    PostgreSQL 16 Docker container, no production Supabase credentials,
    no production patient data, no external SMS or email provider executed.
    The Docker test container was removed after verification.
    Final results: 77 tests collected; 77 passed; 0 failed; 0 skipped;
    0 errors. No PostgreSQL test was skipped.
- Verified behavior (test-proven on PostgreSQL):
    - Office SMS remains enabled; office email remains enabled.
    - Patient SMS is never attempted — technically disabled under the
      current product policy.
    - The patient phone is never used as an office-notification fallback.
    - build_patient_sms remains FUTURE-ONLY and unreachable from the
      current production booking-notification flow (trap-proven).
    - patient_sms_sent remains False; the intentional patient-SMS
      disablement creates NO fake notification error in notify_error.
    - Office SMS and office email failures remain independently recorded.
    - All Patch 1, Patch 2A, Patch 2B, and Patch 2C regression tests passed.
- No external notification provider (Twilio, Telnyx, Resend, email, or any
  real SMS service) ran during testing — providers were replaced by
  test-side recording fakes only.
- No database migration was required; migrations 001/002/003 are untouched.
- No patient consent feature and no consent language was added — a future
  patient-SMS feature remains a separately approved, consent-enabled build.
- Status: Patch 2D is VERIFIED LOCALLY. The patient-SMS send is removed
  from the only production notification path, office notifications are
  preserved exactly, and the persisted outcome is honest — Senior Audit
  Critical #3 is closed at the code level under the current product policy
  (a technical-behavior statement; no legal or regulatory compliance is
  claimed). "Verified locally" is the precise claim: the manual
  staging/widget regression checklist earlier in this report remains a
  pre-production requirement.
- CHECKPOINT (Rule 18): Patch 2D closed 2026-07-12 with owner approval.
  Rollback point: the verified Patch 2C checkpoint file versions (no
  migration involved). WARNING: rolling back RESTORES the unapproved
  patient-SMS behavior — any rollback must use the complete Patch 2C
  checkpoint, must never be partial, and the rolled-back state must not be
  deployed to real patient traffic.
- Patch 3 and all other audit findings: NOT started — awaiting explicit
  approval and scope.


================================================================================
PATCH 3 — MIA INTEGRATION / BOOKING PRECEDENCE (Senior Audit Critical #5)
================================================================================

GOAL
- Wire the verified Calendar into Mia's live conversation flow with ONE
  booking-ownership contract per message (external calendar > internal
  calendar > lead-capture-only), resolved fresh from current settings.
- Start the internal booking dialog at the exact moment a non-emergency
  lead completes; continue active dialogs before the information guards
  can swallow booking answers; never let emergencies book.
- Keep every office without booking flags byte-identical to Patch 2D.

FILES CHANGED
1. app/routes/chat.py                       — integration (15 edits, below)
2. app/services/booking_conversation.py     — 2 new public functions + 1
                                              keyword-only parameter
3. app/main.py                              — Calendar wiring (router mount
                                              + calendar model registration)
4. docs/INTEGRATION.md                      — section 3 rewritten to the
                                              implemented contract; database
                                              rollout-order note added
5. calendar_tests/test_chat_integration.py  — NEW (27 tests)
6. CHANGE_REPORT.md                         — this section

FUNCTIONS CHANGED / ADDED

app/services/booking_conversation.py
- handle_booking_message — gains keyword-only information_interruption
  (default False; every pre-Patch-3 call is byte-identical in behavior).
  True yields handled=False AFTER the enabled/emergency/identity gates,
  leaving every booking_* field untouched so the dialog resumes on the
  next scheduling message.
- begin_booking_after_intake (NEW) — the explicit start-after-intake entry.
  Gates: strict booking_enabled; emergency (clears any stale state and
  refuses); name+phone present; state must be NONE. Delegates to the same
  _handle_start every start uses — the completing patient message is passed
  through unchanged and this module alone decides whether it seeds the
  preferred date.
- cancel_active_booking (NEW) — the Calendar-owned reset chat.py calls on
  emergency, ownership transition, or genuine conversation ending.
  Idempotent, tenant-scoped through client.id. Ordering is deliberate:
  release_hold FIRST (atomic; already-free reports success; foreign holds
  are refused unchanged), then _clear_booking_state + commit. A failure
  between the two can leave a released hold with stale state (harmless —
  the next delegation revalidates) but never cleared state with an
  orphaned hold.

app/routes/chat.py
- Imports: BookingState (state constants only) + the three
  booking_conversation entry points.
- booking_dialog_active (NEW) — chat.py's single reader of booking_state.
- is_information_interruption (NEW) — composes ONLY existing detectors
  (general-hours, office-phone, insurance, pricing, question-permission,
  specific-hours-day, info-intent). None of them match "tomorrow",
  "morning", "2", "yes", or "no" (tested).
- send_external_booking_handoff (NEW) — THE single external-handoff owner,
  extracted from the former inline block; reuses
  should_capture_before_booking_link / next_booking_capture_prompt /
  build_booking_handoff_reply / build_booking_handoff_meta unchanged.
  Link-not-yet-sent behavior is byte-identical (including the
  [BOOKING_CAPTURE] diagnostic print and the exactly-once
  booking_link_sent transition). NEW post-link branch: truthful
  acknowledgment "The online booking link is still available below."
  (meta mode external_booking_link_reminder, button/meta preserved).
- route_completed_lead (NEW) — THE single completion-routing owner
  implementing the ownership contract; returns None for
  lead-capture-only offices (caller keeps today's reply). Honest
  delegation-failure fallback: logs, rolls back, consults the
  per-channel-idempotent finalize_and_notify_if_ready; claims office
  follow-up ONLY if a channel actually recorded success, otherwise
  directs to the office phone.
- _routed_completion_response (NEW) — shared persistence plumbing for a
  routed completion (one commit: conversation + assistant Message).
- chat() endpoint — 12 in-flow edits:
  E3  conversation-ending guard: narrowest carve-out — at
      WAITING_FOR_CONFIRMATION, normalized "no"/"no thanks"/"no thank you"
      bypass the guard and reach the Calendar rejection path; every
      genuine ending during a dialog calls cancel_active_booking (wrapped:
      log + rollback on failure) and keeps the existing ending reply.
  E4  time-only outside-hours guard gated off while a dialog is active.
  E5  intake time-window capture guard gated off while a dialog is active
      (booking answers never overwrite lead_time_window).
  E6  dangerous-dental guard: same-request cleanup when is_true_emergency.
  E7  urgent-trauma guard: same-request cleanup.
  E8  emergency-routing guard: same-request cleanup.
      (E6-E8 all: cleanup failure is logged + rolled back; the emergency
      wording is unchanged and always returned — never a 500.)
  E9  CALENDAR BOOKING CONTINUATION hook before the Operational override:
      active dialog + external URL -> cancel + external handoff in the
      SAME request; else handle_booking_message with the interruption
      flag; handled=False falls through unchanged; delegation failure
      uses the honest fallback above.
  E10 external booking block body replaced by the shared owner; the
      former "not booking_link_sent" trigger clause moved INTO the owner
      (post-link scheduling now gets the reminder instead of silently
      falling through).
  E11-E15 the five completion call sites invoke route_completed_lead
      immediately AFTER their existing mark_completed_and_notify_office
      call runs unchanged: short-symptom, patient-type, priority
      time-window, lead_capture_complete, priority receptionist-bypass.

app/main.py
- from app.routes import calendar as calendar_routes;
  import app.calendar_models  (# noqa) — registers calendar tables for
  Base.metadata.create_all; app.include_router(calendar_routes.router).

DATABASE CHANGES
- None. No migration. ROLLOUT ORDER REQUIREMENT documented in
  INTEGRATION.md: migrations 001, 002, 003 must be applied before the
  integrated code deploys; 003 must precede any code that writes the
  offer-expiration columns.

BEHAVIOR ADDED
- Completed non-emergency leads at internal-calendar offices flow into the
  booking dialog instead of the manual-callback ending (approved temporary
  MVP: the completed-lead office notification still runs FIRST; a later
  successful booking sends the separate booking notification; dedup is out
  of scope under Recommended #1).
- External-calendar offices own booking for the whole conversation,
  including a truthful post-link reminder and a same-request internal->
  external transition (hold released, state cleared) if the URL appears
  mid-dialog.
- Mid-dialog information questions are answered by existing paths with the
  dialog state left byte-unchanged; booking answers reach the state
  machine instead of the intake guards.
- "no"/"no thanks"/"no thank you" at the confirmation step are slot
  rejections; genuine endings cancel the dialog and release the hold.
- Emergencies mid-dialog clean up the dialog in the same request; the
  emergency reply is always the patient-facing response.

BEHAVIOR INTENTIONALLY UNCHANGED
- Offices with no booking URL and no calendar.booking_enabled=true:
  byte-identical replies and metas at every completion branch (tested).
- Pre-link external behavior: capture-first prompts and the first link
  handoff (wording, meta, [BOOKING_CAPTURE] print, exactly-once flag).
- All emergency/safety wording; guard ordering (safety still evaluates
  before the continuation hook); intake wording; office notification
  content and per-channel idempotency; abuse guards; FAQ behavior.
- booking_conversation state machine semantics (Patches 1-2C) — the two
  new functions reuse the existing internals; no state handler changed.

RISKS
- chat.py flow coupling: is_information_interruption composes existing
  detectors; a future information guard placed below the continuation hook
  must be added to that list (documented at the definition and in
  INTEGRATION.md).
- Approved temporary double-notification (lead + booking) until the
  Recommended #1 outbox work.
- The continuation hook intercepts every message while a dialog is active
  (after safety guards); any future pre-hook guard that should interrupt
  booking must either clear state via cancel_active_booking or be added to
  the interruption detector list.
- Latent pre-existing receptionist_bypass_reply bare-string issue (noted
  in Patch 2 planning) remains out of scope and untouched.

TESTS TO PERFORM (local — disposable PostgreSQL 16 in Docker)
    docker run --name mia-calendar-test-db -d -p 5433:5432 \
      -e POSTGRES_PASSWORD=test \
      -e POSTGRES_DB=mia_calendar_test \
      postgres:16
    ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes \
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/mia_calendar_test \
    pytest calendar_tests/ -v
  (Correction pass: the database name must contain "test" — Patch 1's
  destructive-test safety gate rejects .../postgres.)
- Expected: 110 collected / 110 passed (77 existing + 33 new in
  calendar_tests/test_chat_integration.py).
- The new file needs the packages Mia already uses (fastapi, openai,
  twilio, resend installed in the venv); every network boundary is
  monkeypatched to recording fakes — no OpenAI/Twilio/Resend call occurs.

ROLLBACK METHOD
- Revert app/routes/chat.py, app/services/booking_conversation.py,
  app/main.py, and docs/INTEGRATION.md to their Patch 2D checkpoint
  versions; delete calendar_tests/test_chat_integration.py; revert this
  report section. No migration to reverse.
- Operational rollback without code changes: setting an office's
  calendar.booking_enabled to false (or removing it) instantly restores
  lead-capture-only behavior for that office; removing booking_url
  restores it for external offices.

VERIFICATION STATUS (Rule 19 — honest claim)
- NOT YET VERIFIED LOCALLY. The implementation environment has no network,
  no PostgreSQL, and no installed fastapi/sqlalchemy/pytest, so the full
  suite DID NOT run here. What DID run here:
    - python3 -m py_compile on all four changed Python files and the new
      test file (all pass).
    - A stub-module smoke harness importing the REAL edited chat.py and
      booking_conversation.py (31/31 checks): interruption detector
      positives + the five booking-answer negatives against the real
      detectors; rejection-phrase normalization; booking_dialog_active;
      cancel_active_booking ordering/field-wipe/idempotence; the
      interruption yield leaving state untouched; begin_booking_after_intake
      gates (disabled/identity/emergency); the post-link reminder wording
      and mode; the honest fallback wording, mode, and single
      finalize_and_notify_if_ready consultation.
- The checkpoint stays OPEN until the owner runs the full 110-test
  PostgreSQL suite locally (110 collected = 77 verified Patch 2D tests +
  33 Patch 3 tests; see the correction-pass subsection below) and this
  section is updated with the verified results.


--------------------------------------------------------------------------------
PATCH 3 — CORRECTION PASS (owner review defects; no redesign)
--------------------------------------------------------------------------------

GOAL
- Fix seven flow defects found in the owner's independent review of the
  delivered Patch 3, without redesigning unrelated code.

FILES CHANGED (correction pass)
1. app/routes/chat.py
2. app/services/booking_conversation.py
3. docs/INTEGRATION.md (contract section updated to match)
4. calendar_tests/test_chat_integration.py
5. CHANGE_REPORT.md (this subsection + corrected test command)
app/main.py: UNCHANGED — no correction required it.

CORRECTIONS
1. MEDICAL-ADVICE SAFETY WINS. The continuation hook now yields whenever
   looks_like_medical_advice(user_text) is True: booking state and any held
   slot stay byte-unchanged (not necessarily an emergency), the EXISTING
   medical-advice guard answers (its wording is reused, never duplicated),
   and if an external URL appeared on that same message the ownership
   transition waits for the next appropriate message. The detector never
   matches "yes"/"no"/"2"/"tomorrow"/"morning" (booking words force it
   False; no advice phrasing).
2. EMERGENCY DEFENSE RELEASES HOLDS. Both handle_booking_message's and
   begin_booking_after_intake's emergency gates now use the Calendar-owned
   cancel_active_booking pathway (tenant-scoped hold release FIRST, then
   full field clear) instead of _clear_booking_state alone, so an
   emergency-flagged conversation with a live selected-slot hold never
   orphans it. handled=False is still returned.
3. SINGLE OWNER ON TRANSITION FAILURE. If cancel_active_booking raises
   during an internal -> external transition, chat.py now logs, rolls
   back, does NOT set booking_link_sent, does NOT send any external
   handoff or reminder, replies with the honest office-follow-up fallback
   (persisted per-channel flags; zero duplicate sends), and leaves the
   dialog intact so the next message retries the transition. The fallback
   wording now has ONE owner: _booking_error_reply_text, used by
   route_completed_lead, the continuation-failure path, and the
   transition-failure path.
4. POST-LINK REMINDER ONLY FOR SCHEDULING INTENT. The external trigger is
   split: the FIRST handoff keeps today's trigger byte-unchanged
   (is_scheduling_now OR service_reason_now OR the stored
   active_service_reason); after booking_link_sent=True the reminder fires
   only for actual scheduling or service-selection intent in the CURRENT
   message (is_scheduling_now OR service_reason_now), so the stored
   lead_reason can no longer hijack unrelated messages. The block is also
   gated off while an internal dialog is active — during a dialog,
   ownership routing belongs solely to the continuation hook (one owner).
5. FRESH-OWNERSHIP CONTRACT COMPLETED. One narrow post-completion routing
   point (immediately after the continuation hook) lets a COMPLETED lead
   with no active dialog and no external URL start a NEW internal dialog
   on a scheduling/date message — e.g. after a transition whose URL was
   later removed, or after a genuine ending. It reuses route_completed_lead
   (no scattered routing), booking_link_sent never blocks it, emergencies /
   medical questions / information questions are excluded, and the
   duplicate-appointment defense in the Calendar start still applies. The
   delivered no-resurrection test contradicted this contract and was
   corrected: stale state never resurrects, but a clean NEW dialog starts.
6. LOCATION INTERRUPTION. The Operational override's inline location
   phrase list was extracted into looks_like_location_request (single
   owner), the override now calls it (identical normalization, identical
   behavior), and it joined is_information_interruption — an active dialog
   now yields for "Where are you located?" exactly as it does for hours.
7. TEST COMMAND CORRECTED above: named container, POSTGRES_DB=
   mia_calendar_test, and a TEST_DATABASE_URL whose database name passes
   Patch 1's destructive-test safety gate.

BEHAVIOR INTENTIONALLY UNCHANGED (correction pass)
- First-handoff external trigger and all its replies/meta; every emergency
  and medical wording; the ending-guard and emergency cleanup semantics
  from the approved conditions; all Patch 1-2D state-machine behavior;
  app/main.py.

TESTS ADDED / CHANGED (27 -> 33 new tests; total expected 110)
Added: test_medical_advice_mid_booking_yields_and_resumes,
  test_medical_advice_defers_external_transition,
  test_emergency_gate_releases_active_hold,
  test_transition_cleanup_failure_keeps_single_owner,
  test_post_link_unrelated_message_not_hijacked,
  test_location_interruption_pauses_and_resumes.
Changed: test_next_message_after_ending_cannot_resume_booking ->
  test_stale_state_never_resurrects_after_ending (neutral message; stale
  state assertions strengthened); test_external_url_removed_no_resurrection
  -> test_url_removed_fresh_internal_dialog_starts (corrected to the
  fresh-ownership contract); test_post_link_scheduling_gets_reminder
  (booking button/meta assertion added — strengthened, not weakened).
No existing assertion was weakened; the two corrected tests asserted the
contract-contradicting behavior the owner ordered fixed.

VERIFICATION STATUS (Rule 19 — honest claim)
- STILL NOT VERIFIED LOCALLY (same environment limits as above). Ran here:
  py_compile on chat.py, booking_conversation.py, and the test file (pass),
  and the stub-module smoke harness extended for the corrections (see the
  updated harness): 52/52 checks pass (31 original + 21 correction-pass), including the medical-advice
  detector positives/negatives used by the new hook gate, the location
  detector and its membership in is_information_interruption, the
  emergency gates releasing a fake active hold through cancel_active_booking
  from BOTH entries, and the single-owner fallback wording.
- The checkpoint stays OPEN pending the owner's full 110-test PostgreSQL
  run with the corrected command above.


--------------------------------------------------------------------------------
PATCH 3 — LOCAL RUN 1 RESULT AND FAILURE-CORRECTION PASS (tests only)
--------------------------------------------------------------------------------

LOCAL POSTGRESQL RUN 1 (owner, 2026-07-12): 110 collected, 107 passed,
3 failed, 0 skipped, 0 collection errors. Patch 3 NOT verified.

ROOT CAUSES (each diagnosed by executing the real chat.py functions against
the failing fixtures — no production routing defect found; all three fixes
are test-file-only):

1 & 3. test_internal_short_symptom_completion_starts_booking and
   test_priority_lead_booked_end_to_end_with_priority_urgency
   (same root). Gate-by-gate result for the completing message
   "tomorrow morning" (run day Sunday 2026-07-12):
     - conversation_uses_short_symptom_flow ........ True
     - lead_is_ready_for_office_notification ....... True
     - lead_status != "completed" .................. True
     - canonical time-window value ................. "Mon morning"
     - time_window_is_complete("Mon morning") ...... True
     - route_completed_lead reached ................ NO
   The false gate is NONE of the four completion conditions — it is the
   pre-existing time-window ISSUE branch ABOVE them:
   build_time_window_issue_reply(client, "Mon morning") returned
   "The office is closed on Monday. What day/time works better for you?"
   because the test Client had NO office_hours configured and chat.py's
   untouched Patch 2D logic treats an unconfigured day as CLOSED
   (row.get("open", False)). The time window was therefore never stored,
   the completion condition was never evaluated, and the guard returned
   mode intake_time_window_capture. The fixture failed to represent a
   legitimate production short-symptom lead: real offices have
   Client.office_hours (JSONB) configured.
   FIX (setup only): make_client gained office_hours=None (default unset,
   preserving the tests that assert the no-hours fallback replies); the
   two failing tests now pass a production-realistic all-week-open struct
   (09:00-17:00, all seven days, so "tomorrow" is valid on any run date).
   Verified against the real functions: with the struct,
   build_time_window_issue_reply returns None, the window stores as
   complete, and the approved contract path (notify once -> route ->
   booking starts -> "tomorrow morning" seeds the date ->
   waiting_for_time_preference -> urgency "priority" at finalize) is
   exactly what the unchanged assertions require.

2. test_post_link_unrelated_message_not_hijacked. Over-specific test
   expectation, not a hijack: the reminder correctly did NOT fire and the
   hours path answered. The Patch 2D operational override appends the next
   intake prompt when intake is unfinished and the lead is not completed
   (op_reply + "\n\n" + _next_intake_prompt) — code untouched by Patch 3
   (present in no Patch 3 diff hunk). FIX (test only): the expected reply
   is now the exact existing combined wording (hours fallback + "One quick
   question — Kevin Alvarado, are you a new or returning patient?"), and
   the assertions were strengthened: mode faq_operational_no_match, mode
   NOT external_booking_link_reminder, booking_link_sent still True,
   booking_state none, zero completed-lead notifications sent.

FILES CHANGED (this pass): calendar_tests/test_chat_integration.py and
CHANGE_REPORT.md only. No production file changed; chat.py,
booking_conversation.py, and app/main.py remain byte-identical to the
approved v2 package. Expected total remains 110 collected = 77 verified
Patch 2D tests + 33 Patch 3 tests.

VERIFICATION STATUS (Rule 19): STILL NOT VERIFIED. Ran here: the real-code
gate trace above (stub-module harness executing the actual edited chat.py
functions) and py_compile on the corrected test file. The checkpoint stays
OPEN pending the owner's full 110-test PostgreSQL rerun.


--------------------------------------------------------------------------------
PATCH 3 — LOCAL VERIFICATION COMPLETE; CHECKPOINT CLOSED
--------------------------------------------------------------------------------

FINAL LOCAL POSTGRESQL RUN (owner, 2026-07-12)
- Environment: Windows, Python 3.14.2, pytest 9.1.1, fresh Patch 3 virtual
  environment, disposable PostgreSQL 16 Docker container. No production
  Supabase credentials, no production patient data, no real SMS or email
  provider executed. The Docker container was removed after verification.
- Result: 110 collected, 110 passed, 0 failed, 0 skipped, 0 errors, in
  6.44 seconds. No PostgreSQL test was skipped.
- 110 collected = 77 verified Patch 2D tests + 33 Patch 3 tests.

RUN HISTORY (complete and honest)
- Run 1: 110 collected, 107 passed, 3 failed, 0 skipped. All three failures
  were test-fixture/expectation defects, not production-code defects (full
  gate-by-gate diagnosis in the LOCAL RUN 1 section above):
    1. The two short-symptom/priority tests provided no office_hours
       fixture, so the pre-existing intake logic treated the resolved day
       as closed and the time window was never stored. The fixtures were
       corrected to provide valid office hours.
    2. The post-link unrelated-message test expected only the office-hours
       sentence, but the verified pre-Patch-3 answer-first behavior also
       appends the next unfinished intake question. The expectation was
       corrected to the existing combined reply.
- Correction pass between the runs changed ONLY
  calendar_tests/test_chat_integration.py and CHANGE_REPORT.md. No
  production Python file changed during the correction pass, and no
  existing production assertion was weakened.
- Run 2 (final): 110/110 as recorded above.

VERIFIED PATCH 3 BEHAVIOR (each item covered by the passing suite)
- Exactly one booking owner per conversation, resolved per message.
- An active external booking URL takes precedence over the internal
  Calendar.
- booking_link_sent does not transfer ownership to the internal Calendar.
- External post-link reminders are shown only for scheduling/service-
  selection intent in the current message.
- The internal Calendar starts after eligible completed intake, at all
  five completion call sites.
- Non-emergency priority leads can book; the final appointment urgency
  remains "priority".
- Emergency flows win and clear Calendar state during the SAME request.
- Active holds are released during Calendar cancellation (emergency,
  ownership transition, and genuine conversation endings), tenant-scoped
  and idempotent.
- A cleanup failure cannot suppress the emergency reply (never a 500,
  never a false success claim).
- Internal-to-external transitions never create two booking owners; a
  failed transition is answered honestly and retried on the next message.
- Ownership resolves fresh after settings changes: URL added mid-dialog
  transitions in the same request; URL removed later allows a clean NEW
  internal dialog without resurrecting stale state.
- Office-information and location questions interrupt the Calendar safely
  (state byte-unchanged; the next scheduling answer resumes).
- Medical-advice safety responses win over the Calendar; state and any
  held slot remain unchanged.
- Genuine conversation endings cancel active Calendar state and release
  holds while preserving Mia's existing ending reply.
- "no" (and "no thanks" / "no thank you") at booking confirmation reaches
  the Calendar state machine's rejection/change path.
- app/main.py mounts the Calendar router and registers the Calendar models.
- The office completed-lead notification runs BEFORE Calendar booking
  (approved temporary MVP behavior), and a later completed booking
  produces the separate booking notification.
- Patient SMS remains disabled (Patch 2D policy unchanged).
- All Patch 1, 2A, 2B, 2C, and 2D regression tests passed (the full 77-test
  baseline ran in the same suite).

DEPLOYMENT NOTES (unchanged requirements, restated at closure)
- Patch 3 required NO new database migration.
- Existing migrations 001, 002, and 003 MUST be applied before the
  integrated Calendar code is deployed to production.
- Migration 003 must be applied before code that writes the
  offer-expiration fields runs.
- The manual staging/widget regression checklist earlier in this report
  remains a pre-production requirement.

STATUS
- Patch 3 is VERIFIED LOCALLY. Senior Audit Critical #5 (Mia integration /
  booking precedence) is closed at the code level: the verified behavior
  is exactly the approved ownership contract with its correction-pass
  conditions.
- CHECKPOINT (Rule 18): Patch 3 closed 2026-07-12 with owner approval.
  Rollback point: the verified Patch 2D checkpoint file versions (revert
  app/routes/chat.py, app/services/booking_conversation.py, app/main.py,
  docs/INTEGRATION.md; delete calendar_tests/test_chat_integration.py;
  revert this report). No migration to reverse. Operational rollback
  without code changes: calendar.booking_enabled=false (or removing
  booking_url) per office restores lead-capture-only behavior for that
  office; the code-level external-handoff extraction rolls back only with
  the file reverts.
- Patch 4 and all other audit findings: NOT started — awaiting explicit
  approval and scope.


================================================================================
# PATCH 4 — STAFF CONFIRMATION TRANSITION (Senior Audit Critical #4)

Per Constitution Rule 13. No vague claims.

## Goal
Senior Audit Critical #4: "Appointments default to 'pending,' but the office
cannot confirm them." With require_staff_confirmation enabled (the safe
default), Mia saves appointments as PENDING, the office SMS says "NEEDS
CONFIRMATION" — and no endpoint or service function could ever perform
pending -> confirmed. Appointments could stay pending forever, or be
"confirmed" only by unlocked, out-of-band Supabase row edits (a Rule 15
violation). Patch 4 adds the single supported transition: a tenant-scoped,
row-locked, idempotent staff-confirmation service and admin endpoint, plus a
confirmed_at audit column (migration 004). Nothing else changes.

## Approved product contract (implemented exactly)
- PENDING -> CONFIRMED is the ONLY transition this feature performs.
- Re-confirming an already-confirmed appointment is an idempotent SUCCESS;
  the original confirmed_at is preserved byte-for-byte.
- CANCELLED, COMPLETED, and NO_SHOW are not confirmable (409); rejection
  never mutates anything, including a previously recorded confirmed_at.
- Unknown ids and another office's ids return the identical 404 wording
  ("Appointment not found.") — indistinguishable, tenant-isolated.
- Confirmation uses the EXISTING appointment_repository
  .get_appointment_for_update (SELECT ... FOR UPDATE, client_id filtered
  inside the locked query). No new repository function.
- NO notification of any kind: no additional office SMS/email (authorized
  office staff are performing the action) and no patient message (Patch 2D
  policy — patient SMS remains disabled). Notification flags and
  notify_error are untouched by every confirm path.
- No Mia conversation, state-machine, or widget behavior changes.
- confirmed_at semantics (approved): records the UTC instant of the FIRST
  successful STAFF pending -> confirmed action ONLY. Appointments created
  directly as CONFIRMED (require_staff_confirmation=false) keep
  confirmed_at=NULL on purpose — finalize_booking is byte-unchanged and
  never sets it. Documented in the ORM comment, migration comments, the SQL
  COMMENT ON COLUMN, docs/INTEGRATION.md §7, and here.

## Files changed
- migrations/004_staff_confirmation_up.sql     (NEW — adds nullable
  confirmed_at timestamptz to appointments; BEGIN/COMMIT script; NO
  "IF NOT EXISTS" so a re-apply fails loudly, per the 002/003 convention)
- migrations/004_staff_confirmation_down.sql   (NEW — DROP COLUMN IF EXISTS
  confirmed_at; the IF EXISTS is the APPROVED safe-rollback semantics: a
  repeat down run is a harmless no-op; touches nothing from 001/002/003)
- app/calendar_models.py                       (confirmed_at column with the
  approved-semantics teaching comment; mirrors migration 004 exactly)
- app/services/booking_service.py              (new confirm_appointment; the
  module OWNER header now includes confirming; the BookingResult reason
  vocabulary comment corrected — it claimed to be "the complete failure
  vocabulary" but omitted the existing already_cancelled, so the correction
  adds already_cancelled alongside the three new Patch 4 reasons:
  appointment_missing / already_confirmed / not_confirmable)
- app/routes/calendar.py                       (new POST
  /admin/calendar/appointments/{id}/confirm; AppointmentView and
  _appointment_view expose nullable confirmed_at, UTC-normalized via
  ensure_utc; header endpoint list updated)
- calendar_tests/test_booking_db.py            (13 new tests + coverage-map
  header lines; NO existing test or assertion changed)
- calendar_tests/test_migration_schema.py      (4 new self-contained 004
  tests following the 003 standard; NO existing test changed)
- docs/INTEGRATION.md                          (rollout order now
  001 -> 002 -> 003 -> 004 BEFORE Patch 4 code, with the reason; new §7
  documenting the endpoint, response mapping, no-notification policy, and
  confirmed_at semantics)
- CHANGE_REPORT.md                             (this section)

## Files inspected and deliberately NOT changed
- app/repositories/appointment_repository.py — get_appointment_for_update
  already provides exactly the tenant-scoped row lock required; zero changes.
- app/services/notification_service.py — no confirmation notification per
  the approved policy; the booking-time "NEEDS CONFIRMATION" office-SMS
  wording remains accurate and is now completed by a real transition.
- calendar_tests/conftest.py — Base.metadata.create_all picks up the new ORM
  column automatically; safeguards and fixtures unchanged.
- app/services/booking_conversation.py, chat.py, widget files, consent
  language — the patient-facing "the office will contact you to confirm"
  wording makes no channel or timing claim and remains true; no conversation
  or widget behavior changes.
- app/services/appointment_hold_service.py, availability_rules.py,
  availability_service.py, calendar_settings_service.py, app/models.py,
  app/main.py, migrations 001/002/003, tenant authentication,
  calendar_tests/test_appointment_intent.py, test_availability_rules.py,
  test_notification_policy.py, test_chat_integration.py.

## Functions changed
- booking_service.confirm_appointment — NEW. Signature: (db, client_id,
  appointment_id, *, now_utc) — keyword-only aware-UTC now per the Patch 2C
  convention. APPROVED CONDITION 1: the injected now_utc is normalized
  through the existing ensure_utc helper before storing; the function never
  reads the real clock (no datetime.now anywhere in the service), so tests
  are deterministic. Outcomes: ok (PENDING -> CONFIRMED + confirmed_at, ONE
  commit) / already_confirmed (success=True, NOTHING written, rollback
  releases the lock) / appointment_missing (unknown or cross-tenant) /
  not_confirmable (detail carries the current status; nothing mutated).
  Unexpected exceptions roll back and re-raise (Rule 16). The
  pending -> confirmed UPDATE cannot violate the migration-002 partial
  unique indexes (indexed columns unchanged; the row stays inside the
  status <> 'cancelled' predicates), so no IntegrityError classification
  exists here by design.
- routes/calendar.confirm_appointment — NEW route. 200 fresh confirmation,
  200 idempotent re-confirm, 404 "Appointment not found." (identical wording
  for unknown and cross-tenant), 409 for cancelled/completed/no_show,
  unexpected database exceptions roll back inside the service and propagate.
  Injects now_utc=datetime.now(UTC) at the transport boundary only.
- routes/calendar._appointment_view — adds confirmed_at (ensure_utc when
  non-null, else None). finalize_booking, cancel_appointment, and every
  other existing function: byte-unchanged.

## Database changes
- Migration 004 (additive only): appointments.confirmed_at TIMESTAMPTZ NULL
  + COMMENT ON COLUMN stating the approved semantics. No row data touched;
  existing rows read back NULL. No CHECK change ('confirmed' was already a
  legal status in 001). No index change.
- ROLLOUT ORDER (documented in INTEGRATION.md): apply 001 -> 002 -> 003 ->
  004, all BEFORE deploying Patch 4 code — the ORM model, confirm service,
  and admin appointment views reference confirmed_at, so code deployed
  before 004 fails on the first appointment query. None of 001-003 is in
  production yet, so production applies one ordered sequence.

## Behavior added
Staff can POST /admin/calendar/appointments/{id}/confirm (X-Admin-Key +
client_id, the existing route conventions) and the appointment transitions
pending -> confirmed under a row lock, recording confirmed_at once. Repeat
confirmations succeed idempotently with zero duplicate effects. The confirm
response and the appointment list expose nullable confirmed_at.

## Behavior intentionally unchanged
- finalize_booking still assigns PENDING/CONFIRMED from
  require_staff_confirmation exactly as before and NEVER sets confirmed_at.
- cancel_appointment is byte-unchanged: confirmed -> cancelled remains
  legal, frees the slot, and now demonstrably preserves confirmed_at on the
  cancelled row (test-proven); completed/no_show remaining cancellable is
  the KNOWN open Recommended #6 finding, deliberately untouched.
- All notification behavior (office SMS/email at booking time only; patient
  SMS disabled), holds, availability, offer expiry, Mia integration,
  emergency behavior, external/Zocdoc precedence, tenant isolation, widget
  behavior, one-question-per-message: untouched.
- The shared global ADMIN_API_KEY still authorizes any client_id — Senior
  Audit Critical #2, which is Patch 5's scope, is NOT addressed or worsened
  by this patch.

## Risks
1. Rollback of migration 004 discards recorded confirmed_at values (the
   point of a rollback); appointment statuses already flipped to confirmed
   stay confirmed — a valid pre-Patch-4 value; rewriting them would falsify
   staff actions. Documented in the down script.
2. confirmed_at=NULL is intentionally ambiguous-looking on auto-confirmed
   appointments ("confirmed but no timestamp"). This is the APPROVED
   semantics — the column records staff actions only — and is documented in
   four places to prevent misreading.
3. The vocabulary-comment correction in BookingResult adds the previously
   omitted already_cancelled to a comment claiming completeness; a
   documentation-only line, no behavior.
4. The confirm route, like every current admin route, is protected by the
   shared global admin key (Critical #2 — Patch 5). It exposes no patient
   data beyond what the existing appointment list already returns.

## Tests added (17 new; collection 110 -> 127; NO existing test changed)
calendar_tests/test_booking_db.py (31 -> 44):
- test_confirm_pending_appointment_succeeds — production-path PENDING
  appointment; ok; confirmed_at equals EXACTLY the injected fixed instant;
  slot stays BOOKED.
- test_confirm_repeat_is_idempotent_preserves_confirmed_at — second confirm
  with a DIFFERENT injected instant: success, already_confirmed,
  confirmed_at byte-for-byte unchanged.
- test_confirm_unknown_appointment_missing — appointment_missing; a
  bystander appointment + slot provably untouched; provider traps at 0.
- test_confirm_other_client_appointment_missing — office B confirming
  office A's real id: outcome tuple IDENTICAL to a nonexistent id; office
  A's row untouched; traps at 0.
- test_confirm_cancelled_appointment_rejected — production cancel first;
  not_confirmable detail 'cancelled'; appointment/slot/notification fields
  all byte-unchanged; traps at 0.
- test_confirm_completed_appointment_rejected — same mutation-free proof for
  'completed' (status set directly on purpose: no production transition
  writes it yet; only the confirm gate is under test).
- test_confirm_no_show_appointment_rejected — same for 'no_show'.
- test_confirm_sends_no_notifications — the SUCCESS path: _send_sms and
  _send_email trapped (0 invocations); the four notification bookkeeping
  fields byte-identical before/after.
- test_confirm_auto_confirmed_appointment_keeps_null_confirmed_at —
  require_staff_confirmation=false; finalize creates CONFIRMED with
  confirmed_at NULL; staff confirm returns already_confirmed and KEEPS NULL
  (approved staff-only semantics, and proof finalize_booking is unchanged).
- test_cancel_then_confirm_rejected — confirm(T1) -> cancel -> confirm
  again: rejected, final status cancelled, and the earlier confirmed_at=T1
  SURVIVES (rejection wipes nothing).
- test_confirm_then_cancel_allowed_preserves_confirmed_at — the existing
  confirmed -> cancelled transition still works, frees the slot, and
  preserves confirmed_at for the audit trail.
- test_concurrent_confirm_same_appointment_single_transition — genuinely
  CONCURRENT (two threads, two sessions, barrier) with DIFFERENT injected
  instants so an overwrite would be visible. APPROVED CONDITION 6 proven:
  both calls succeed; exactly one reason 'ok' and exactly one
  'already_confirmed'; exactly ONE timestamp written (the winner's injected
  instant); the loser observes exactly that same value; the slot remains
  BOOKED.
- test_confirm_route_status_mapping — the route invoked directly with the
  session (the established DST-test pattern): 200 fresh + 200 idempotent
  with identical confirmed_at; 404 for unknown AND cross-tenant with
  asserted-IDENTICAL wording; 409 for cancelled; and BOTH views expose
  nullable confirmed_at consistently (the cancelled row keeps its value in
  the list; a fresh pending row shows null).

calendar_tests/test_migration_schema.py (8 -> 12; each self-contained per
the 003 standard — applies 004 itself from the 001+002 baseline and removes
it in cleanup):
- test_004_adds_confirmed_at_nullable_timestamptz — exactly one column
  added; timestamp with time zone; nullable; down restores the exact
  baseline.
- test_reapplying_004_fails_loudly — the UP has no IF NOT EXISTS; second
  apply raises; ROLLBACK; connection proven usable; cleanup.
- test_004_down_removes_column_and_preserves_001_002_003 — applies 003 to
  prove it survives; after 004 up+down: appointments and slots column sets
  equal the snapshots, 003's conversation columns intact, both Patch 1
  unique indexes keep UNIQUE + 'cancelled' predicates; a SECOND down run is
  a no-op (the approved IF EXISTS semantics, proven).
- test_004_up_reapplies_after_down — up -> down -> up round-trips with the
  correct type.

## Tests to perform
    pytest calendar_tests/test_appointment_intent.py \
           calendar_tests/test_availability_rules.py -v
    ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes \
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/mia_calendar_test \
    pytest calendar_tests/ -v
Expected: 127 collected, 127 passed, 0 failed, 0 skipped, 0 errors
(PostgreSQL required; no database test may be skipped).
127 = 110 verified Patch 3 tests (all byte-unchanged) + 13 new
test_booking_db tests + 4 new test_migration_schema tests.

## Rollback method
1. Revert app/services/booking_service.py, app/routes/calendar.py,
   app/calendar_models.py, calendar_tests/test_booking_db.py,
   calendar_tests/test_migration_schema.py, docs/INTEGRATION.md, and
   CHANGE_REPORT.md to their verified Patch 3 checkpoint versions.
2. Delete migrations/004_staff_confirmation_up.sql and
   migrations/004_staff_confirmation_down.sql.
3. If 004 was applied to any database: revert (or stop) the Patch 4 code
   FIRST, then run migrations/004_staff_confirmation_down.sql. It drops
   exactly the confirmed_at column (recorded staff timestamps are discarded
   — the rollback's intent); statuses already 'confirmed' remain 'confirmed'
   (valid pre-Patch-4 value). The down script is safely re-runnable
   (IF EXISTS).
4. Operational note: no settings flag exists or is needed — the endpoint is
   staff-invoked only; not calling it reproduces pre-Patch-4 behavior
   exactly.

## Verification status (honest, per Rule 19)
- Executed in the patch-authoring environment on 2026-07-12:
  (a) python3 -m py_compile on all five changed/new .py files — 5/5 pass;
  (b) static condition checks against the approved final conditions —
      confirmed: the service stores ensure_utc(now_utc) and contains NO
      datetime.now call and NO notification import; the route injects the
      clock at the transport boundary only; the 404 wording is character-
      identical between confirm and cancel; AppointmentView and
      _appointment_view both carry confirmed_at; the ORM column is nullable
      timestamptz; the 004 UP script has BEGIN/COMMIT and no IF NOT EXISTS
      guard on the ALTER; the DOWN uses DROP COLUMN IF EXISTS; exactly 13
      new test functions in test_booking_db.py (31 -> 44) and exactly 4 in
      test_migration_schema.py (8 -> 12);
  (c) diff review confirming finalize_booking, cancel_appointment, all
      repository functions, and every pre-existing test are byte-unchanged.
- NOT executed in the patch-authoring environment (no PostgreSQL, no
  SQLAlchemy/psycopg2/FastAPI installed there): the real 127-test pytest
  suite. Patch 4 is therefore NOT VERIFIED. No test-result claim is made.
- CHECKPOINT (Rule 18): OPEN — awaiting the owner's full local PostgreSQL
  run (fresh venv + disposable PostgreSQL 16 container, per the commands
  above) and owner approval. Rollback point: the verified Patch 3
  checkpoint file versions plus deletion of the two 004 migration files
  (down script available if 004 was applied).
- Patch 5 (Senior Audit Critical #2 — tenant authorization) and all other
  audit findings: NOT started — awaiting explicit approval and scope.


--------------------------------------------------------------------------------
PATCH 4 — LOCAL VERIFICATION COMPLETE; CHECKPOINT CLOSED
--------------------------------------------------------------------------------

FINAL LOCAL POSTGRESQL RUN (owner, 2026-07-12)
- Environment: Windows, Python 3.14.2, pytest 9.1.1, fresh Patch 4 virtual
  environment, disposable PostgreSQL 16 Docker container. No production
  Supabase credentials, no production patient data, no real SMS or email
  provider executed. The Docker container was removed after verification.
- Result: 127 collected, 127 passed, 0 failed, 0 skipped, 0 errors, in
  8.08 seconds. No PostgreSQL test was skipped.
- 127 collected = 110 verified Patch 3 tests (all byte-unchanged) + 13 new
  test_booking_db tests + 4 new test_migration_schema tests.

RUN HISTORY (complete and honest)
- Run 1 (final): 127/127 as recorded above. No failures occurred and no
  correction pass was needed; every file ran exactly as delivered in the
  Patch 4 implementation.

VERIFIED PATCH 4 BEHAVIOR (each item covered by the passing suite)
- Pending appointments can be confirmed by authorized staff through the
  supported transition (Senior Audit Critical #4).
- pending -> confirmed executes under the tenant-scoped
  SELECT ... FOR UPDATE appointment lock.
- Confirming an already-confirmed appointment returns an idempotent
  success (reason already_confirmed) with zero duplicate effects.
- confirmed_at is written only on the FIRST staff pending -> confirmed
  action, normalized to aware UTC through ensure_utc.
- Repeated confirmation preserves the original confirmed_at value
  byte-for-byte, even with a different injected instant.
- Auto-confirmed appointments (require_staff_confirmation=false) keep
  confirmed_at=NULL, both at creation and after a staff re-confirm —
  finalize_booking never sets the column.
- Cancelled, completed, and no_show appointments cannot be confirmed
  (not_confirmable, detail carries the current status).
- Unknown and cross-tenant appointment IDs return the same tenant-isolated
  404 behavior with character-identical wording.
- Every failed transition is mutation-free: appointment status,
  confirmed_at, slot status, notification flags, and notify_error are all
  proven byte-unchanged.
- Confirming sends no patient notification of any kind (Patch 2D policy
  unchanged) and no additional office SMS or email — both provider send
  functions were trap-proven at zero invocations on failure AND success
  paths.
- Concurrent confirmation (two threads, two sessions, different injected
  instants) produces exactly one real transition: one reason "ok", one
  "already_confirmed", exactly one timestamp written (the winner's), and
  the loser observes exactly that same value.
- The slot remains BOOKED after confirmation; confirmation never reads,
  locks, or changes the slot row.
- AppointmentView exposes nullable confirmed_at consistently in the
  confirm response and the appointment-list response (set values survive a
  later cancellation; pending rows show null).
- Migration 004 is additive, reversible, fails loudly on duplicate apply
  (no IF NOT EXISTS on the up), removes only confirmed_at on rollback
  (IF EXISTS down proven re-runnable as a no-op) while preserving
  everything from 001, 002, and 003, and reapplies successfully after a
  down (up -> down -> up round-trip).
- All Patch 1, 2A, 2B, 2C, 2D, and 3 regression tests passed (the full
  110-test baseline ran byte-unchanged in the same suite).

DEPLOYMENT NOTES (restated at closure)
- Production rollout order is 001 -> 002 -> 003 -> 004 -> Patch 4 code.
- Patch 4 code MUST NOT deploy before migration 004: the ORM model, the
  confirm service, and the admin appointment views reference confirmed_at.
- The manual staging/widget regression checklist earlier in this report
  remains a pre-production requirement.

STATUS
- Patch 4 is VERIFIED LOCALLY. Senior Audit Critical #4 (no supported
  staff-confirmation transition) is closed at the code level: the verified
  behavior is exactly the approved product contract and final conditions.
- CHECKPOINT (Rule 18): Patch 4 closed 2026-07-12 with owner approval.
  Rollback point: the verified Patch 3 checkpoint file versions (revert
  app/calendar_models.py, app/services/booking_service.py,
  app/routes/calendar.py, calendar_tests/test_booking_db.py,
  calendar_tests/test_migration_schema.py, docs/INTEGRATION.md, and this
  report; delete both 004 migration files). If migration 004 was applied
  to a database, revert or stop the Patch 4 code FIRST, then run
  migrations/004_staff_confirmation_down.sql (drops only confirmed_at;
  statuses already 'confirmed' remain 'confirmed'). Operational note: no
  settings flag exists or is needed — the confirm endpoint is
  staff-invoked only, and not calling it reproduces pre-Patch-4 behavior
  exactly.
- Patch 5 (Senior Audit Critical #2 — tenant authorization) and all other
  audit findings: NOT started — awaiting explicit approval and scope.

============================================================================
# PATCH 5 — PER-TENANT CALENDAR ADMIN AUTHORIZATION (Senior Audit Critical #2)
============================================================================

GOAL
Replace the single shared ADMIN_API_KEY on the six /admin/calendar/* routes
with per-office credentials. The authenticated credential determines the
tenant; the request's client_id must equal it or the route answers 404
"Client not found." exactly as if the id did not exist. Caller-supplied
foreign client_ids are never queried. The global key loses ALL Calendar
access with no fallback; the non-calendar /admin routes keep it unchanged.

FILES CHANGED
New:
1. app/services/calendar_admin_auth.py — the single authorization owner.
2. migrations/005_calendar_admin_credentials_up.sql
3. migrations/005_calendar_admin_credentials_down.sql
4. calendar_tests/test_admin_auth.py — 31 collected tests.
Modified:
5. app/calendar_models.py — CalendarAdminCredential ORM model added
   (mirrors 005 exactly); import list gains CheckConstraint. Nothing else.
6. app/routes/calendar.py — see FUNCTIONS CHANGED.
7. calendar_tests/test_booking_db.py — 8 call sites in exactly 2 existing
   tests adapted to the new dependency signature (see below); every
   assertion and expected result preserved; test count unchanged (44
   collected from this file as before).
8. calendar_tests/test_migration_schema.py — 4 new self-contained 005
   tests appended (003/004 standard); 12 existing tests untouched.
9. docs/INTEGRATION.md — §6/§7 curl examples now use $CALENDAR_ADMIN_KEY
   with a Patch 5 note; new §8 (provisioning, rotation, safe cutover;
   placeholders only, no real key or hash anywhere).
10. CHANGE_REPORT.md — this section.
Not modified (confirmed): calendar_tests/conftest.py, requirements.txt,
app/routes/admin.py, app/config.py, app/models.py, app/database.py,
app/main.py, repositories, booking/hold/notification services, chat.py,
widget files, consent language, migrations 001–004, unrelated tests.

FUNCTIONS CHANGED
app/services/calendar_admin_auth.py (all new):
- hash_calendar_admin_key(raw) -> 64-hex SHA-256.
- generate_calendar_admin_key() -> (raw, hash); pure, takes no session.
- authenticate_calendar_admin(db, raw_key) -> Client. Owns format
  validation (mia_cal_ + 43 base64url chars), hashing, lookup by the
  unique key_hash index joined to clients, active/revoked checks (BOTH
  checked independently in the application despite the DB CHECK — fails
  closed against drift/corruption), Client.active check, tenant return.
  Every credential failure is the identical 401 "Invalid admin key.".
  Database errors: session rolled back, ORIGINAL exception propagates —
  never converted to 401, never any global-key fallback (the module does
  not import app.config).
app/routes/calendar.py:
- REMOVED: require_admin (global-key gate), load_client_or_404
  (caller-supplied-id lookup — the vulnerability itself).
- ADDED: require_calendar_admin (transport wiring only: OPTIONAL
  X-Admin-Key header so a missing header is 401 not 422, session
  injection, delegate to the owner); require_tenant_match (the one
  mismatch-first comparison, 404 "Client not found.").
- create_slots, list_slots, block_slot, list_appointments,
  confirm_appointment, cancel_appointment: dependency `_ : None =
  Depends(require_admin)` -> `authenticated_client: Client =
  Depends(require_calendar_admin)`; body starts with
  `client = require_tenant_match(<requested id>, authenticated_client)`.
  In list_appointments the tenant gate now precedes the
  end_day<start_day 422 check (authorization before parameter
  semantics; see BEHAVIOR ADDED). No other route logic touched.
app/calendar_models.py:
- ADDED CalendarAdminCredential: UUID PK (DB default gen_random_uuid(),
  client-side uuid4 for ORM inserts), client_id FK ON DELETE RESTRICT,
  key_hash VARCHAR(64) NOT NULL, label TEXT NOT NULL, active BOOLEAN NOT
  NULL DEFAULT true, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ NULL; CHECK key_hash ~ '^[0-9a-f]{64}$'; CHECK
  NOT (active AND revoked_at IS NOT NULL); the ONE named unique index
  uq_cal_admin_cred_key_hash (no unique=True column duplication —
  approved condition 5); ix_cal_admin_cred_client_id.
calendar_tests/test_booking_db.py (adapted only as required):
- test_admin_routes_use_dst_safe_windows: 2 call sites `_=None` ->
  `authenticated_client=client_row`; one docstring sentence updated.
- test_confirm_route_status_mapping: 6 call sites; the cross-tenant probe
  now passes `authenticated_client=office_b` (office B authenticates as
  itself and probes A's appointment id — same 404, same wording, same
  assertions); comment extended to say so.

DATABASE CHANGES
Migration 005 (additive only): calendar_admin_credentials table as above,
with COMMENTs. UP has no IF NOT EXISTS (fails loudly on reapply). DOWN
drops the table with IF EXISTS (safe-rollback; repeat run is a no-op).
No existing table/column/index/row touched. Stored data is hashes only.

BEHAVIOR ADDED
- Calendar routes authenticate per-office credentials; the credential is
  the tenant. Mismatched client_id -> 404 "Client not found.",
  indistinguishable (response AND database activity) from a nonexistent
  id; the foreign id is never queried; repository client_id filtering
  remains as defense in depth.
- Global ADMIN_API_KEY -> 401 on every Calendar route.
- Missing/empty/malformed/unknown/revoked/inactive-client credentials ->
  the identical 401 "Invalid admin key." (missing header is 401, not 422).
- Auth DB failure -> rollback + visible server failure (fail closed).
- Rotation supported: multiple active credentials per office; revocation
  (active=false, revoked_at=now()) effective on the next request.
- One deliberate response change: mismatched tenant + invalid date range
  on GET appointments is now 404 (was 422 for a global-key caller) —
  authorization is checked before parameter semantics.

BEHAVIOR INTENTIONALLY UNCHANGED
- All six routes' post-authorization logic, wording, status codes, and
  views, byte-for-byte (422/404/409 semantics, idempotent confirm,
  confirmed_at rules, DST-safe windows, no notifications from admin
  actions).
- app/routes/admin.py and its global-key behavior; chat/booking/widget
  paths (they never used these routes); migrations 001–004; conftest.

RISKS
- Deploying Patch 5 code before migration 005 + provisioning locks staff
  out of Calendar admin (the cutover order in docs/INTEGRATION.md §8 and
  DEPLOYMENT below is mandatory).
- Lost raw keys are unrecoverable by design (hashes only) — re-provision.
- The ORM CHECK `~` regex is PostgreSQL syntax: any future SQLite
  create_all of calendar tables would fail loudly (suite and production
  are PostgreSQL; documented in the model).
- HTTP tests add a test-only httpx dependency (approved; NOT added to
  requirements.txt).

TESTS TO PERFORM (local, disposable PostgreSQL 16 — Windows PowerShell)
  python -m pip install pytest sqlalchemy psycopg2-binary httpx
  docker run --name mia-calendar-test-db -d -p 5433:5432 `
    -e POSTGRES_PASSWORD=test `
    -e POSTGRES_DB=mia_calendar_test `
    postgres:16
  $env:ALLOW_DESTRUCTIVE_CALENDAR_TESTS = "yes"
  $env:TEST_DATABASE_URL =
    "postgresql://postgres:test@localhost:5433/mia_calendar_test"
  $env:DATABASE_URL = $env:TEST_DATABASE_URL
  python -m pytest calendar_tests --collect-only -q
  python -m pytest calendar_tests -v --tb=short
Expected: 162 collected, 162 passed, 0 failed, 0 skipped, 0 errors —
127 verified Patch 4 tests + 31 test_admin_auth (27 planned + 3
cross-tenant mutation-free write cases + 1 auth-DB-failure case) + 4
migration 005 tests.

VERIFICATION STATUS (Rule 19 — honest)
Commands actually executed in this delivery environment (which has NO
PostgreSQL, NO network, and NO installed sqlalchemy/fastapi/pytest/httpx):
- python3 -m py_compile on all six changed/new Python files — all compile.
- Static condition checks (AST/DDL): no Column(unique=...) duplication;
  key_hash DDL is VARCHAR(64); hex + consistency CHECKs and ON DELETE
  RESTRICT present; neither calendar_admin_auth.py nor routes/calendar.py
  references app.config; exactly 6 routes use require_calendar_admin and
  require_tenant_match; 401/404 details byte-exact; parametrize-aware
  collected count of test_admin_auth.py is exactly 31; migration test file
  is 16 (12 existing + 4 new); test_booking_db.py remains 44 functions
  with zero residual `_=None` sites.
THE 162-TEST SUITE HAS NOT BEEN RUN. Patch 5 is NOT verified. No behavior
or test-result claim above beyond compilation and the static checks is
asserted as verified (Rules 16/19).

----------------------------------------------------------------------------
PATCH 5 — LOCAL RUN 1 RESULT AND TEST-ONLY CORRECTION
----------------------------------------------------------------------------

LOCAL POSTGRESQL RUN 1 (owner): 162 collected, 161 passed, 1 failed,
0 skipped, 0 errors, 8.74 seconds. Patch 5 NOT verified.

FAILED: test_tenant_mismatch_rejected_before_foreign_lookup
  (assert not foreign_id_queried -> assert not True)

ROOT CAUSE (test instrumentation, NOT a production defect):
- _provision(db, client_row) commits the shared SQLAlchemy session, which
  EXPIRES the office_b ORM object (expire_on_commit).
- The SQL capture listener was installed BEFORE the test resolved
  office_b.id — while building the request and again while evaluating the
  captured statements, accessing the expired object issued an ORM fixture
  refresh SELECT carrying Office B's UUID.
- The listener captured that fixture refresh and the assertion mistook it
  for a foreign-tenant query by the route. The route itself never queried
  the foreign id.
- NO PRODUCTION DEFECT FOUND. PRODUCTION CODE UNCHANGED — app/routes/
  calendar.py, app/services/calendar_admin_auth.py, app/calendar_models.py,
  and both 005 migrations are byte-identical to the delivered
  implementation.

FIX (calendar_tests/test_admin_auth.py, this one test only):
- All ORM-backed values are resolved to plain UUID/str
  (foreign_client_id / foreign_id_text) BEFORE the listener is installed;
  office_b is never accessed while the listener is active (statically
  checked).
- The request is built from foreign_id_text; captured SQL is inspected
  against the same pre-resolved foreign_id_text.
- Capture representation made robust: (str(statement), repr(parameters)).
- Assertions preserved AND strengthened: 404; detail exactly
  "Client not found."; capture ran AND contains the
  credential-authentication SELECT (calendar_admin_credentials);
  foreign_id_text appears in no captured statement and in no captured
  bound parameters. The SQL-capture assertion was not weakened; nothing
  was mocked.

FILES CHANGED (this pass): calendar_tests/test_admin_auth.py and
CHANGE_REPORT.md only (plus the regenerated PATCH5_UNIFIED_DIFFS.txt
delivery artifact). No test added or removed; collection remains 162.

CHECKPOINT: remains OPEN until the corrected 162-test run passes locally.

DEPLOYMENT (approved safe rollout)
1. Apply migration 005 (after 001–004). 2. Provision credentials; store
only hashes. 3. Securely configure raw keys in the intended tools.
4. Verify the entire cutover in staging. 5. Deploy Patch 5 code during a
controlled cutover. 6. Immediately test: own-tenant success; cross-tenant
404; old global key 401 on Calendar routes; non-calendar /admin still
accepts the global key. 7. If provisioned credentials fail, immediately
revert to Patch 4 code (it never reads the credential table, so global-key
access resumes at once). No global fallback exists or is allowed.

ROLLBACK METHOD
Revert to the verified Patch 4 checkpoint versions of
app/calendar_models.py, app/routes/calendar.py,
calendar_tests/test_booking_db.py, calendar_tests/test_migration_schema.py,
docs/INTEGRATION.md, and this report; delete
app/services/calendar_admin_auth.py, calendar_tests/test_admin_auth.py,
and both 005 migration files. If migration 005 was applied to a database,
revert the code FIRST, then optionally run
migrations/005_calendar_admin_credentials_down.sql (drops only the
credential table; discards stored hashes — raw keys in staff tools become
inert; re-applying later requires re-provisioning).

STATUS
- Implementation complete per the approved architecture and all nine final
  conditions. CHECKPOINT (Rule 18): OPEN — awaiting the owner's local
  162-test PostgreSQL run. Fix-failures-only mode applies next.
- Patch 6: NOT started.

----------------------------------------------------------------------------
PATCH 5 — LOCAL VERIFICATION COMPLETE; CHECKPOINT CLOSED
----------------------------------------------------------------------------

FINAL LOCAL POSTGRESQL RUN (owner, 2026-07-13)
- Environment: Windows, Python 3.14.2, pytest 9.1.1, fresh Patch 5 virtual
  environment, disposable PostgreSQL 16 Docker container. No production
  Supabase credentials, no production patient data, no real SMS or email
  provider executed. The Docker container was removed after verification.
- Result: 162 collected, 162 passed, 0 failed, 0 skipped, 0 errors, in
  8.70 seconds. No PostgreSQL test was skipped.
- 162 collected = 127 verified Patch 4 tests + 31 test_admin_auth tests
  (27 planned + 3 cross-tenant mutation-free write cases + 1
  auth-database-failure case) + 4 migration 005 tests.

RUN HISTORY (complete and honest)
- Run 1: 162 collected, 161 passed, 1 failed, 0 skipped, 0 errors
  (8.74 seconds). The single failure
  (test_tenant_mismatch_rejected_before_foreign_lookup) was TEST
  INSTRUMENTATION ONLY: the SQL capture listener recorded an ORM fixture
  refresh SELECT caused by accessing the expired office_b object after the
  listener was installed (the shared session's commit in _provision expires
  it), and mistook that refresh for a foreign-tenant query by the route.
  NO PRODUCTION DEFECT WAS FOUND. The correction pass changed ONLY
  calendar_tests/test_admin_auth.py (pre-resolving the foreign UUID to
  plain values before installing the listener; robust parameter repr; the
  SQL-capture assertion preserved and strengthened to require the
  credential-authentication SELECT) and CHANGE_REPORT.md. Production code
  remained byte-unchanged throughout.
- Run 2 (final): 162/162 as recorded above.

VERIFIED PATCH 5 BEHAVIOR (each item covered by the passing suite)
- Calendar admin credentials are bound to exactly one office.
- Raw credentials are never stored: only SHA-256 lowercase hexadecimal
  hashes are persisted (and the migration's hex CHECK rejects raw-key-
  shaped values).
- Missing, empty, malformed, unknown, revoked, and inactive-client keys
  all return identical 401 responses with the exact detail
  "Invalid admin key.".
- A missing X-Admin-Key header returns 401, not 422.
- The old global ADMIN_API_KEY receives 401 on every Calendar route.
- Non-calendar app/routes/admin.py remains unchanged and still accepts the
  global key.
- The authenticated credential determines the tenant; the caller-supplied
  client_id must match the authenticated tenant.
- Tenant mismatch returns 404 "Client not found." on every Calendar route,
  indistinguishable from a nonexistent client id.
- Foreign tenant IDs are not queried after authentication (proven by
  cursor-level statement capture).
- Cross-tenant block, cancel, and confirm attempts are mutation-free:
  Office B's appointment status, confirmed_at, notification flags,
  notify_error, and slot state are byte-unchanged, with zero executions of
  any service/repository write owner.
- Cross-tenant write attempts send no SMS or email (both provider send
  functions trap-proven at zero invocations).
- Repository client_id filtering remains in place as defense in depth.
- Credential rotation works: two overlapping active credentials both
  authenticate; revoking the first kills only the first.
- Revocation takes effect on the next request.
- Authentication database failures roll back the session, propagate as
  visible server failures (never converted to 401), never consult a global
  fallback, and no route business operation executes (trap-proven at zero
  calls with the slot state unchanged).
- Migration 005 is additive, reversible, fails loudly on duplicate apply
  (no IF NOT EXISTS on the up), preserves migrations 001 through 004 on
  rollback (proven with 003 and 004 applied), and reapplies after a down
  (up -> down -> up round-trip); a repeat down run is a harmless no-op.
- All Patch 1, 2A, 2B, 2C, 2D, 3, and 4 regression tests passed (the full
  127-test baseline ran in the same suite, including the two adapted
  route-signature tests with every assertion preserved).

DEPLOYMENT NOTES (restated at closure)
- Production rollout order:
  1. Apply migration 005.
  2. Provision per-office credentials.
  3. Store only hashes.
  4. Securely configure raw keys in staff tools.
  5. Verify in staging.
  6. Deploy Patch 5 code.
  7. Immediately verify own-tenant success, cross-tenant denial, old
     global key rejection on Calendar routes, and continued non-calendar
     admin access.
- No global-key fallback is allowed — none exists in the code.
- Production migrations 001 through 005 remain deployment prerequisites,
  in order, before Patch 5 code.
- The manual staging/widget regression checklist earlier in this report
  remains a pre-production requirement.

STATUS
- Patch 5 is VERIFIED LOCALLY. Senior Audit Critical #2 (a single shared
  admin key authorizing any client_id) is closed at the code level: the
  verified behavior is exactly the approved architecture and all nine
  final implementation conditions.
- CHECKPOINT (Rule 18): Patch 5 closed 2026-07-13 with owner approval.
  Rollback point: the verified Patch 4 checkpoint file versions (revert
  app/calendar_models.py, app/routes/calendar.py,
  calendar_tests/test_booking_db.py, calendar_tests/test_migration_schema.py,
  docs/INTEGRATION.md, and this report; delete
  app/services/calendar_admin_auth.py, calendar_tests/test_admin_auth.py,
  and both 005 migration files). If migration 005 was applied to a
  database, revert or stop the Patch 5 code FIRST (Patch 4 code never
  reads the credential table, so global-key access resumes at once), then
  optionally run migrations/005_calendar_admin_credentials_down.sql
  (drops only the credential table; stored hashes are discarded — raw
  keys in staff tools become inert; re-applying later requires
  re-provisioning).
- Patch 6 and all other audit findings: NOT started — awaiting explicit
  approval and scope.

===============================================================================
PATCH 6 — HTML ESCAPING AND STORED-ERROR SANITIZATION
(Senior Audit RECOMMENDED FINDING #7)
Delivered: 2026-07-14 — LOCAL VERIFICATION PENDING (see STATUS)
===============================================================================

GOAL
Close Recommended Finding #7 for all four staff notification outputs
(calendar office email, calendar office SMS, lead office email, lead office
SMS): HTML-escape untrusted text exactly once at the email rendering
boundary; normalize and bound every untrusted plain-text field value at the
notification output boundary; replace stored/API notification errors with a
fixed closed vocabulary; withhold legacy/malformed stored errors at the
AppointmentView boundary; remove notification internals (notify_errors)
from the patient-facing booking reply meta; make the completed-lead meta
error values exactly "send_failed"; and make the covered notification and
Calendar error-path server logs PII- and provider-detail-free (including
removing repr(exception) and traceback output at the seven Calendar error
boundaries, per the owner's strike decision of 2026-07-14).

FILES CHANGED (9 — exactly the approved allowed list)
1. app/services/notification_service.py
2. app/services/booking_conversation.py
3. app/routes/calendar.py
4. app/routes/chat.py
5. calendar_tests/test_notification_policy.py  (+12 tests)
6. calendar_tests/test_booking_db.py           (+3 tests)
7. calendar_tests/test_chat_integration.py     (+4 tests)
8. docs/INTEGRATION.md                          (section 5 extended)
9. CHANGE_REPORT.md                             (this appended section)

FUNCTIONS CHANGED / ADDED
- notification_service.py — ADDED single owners (Rule 3):
  normalize_notification_field, render_email_html,
  sanitize_stored_notify_error, sanitized_exception_class; named constants
  FIELD_LIMIT_NAME/PHONE/EMAIL/FREE_TEXT/ENUM/SOURCE/PRACTICE_NAME (120/32/
  254/300/16/32/120), SUBJECT_MAX_LENGTH (160), SEND_FAILED,
  OFFICE_SMS/EMAIL_SEND_FAILED, OFFICE_SMS/EMAIL_SKIPPED (skipped strings
  byte-identical to pre-Patch-6), VALID_NOTIFY_ERROR_VALUES (8 values),
  NOTIFY_ERROR_MAX_LENGTH (112), NOTIFY_ERROR_WITHHELD.
  CHANGED: build_office_sms and build_office_email_body normalize/bound
  field VALUES only (labels, order, wording, structural newlines
  unchanged); send_booking_notifications stores the fixed vocabulary
  entries on channel failure (never exception text), builds a normalized,
  160-bounded subject, and logs controlled fields only; _send_email routes
  the plain body through render_email_html; _record_outcome's failure log
  is class-name + UUID only. build_patient_sms (FUTURE-ONLY, no call site)
  is byte-unchanged.
- booking_conversation.py — _handle_confirmation: send_booking_notifications
  return value no longer captured; "notify_errors" removed from the booked
  reply meta. No other keys, wording, or control flow changed.
- calendar.py — _appointment_view: notify_error now passes through
  sanitize_stored_notify_error (approved vocabulary passes unchanged;
  anything else returns "notification_error: detail_withheld"). Stored
  values are never rewritten.
- chat.py —
  * imports the Patch 6 single-owner helpers/constants from
    notification_service (no logic duplicated in chat.py — Rule 3);
  * send_office_lead_email: subject normalized to 160 at the boundary,
    body rendered via render_email_html (recipient, provider, env vars,
    and wording unchanged);
  * build_staff_lead_summary / build_staff_lead_sms: every untrusted VALUE
    normalized/bounded; labels, order, wording, newline joins and " | "
    separators unchanged;
  * notify_office_of_completed_lead: begin diagnostics reduced to one
    controlled event line (previously printed office contacts and complete
    summary/SMS bodies); on provider failure email_send_error /
    sms_send_error are exactly SEND_FAILED (previously str(e)) and the
    failure log carries event/channel/code/exc_class/conversation UUID
    only. Success prints, recipients, per-channel flags, and idempotency
    behavior unchanged;
  * [NEXT_BOOKING_CAPTURE_PROMPT] and [BOOKING_CAPTURE] diagnostics now
    print booleans + conversation UUID instead of repr'd patient name,
    phone, and prompt text (supersedes the Patch 3 byte-identical
    preservation of the latter, per the approved Patch 6 contract);
  * the SEVEN Calendar error-boundary sites (completion delegation,
    conversation-ending cleanup, three emergency cleanups, ownership
    cleanup, continuation hook) now log
    "CALENDAR ... ERROR: exc_class=<sanitized> conversation=<uuid>" with
    NO repr(e) and NO traceback.print_exc(); their seven paired rollback
    error prints are likewise class-name + UUID only. Every rollback call,
    fallback reply, status/mode, and retry/ownership behavior is
    byte-unchanged — log content only, per the strike decision.

DATABASE CHANGES
None. No migration, no schema change, no stored value rewritten.

BEHAVIOR ADDED
- Untrusted text in staff emails is HTML-inert (escaped once, fixed <pre>
  wrapper); staff SMS values are flattened/bounded plain text (never
  HTML-escaped).
- Email subjects (both emails) are control-character-free and <= 160.
- appointments.notify_error is a closed 8-value vocabulary (max 112);
  AppointmentView withholds anything outside it as
  "notification_error: detail_withheld".
- Booking reply meta no longer contains notify_errors.
- lead_email_error / lead_sms_error are None or exactly "send_failed".
- Covered server logs carry only fixed event names, channels, fixed codes,
  sanitized exception class names, booleans, and UUIDs.

BEHAVIOR INTENTIONALLY UNCHANGED
- Notification recipients, providers, env vars, per-channel isolation,
  channel order (SMS then email), skipped-entry strings, wording, field
  order, and template structure of every staff message.
- Patient-facing booking wording and all other meta keys; patient SMS
  policy (disabled — Patch 2D); consent language; widget HTML/JS.
- Booking lifecycle, availability logic, tenant authentication,
  repositories, migrations 001–005, admin routes.
- Error-boundary control flow: every rollback, fallback reply, status,
  mode, and ownership/retry behavior at the seven Calendar sites.
- The unrelated EXTRACTOR ERROR, FAQ EVENT ERROR, and OPENAI ERROR
  diagnostics (explicitly out of scope; deliberately left as-is).

RISKS
- Log-shape change: operators grepping for old [LEAD_SUMMARY]/[LEAD_SMS]/
  [LEAD_NOTIFY_EMAIL]/[LEAD_NOTIFY_PHONE]/[LEAD_EMAIL_ERROR]/
  [LEAD_SMS_ERROR] lines must switch to the [LEAD_NOTIFY] event lines;
  debugging provider failures now relies on exception class + UUIDs plus
  provider-side dashboards (intentional trade for PII/secret removal).
- Any external admin-UI code that displayed raw legacy notify_error text
  will now see "notification_error: detail_withheld" for legacy rows.
- Any consumer that read reply meta notify_errors (none known; widget does
  not) would lose that key.
- Extremely long legitimate values (>300-char reasons) are visibly
  truncated with "…" in notifications only; stored data is complete.

TESTS (Rule 11) — 19 new, matrix as approved
- test_notification_policy.py (12): name-markup escaping; script/handler
  escaping; practice-name escaping; ampersand/quote escaping readable;
  fixed <pre> wrapper + single-pass proof; subject CR/LF + 160 bound;
  exact normalization contract; SMS plain/flattened/bounded with template
  structure and no HTML escaping; provider exception never persisted
  (message NOR class name); secret/header/URL-shaped text absent from
  errors, storage, and AppointmentView with patient channel untouched; two
  failures exact SMS-then-email value + 8-value vocabulary with proven
  112 max passing the sanitizer; channel-failure log controlled-fields.
- test_booking_db.py (3): all approved vocabulary values pass the view
  unchanged (and None -> None); six malformed/legacy shapes each withheld
  with storage untouched; full failing-provider booking conversation has
  no notify_errors meta key with all other keys/wording preserved and
  honest per-channel storage intact.
- test_chat_integration.py (4): lead email escaped once inside the
  byte-identical wrapper; builder values flattened/bounded with structure
  intact + subject header-injection bound at the real send boundary;
  lead failure meta exactly send_failed with raw provider text absent from
  ChatResponse; end-to-end hostile-name run — lead SMS flattened/unescaped
  with " | " intact, lead-email failure and CALENDAR ERROR boundary logs
  controlled-fields only, and no name/phone/URL/header/token/message/
  traceback in captured server output.
- Zero existing assertions weakened; zero existing tests removed. The
  skipped-entry strings and email wrapper are byte-identical, so every
  prior notify_error and notification assertion passes unchanged.

EXPECTED COLLECTION
162 (verified Patch 5 baseline) + 12 + 3 + 4 = 181 collected / 181 passed.
Parametrize-aware AST count of the four changed test files: 105 -> 124
(+19); no new parametrization.

VERIFICATION COMMANDS (authoring environment, this delivery)
- python3 patch6_chat_edits.py            (all 30 chat.py edits applied with
  exact asserted match counts; 7 calendar tracebacks removed; the 3
  out-of-scope tracebacks confirmed remaining)
- python3 -m py_compile on all four changed app modules and all three
  changed test files (pass)
- Stdlib-only smoke execution of the four pure helpers (normalization
  contract, single-pass escaping, wrapper bytes, 8-value vocabulary with
  112 max, sanitizer pass/withhold matrix, class-name sanitizer) — pass.
  These are helper-logic checks only, NOT the suite.

HONEST TEST RESULTS (Rule 19)
The full PostgreSQL suite was NOT run in the authoring environment (no
Docker/PostgreSQL or app dependencies available there). NO pass claim is
made. Patch 6 is NOT verified until the owner runs the complete suite
locally (disposable PostgreSQL 16, TEST_DATABASE_URL) and observes
181 collected / 181 passed / 0 failed / 0 skipped / 0 errors, after which
the STATUS below must be updated to VERIFIED with the actual numbers.

ROLLBACK
Code-only patch: restore the verified Patch 5 checkpoint versions of the
nine files above (equivalently: git revert of the Patch 6 commit). No
migration to reverse; no data changes to undo. Legacy notify_error rows
were never rewritten, so rollback restores their previous raw display
automatically.

DEPLOYMENT NOTES
- No new migration. Production prerequisite order is unchanged: migrations
  001–005 before any patch code.
- Deploy is code-only; no credential, env var, or provider configuration
  changes.
- The manual staging/widget regression checklist earlier in this report
  remains a pre-production requirement.

STATUS
- Patch 6 DELIVERED 2026-07-14 under the owner's final conditions
  (strike-option group INCLUDED: the seven Calendar error boundaries were
  changed, log content only). LOCAL VERIFICATION PENDING — not closed, no
  checkpoint yet (Rule 18/19).
- Confirmation: no out-of-scope file changed (git status limited to the
  nine allowed files). Patch 7: NOT started.

-------------------------------------------------------------------------------
PATCH 6 — STATIC-REVIEW CORRECTION PASS (2026-07-14, pre-verification)
-------------------------------------------------------------------------------
Scope: exactly the three defects from the owner's static review. Files
changed in this pass: app/routes/chat.py, calendar_tests/test_booking_db.py,
calendar_tests/test_chat_integration.py, CHANGE_REPORT.md (this block).
notification_service.py, booking_conversation.py, calendar.py,
test_notification_policy.py, and INTEGRATION.md are byte-unchanged from the
Patch 6 delivery.

1. Two remaining PII-bearing chat.py logs removed (the delivered inventory
   had missed both):
   - The [GATE] diagnostic no longer prints user_text[:80]; it now prints
     text_present (boolean), text_length (integer), and the conversation
     UUID. Every gate calculation and all routing behavior unchanged.
   - The emergency FOLLOW-UP intake "DEBUG:" print (actual lead name +
     phone) is replaced by a controlled [EMERGENCY_FOLLOWUP] event log:
     emergency/has_name/has_phone booleans + conversation UUID. Emergency
     intake behavior, replies, notification execution, lead fields, status,
     and metadata unchanged.
   A repository-wide sweep now shows the ONLY prints referencing patient-
   derived values or repr(exception) are the three explicitly out-of-scope
   diagnostics (EXTRACTOR ERROR, FAQ EVENT ERROR, OPENAI ERROR).

2. Lead-email subject: the approved 120-char practice-name limit is now
   enforced via the shared owner (normalize_notification_field with
   FIELD_LIMIT_PRACTICE_NAME, "Dental Office" fallback) BEFORE subject
   assembly in notify_office_of_completed_lead; the complete subject still
   passes the 160-char boundary normalization inside send_office_lead_email.
   No normalization logic duplicated in chat.py.

3. test_booked_reply_meta_has_no_notify_errors_key corrected: the office
   now has both notification contacts configured, counting traps prove
   _send_sms and _send_email are each invoked exactly once and raise,
   booking still succeeds, notify_errors stays absent from patient-facing
   meta with wording/other keys unchanged, appointment.notify_error is
   exactly "office_sms: send_failed; office_email: send_failed", and
   patient_sms_sent stays False. No new test function for this item.

TESTS in this pass:
- Strengthened: test_lead_email_subject_and_body_values_normalized_bounded
  (practice-name 119 + U+2026 proof at assembly, subject <= 160, CR/LF
  proof retained, stored client.practice_name unchanged);
  test_lead_sms_values_normalized_and_server_logs_pii_free ([GATE] present
  with text_present/text_length, raw "text=" field absent);
  test_booked_reply_meta_has_no_notify_errors_key (as above).
- Added (required — NO existing test reaches the emergency follow-up
  logging path, which requires the previous assistant turn to be the
  emergency contact prompt):
  test_emergency_followup_logs_controlled_fields_only.

EXPECTED COLLECTION (recalculated honestly): 162 + 20 = 182 collected /
182 passed. Parametrize-aware AST count of the four changed test files:
105 -> 125; no parametrization.

VERIFICATION STATUS unchanged: the full PostgreSQL suite has NOT been run
in the authoring environment; NO pass claim is made. Patch 6 remains
LOCAL VERIFICATION PENDING. Patch 7: NOT started.

-------------------------------------------------------------------------------
PATCH 6 — LOCAL RUN 1 RESULT AND TEST-FIXTURE CORRECTION (2026-07-14)
-------------------------------------------------------------------------------
FIRST LOCAL POSTGRESQL RUN (owner's machine, disposable PostgreSQL 16):
- 182 collected
- 171 passed
- 0 failed
- 11 setup errors
- 0 skipped
- 10.83 seconds

ROOT CAUSE (test-only defect; no production defect found):
- The 11 new notification-policy tests that use database fixtures requested
  a nonexistent pytest fixture named `client`; the verified shared conftest
  fixture is `client_row`. pytest stopped those 11 tests during setup —
  their bodies never executed. (The 12th new test in that file,
  test_field_normalization_contract_exact, takes no fixtures and passed.)
- All 171 tests that reached execution passed, including every Patch 1–5
  regression and the other 9 new Patch 6 tests.
- Production code is unchanged and was not implicated.

CORRECTION (smallest safe edit; calendar_tests/test_notification_policy.py
and this report only):
- Each of the 11 affected test signatures now requests `client_row`, and
  the first statement of each body is `client = client_row`, preserving
  every existing statement and assertion verbatim. No fixture added, no
  conftest change, no test added or removed, no assertion weakened.
- AST verification: 21 test functions in the file (unchanged); zero tests
  request a bare `client` fixture; expected collection remains 182.

STATUS: Patch 6 remains OPEN — LOCAL VERIFICATION PENDING the corrected
rerun (expected 182 collected / 182 passed / 0 failed / 0 skipped /
0 errors). No verification is claimed. Patch 7: NOT started.

-------------------------------------------------------------------------------
PATCH 6 — FINAL LOCAL VERIFICATION AND CHECKPOINT (2026-07-14)
-------------------------------------------------------------------------------

VERIFICATION STATUS (honest, per Rule 19)
- VERIFIED LOCALLY by the project owner on 2026-07-14:
    Environment: Windows, Python 3.14.2, pytest 9.1.1, fresh Patch 6
    virtual environment, disposable PostgreSQL 16 Docker container, no
    production Supabase credentials, no production patient data, no real
    SMS or email provider executed. The Docker test container was removed
    after verification.
- Complete honest run history (both runs preserved):
    Run 1: 182 collected; 171 passed; 0 failed; 11 setup errors;
    0 skipped; 10.83 seconds. Root cause: the 11 new tests in
    calendar_tests/test_notification_policy.py requested the nonexistent
    `client` fixture instead of the verified `client_row` fixture; pytest
    stopped those tests during setup. No production defect was found;
    production code remained unchanged.
    Correction between runs: only
    calendar_tests/test_notification_policy.py and CHANGE_REPORT.md
    changed. All 11 tests were corrected to use `client_row`. No tests
    were added, removed, or weakened.
    Run 2 (final): 182 collected; 182 passed; 0 failed; 0 skipped;
    0 errors. No PostgreSQL test was skipped. Completion time: not
    recorded (the closure instruction's duration placeholder was left
    unfilled; a timing addendum may be appended if supplied).

VERIFIED BEHAVIOR (test-proven on PostgreSQL)
- Calendar office email safely escapes untrusted plain text at the HTML
  rendering boundary; the lead office email safely escapes untrusted
  plain text at the same shared rendering boundary; the fixed HTML <pre>
  markup remains intact.
- Business data remains unescaped in database storage and in JSON
  responses.
- Calendar office SMS remains readable plain text and is not
  HTML-escaped; lead office SMS remains readable plain text and is not
  HTML-escaped.
- Untrusted notification fields are normalized and bounded using the
  approved deterministic contract (control characters 0-31/127/128-159 to
  spaces, whitespace collapsed, stripped, output-boundary truncation to
  limit minus one characters plus U+2026).
- Email subjects cannot contain CR/LF injection; complete subjects stay
  within 160 characters.
- Practice names obey the approved 120-character output limit, including
  inside the lead-email subject before assembly.
- Calendar notify_error stores only the approved fixed vocabulary
  (maximum valid length 112). Raw exception strings, exception class
  names, URLs, headers, credentials, payloads, and stack traces are never
  stored in notify_error.
- AppointmentView passes approved values through unchanged and withholds
  malformed, legacy, duplicate, reversed, unknown, or over-length values
  as exactly: notification_error: detail_withheld. Stored values are
  never rewritten.
- notify_errors is absent from patient-facing Calendar booking metadata.
- lead_email_error and lead_sms_error expose only send_failed or None.
- Patient-facing responses contain no provider exception details.
- Patient SMS remains disabled (Patch 2D policy); no patient email was
  introduced.
- PII-bearing lead and Calendar diagnostic logs were removed or replaced
  with controlled event, boolean, code, class-name, and UUID fields.
- Raw patient messages, names, phones, emails, reasons, notification
  bodies, SQL parameters, provider messages, URLs, headers, secrets, and
  stack traces are absent from the covered server logs.
- Notification recipients, sending providers, routing, patient wording,
  rollback behavior, and persisted notification flags remain unchanged.
- All Patch 1 through Patch 5 regression tests passed in the same suite.

DEPLOYMENT NOTES (restated at closure)
- Patch 6 required no database migration; the deploy is code-only.
- Production migrations 001 through 005 remain deployment prerequisites,
  in order, each applied before its patch's code.
- The manual staging/widget regression checklist earlier in this report
  remains a pre-production requirement.

STATUS
- Patch 6 is VERIFIED LOCALLY. Senior Audit Recommended Finding #7 (HTML
  escaping and stored-error sanitization) is closed at the code level:
  the verified behavior is exactly the approved architecture, the
  approved final conditions (strike-option group included), and both
  approved correction passes.
- CHECKPOINT (Rule 18): Patch 6 closed 2026-07-14 with owner approval.
  Rollback point: the verified Patch 5 checkpoint file versions of the
  nine Patch 6 files (no migration involved). WARNING: rolling back
  RESTORES unsafe raw-error exposure, unescaped email HTML, and
  PII-bearing server logs — any rollback must use the complete Patch 5
  checkpoint, never partially, and the rolled-back state must not be
  deployed to production.
- Patch 7 and all other audit findings: NOT started — awaiting explicit
  approval and scope.


==============================================================================
# PATCH 7 — APPOINTMENT-CANCELLATION LIFECYCLE TRANSITIONS
# (Senior Audit Recommended #6) — VERIFIED LOCALLY / CLOSED 2026-07-14
==============================================================================

## Goal

Replace cancel_appointment's single "not already cancelled" check with an
explicit lifecycle allow-list: only PENDING and CONFIRMED appointments may
be cancelled. COMPLETED and NO_SHOW (and any future status) are rejected
mutation-free with the new reason not_cancellable, so a finished
appointment is never rewritten and its historical slot is never reopened.
Approved product decisions: D1 — already_cancelled stays a mutation-free
success=False / HTTP 409 (not converted to idempotent 200); D2 — the
optional past-slot temporal release guard is struck (no now_utc in
cancellation, no past/future distinction); D3 — cancelled_at deferred (no
model change, no migration 006).

## Files changed

- app/services/booking_service.py — new module constant
  _CANCELLABLE_STATUSES (frozenset: PENDING, CONFIRMED); new allow-list
  guard in cancel_appointment AFTER the already_cancelled check and BEFORE
  any mutation, returning BookingResult(False, "not_cancellable",
  appointment, detail=appointment.status); BookingResult reason-vocabulary
  comment gains not_cancellable; cancel_appointment docstring rewritten
  with the complete transition table (Rule 8/14).
- app/routes/calendar.py — cancel route maps not_cancellable to
  409 "Appointment is {detail} and cannot be cancelled." (exact mirror of
  the confirm route's wording); docstring updated. detail carries only a
  controlled AppointmentStatus word.
- calendar_tests/test_booking_db.py — header coverage-map entry; helper
  _slot_snapshot; seven new test functions (T1 parametrized over
  completed/no_show → eight collected cases). See "Tests added".
- docs/INTEGRATION.md — new "Staff cancellation lifecycle (Patch 7)"
  subsection in §7 documenting the endpoint contract, allow-list, exact
  409 wordings, tenant-indistinguishable 404, and zero-notification
  policy.
- CHANGE_REPORT.md — this appended record.

## Functions changed

- booking_service.cancel_appointment (guard + docstring; no signature
  change)
- routes.calendar.cancel_appointment (one new outcome mapping + docstring)

No other function changed. finalize_booking, confirm_appointment,
place_hold, all repositories, models, auth, notifications, availability
policy, and chat behavior are byte-identical to the Patch 6 checkpoint.

## Database changes

NONE. No model change, no repository change, no migration, no index or
constraint change, no cancelled_at. Code-only deploy; migrations 001–005
remain the production prerequisites in order.

## Behavior added

- completed → cancellation rejected: service reason not_cancellable
  (detail "completed"), HTTP 409, mutation-free, slot untouched.
- no_show → same rejection (detail "no_show").
- Any status outside {pending, confirmed, cancelled-with-its-own-reason}
  is rejected by default (allow-list, Rule 4).
- Correction pass 1: BookingResult.detail is SANITIZED in the lifecycle
  owner — a stored status that is a member of AppointmentStatus.ALL passes
  through exactly; any stored value outside AppointmentStatus.ALL (the
  status column has no database CHECK constraint, so malformed / legacy /
  manually edited / mixed-version rows are possible) is represented
  externally ONLY as the fixed sentinel "unsupported". The raw stored
  value is never echoed through detail or the HTTP 409 response, and it
  is never repaired or rewritten.

## Behavior intentionally unchanged

- pending → cancelled and confirmed → cancelled: identical mutation, slot
  release, hold-field clearing, single-commit transaction, confirmed_at
  preservation.
- already_cancelled: same reason, same success=False, same 409 wording,
  still mutation-free (approved D1).
- Missing/foreign-tenant: same slot_missing reason, same indistinguishable
  404 wording (Patch 5 posture).
- Zero notifications on every cancellation path; patient SMS remains
  disabled (Patch 2D).
- Lock order (appointment row → slot row), repository queries, exception
  propagation (Rule 16), and rollback guarantees.

## Risks

- Low. The guard is pure Python inside the existing row lock; no
  concurrency surface changes. An operator who previously "cancelled"
  completed/no_show rows to reopen old slots loses that (defective)
  shortcut — intended by the audit.
- Correction pass 1 removed the original design's reliance on
  creation-time validation: because appointments.status has no database
  CHECK constraint, a stored value outside AppointmentStatus.ALL is
  possible and the API boundary cannot treat the column as trusted.
  detail is now sanitized in the lifecycle owner (controlled vocabulary
  passes through; everything else becomes "unsupported"), so no
  uncontrolled stored text can enter the HTTP response. Note:
  confirm_appointment's not_confirmable detail (Patch 4) still passes the
  stored status through unsanitized — same class of exposure, out of this
  correction pass's approved scope; flagged for a future decision.

## Tests added (7 functions, 8 collected cases — T1 parametrized ×2)

- T1 test_cancel_terminal_status_rejected[completed|no_show]
- T2 test_repeat_cancel_is_mutation_free
- T3 test_cancel_other_client_appointment_indistinguishable
- T4 test_concurrent_cancel_same_appointment_single_transition (threaded,
  two sessions, barrier, bounded joins)
- T5 test_concurrent_cancel_and_confirm_deterministic (threaded; only the
  two legal outcome pairs)
- T6 test_cancel_commit_failure_rolls_back_cleanly (commit raises;
  fresh-session proof of zero partial mutation)
- T7 test_cancel_route_status_mapping (200 / 404 identical-wording unknown
  and REAL cross-tenant / 409 already-cancelled / 409 exact terminal
  wording; correction pass 1: malformed stored status rejected at the
  service level with detail exactly "unsupported" and at the route level
  with exactly "Appointment is unsupported and cannot be cancelled.", the
  raw stored value absent from the response and NOT rewritten; rejected
  calls mutation-free; no private data in detail)

Expected collection: 182 → 190 (parametrize-aware design count).

## Rollback method

Restore the Patch 6 checkpoint versions of the four other changed files
(booking_service.py, calendar.py, test_booking_db.py, INTEGRATION.md).
CHANGE_REPORT.md is append-only: this section is never deleted — a
rollback, if performed, is recorded by APPENDING a rollback note instead.
No migration to reverse;
code-only in both directions. Rolling back re-opens Recommended #6
(completed/no_show become cancellable and past slots can be reopened) but
carries no data-loss or security regression beyond that.

## Correction pass 1 (static review — lifecycle response safety)

Defect: cancel_appointment returned detail=appointment.status for every
status outside the allow-list, and the route places result.detail directly
into the HTTP 409. The status column has no CHECK constraint, so a
malformed / legacy / manually edited / mixed-version row could carry an
uncontrolled value into the response. The original claim that
creation-time validation makes every stored status controlled was NOT
sufficient at this API boundary and has been withdrawn.

Fix (booking_service.py only; guard and guard order preserved; route
wording unchanged; no model/migration/constraint change; stored data never
repaired): before constructing the not_cancellable result, derive
safe_detail = appointment.status if it is a member of
AppointmentStatus.ALL, else the fixed sentinel "unsupported"; return only
safe_detail through BookingResult.detail. Malformed-status HTTP response
is therefore exactly: Appointment is unsupported and cannot be cancelled.

Tests: test_cancel_route_status_mapping strengthened in place (no test
function added or removed; collection unchanged at 190) with a
malformed-status scenario proving the service-level "unsupported" detail,
the exact route wording, absence of the raw stored value, mutation-free
rejection (appointment incl. confirmed_at and notification fields, slot
status, hold fields), and no private data in the response.

## Verification status (honest, per Rule 19) — FINAL

- Implementation: COMPLETED (delivery of 2026-07-14, including correction
  pass 1).
- Static checks: COMPLETED in the authoring environment and RE-RUN after
  correction pass 1 — python syntax compilation of all changed .py files;
  parametrize-aware AST collection count of the new tests (7 functions →
  8 cases, unchanged by the correction); grep proof
  that calendar_models.py, appointment_repository.py, conftest.py,
  test_admin_auth.py, test_migration_schema.py, and all migrations are
  unchanged.
- PostgreSQL verification: PERFORMED AND PASSED in the owner's local
  environment on 2026-07-14 (see "LOCAL VERIFICATION" below).

## LOCAL VERIFICATION (2026-07-14)

Environment:

- Windows, Python 3.14.2, pytest 9.1.1
- Fresh Patch 7 virtual environment
- Disposable PostgreSQL 16 Docker container (removed after verification)
- No production Supabase credentials, no production patient data, no real
  SMS or email provider executed

Final run:

- 190 tests collected
- 190 tests passed
- 0 failed, 0 skipped, 0 errors
- No PostgreSQL test was skipped

VERIFIED BEHAVIOR (Recommended #6 lifecycle contract)

- Pending appointments can be cancelled; confirmed appointments can be
  cancelled; confirmed_at is preserved after cancellation.
- A valid cancellation releases the booked slot using the existing
  behavior (slot -> available, hold fields cleared, one transaction).
- Repeated cancellation remains mutation-free and returns
  already_cancelled with HTTP 409 (approved decision D1).
- Completed appointments cannot be cancelled; no_show appointments cannot
  be cancelled. A rejected terminal-status cancellation leaves the
  appointment unchanged and the slot BOOKED; hold fields, confirmed_at,
  notification flags, and notify_error are all unchanged on every
  rejection path.
- Malformed or legacy stored statuses (outside AppointmentStatus.ALL) are
  rejected by default; the malformed stored text is never echoed through
  the API and is represented externally only as "unsupported"; the
  malformed stored value is not rewritten (correction pass 1).
- Real foreign-tenant appointments remain indistinguishable from missing
  appointments, and cross-tenant cancellation attempts are mutation-free.
- Cancellation sends no office SMS, no office email, and no patient SMS;
  no patient email was introduced (Patch 2D policy preserved).
- Simultaneous cancellation requests produce exactly one valid transition
  and one already_cancelled result (threaded, two real sessions).
- Cancellation racing with confirmation produces only the two approved
  deterministic outcomes; the final status is CANCELLED in both, with
  confirmed_at populated exactly when the confirmation won.
- A cancellation commit failure propagates and rolls back without partial
  mutation (proven through an independent session).
- HTTP mappings remain privacy-preserving (404 indistinguishable wording;
  409 detail carries only controlled vocabulary or the fixed
  "unsupported" sentinel).
- Tenant authentication (Patch 5) and row-lock behavior remain unchanged.
- All Patch 1 through Patch 6 regression tests passed in the same suite.

DEPLOYMENT NOTES (restated at closure)

- Patch 7 required no database migration; no model or repository change;
  no cancelled_at field was introduced. The deploy is code-only.
- Production migrations 001 through 005 remain deployment prerequisites,
  in order, each applied before its patch's code.
- The manual staging/widget regression checklist earlier in this report
  remains a pre-production requirement.

KNOWN OPEN ITEM (unchanged by Patch 7)

- confirm_appointment's not_confirmable path (Patch 4) still reflects its
  stored status directly into detail WITHOUT the new "unsupported"
  fallback — the same boundary class correction pass 1 closed for
  cancellation. That issue remains open, was deliberately not changed in
  Patch 7 (out of approved scope), and awaits its own decision.

STATUS

- Patch 7 is VERIFIED LOCALLY. Senior Audit Recommended Finding #6
  (appointment-cancellation lifecycle transitions) is closed at the code
  level: the verified behavior is exactly the approved lifecycle contract
  (D1/D2/D3) plus approved correction pass 1.
- CHECKPOINT (Rule 18): Patch 7 closed 2026-07-14 with owner approval.
  Rollback point: the verified Patch 6 checkpoint file versions of the
  five Patch 7 files (no migration involved). Rolling back reopens the
  invalid completed/no_show cancellation behavior (Recommended #6) —
  a rollback should not be deployed to production.
- Patch 8 and all other open audit findings: NOT started — awaiting
  explicit approval and scope.


==================================================================
# PATCH 8 — CONFIRM-APPOINTMENT UNSUPPORTED STATUS SANITIZATION
# (response-boundary correction; the confirm-side mirror of the
# cancellation path's correction pass 1)
# STATUS: IMPLEMENTED — LOCAL VERIFICATION PENDING
==================================================================

DATE: 2026-07-14 (implementation delivery; NOT closed)

GOAL

Close the KNOWN OPEN ITEM recorded at Patch 7 closure:
confirm_appointment's not_confirmable path (Patch 4) reflected the stored
appointment.status directly into BookingResult.detail, and the admin
confirm route interpolated that detail verbatim into its HTTP 409 body.
Because appointments.status has no database CHECK constraint, a malformed,
legacy, manually edited, or mixed-version stored value could be echoed
through the API. Patch 8 sanitizes the detail at the service boundary,
exactly as correction pass 1 did for cancellation.

APPROVED IMPLEMENTATION SHAPE

Inline mirror inside confirm_appointment (owner decision): the sanitizing
expression is written inline in the not_confirmable branch, byte-matching
the cancel path's verified expression. NO shared helper was extracted and
the verified Patch 7 cancel_appointment branch was NOT touched.

FILES CHANGED (4 — plus delivery artifacts)

1. app/services/booking_service.py
   - confirm_appointment, not_confirmable branch ONLY: detail is now
       safe_detail = (appointment.status
                      if appointment.status in AppointmentStatus.ALL
                      else "unsupported")
     instead of appointment.status directly. Teaching comment added
     (PATCH 8, cross-referencing the cancel path's correction pass 1).
   - confirm_appointment docstring: not_confirmable entry now documents
     the sanitized-detail contract (controlled vocabulary passes through
     exactly; anything else is represented ONLY as "unsupported"; the raw
     stored value is never echoed and never repaired or rewritten).
   - NOTHING else changed: branch order (locked tenant-filtered lookup ->
     missing -> already_confirmed -> pending mutation -> not_confirmable),
     confirmed_at behavior, idempotency, transaction/rollback behavior,
     slot non-involvement, and the no-notification policy are untouched.

2. calendar_tests/test_booking_db.py
   - test_confirm_route_status_mapping STRENGTHENED (no new test function,
     no new parametrization; monkeypatch fixture added to the signature):
     * cancelled 409 assertion upgraded from substring to the EXACT
       wording "Appointment is cancelled and cannot be confirmed."
     * completed and no_show route passes added with EXACT wording and
       mutation-free snapshots (row incl. confirmed_at + notification
       fields; slot status; hold fields), slot stays BOOKED.
     * malformed stored status (a fixed test literal proven outside
       AppointmentStatus.ALL, direct-written — no CHECK constraint):
       service-level proof detail == "unsupported"; route-level proof the
       409 body is exactly "Appointment is unsupported and cannot be
       confirmed."; raw value absent from the response; row byte-for-byte
       unchanged across BOTH rejected calls; stored malformed value still
       present (never repaired); slot + hold fields unchanged.
     * foreign-tenant 404 probe now also proven mutation-free.
     * both notification provider functions trapped for the WHOLE test;
       final assertion proves zero SMS/email invocations on every path.
   - No existing assertion weakened; existing 200/repeat-idempotent/404
     coverage retained verbatim.

3. docs/INTEGRATION.md
   - Confirm-endpoint section: the 409 bullet now lists the three exact
     controlled wordings, and a new bullet documents the "unsupported"
     sentinel contract (mirroring the cancel section): controlled terminal
     statuses returned unchanged; uncontrolled stored values represented
     externally ONLY as "unsupported"; the raw stored value is never
     returned; the stored value is not rewritten; no migration or cleanup
     occurs.

4. CHANGE_REPORT.md — this appended section.

FILES DELIBERATELY UNCHANGED

- app/routes/calendar.py — the route already interpolates whatever the
  service supplies; with the service guaranteeing detail is a member of
  AppointmentStatus.ALL or exactly "unsupported", the route is safe
  unchanged (same ownership decision as correction pass 1).
- app/calendar_models.py, app/repositories/appointment_repository.py,
  migrations, authentication, notification services, availability, chat,
  widget files, requirements, conftest.py, all unrelated tests, and the
  Patch 7 cancellation sanitization.

DATABASE / MIGRATION IMPACT

NONE. No CHECK constraint, no migration, no model field, no repository
method, no cleanup script. Malformed legacy rows remain byte-identical by
design. Migrations 001 through 005 remain the production prerequisites,
in order; Patch 8 deploys as code-only.

EXPECTED TEST COLLECTION

190 -> 190. One existing test function strengthened; zero functions added
or removed; zero parametrization changes (parametrize-aware AST count of
the changed test file confirms an unchanged per-file collected count).

VERIFICATION STATUS (Rule 19 — honest verification)

LOCAL VERIFICATION PENDING. The authoring environment for this delivery
has no PostgreSQL 16 container and no network access, so the suite WAS NOT
RUN. No pass/fail claim is made. Static checks performed in the authoring
environment: py_compile on booking_service.py; AST syntax + parametrize-
aware test count on test_booking_db.py; grep proofs that the raw
appointment.status no longer feeds BookingResult.detail in
confirm_appointment. Patch 8 must not be marked closed until Kevin runs
the full PostgreSQL suite locally (expected 190 collected / 190 passed).

ROLLBACK

Restore the verified Patch 7 checkpoint versions of:
  app/services/booking_service.py
  calendar_tests/test_booking_db.py
  docs/INTEGRATION.md
(CHANGE_REPORT.md is append-only: a rollback is recorded as a NEW note,
never by editing this section.) Code-only rollback; no migration involved.
Rolling back reopens the raw-status echo on the confirm 409 — a rollback
should not be deployed to production.

STATUS

- Patch 8 implemented; LOCAL VERIFICATION PENDING.
- Patch 9 and all other open audit findings: NOT started — awaiting
  explicit approval and scope.


PATCH 8 — FINAL LOCAL VERIFICATION AND CHECKPOINT (2026-07-14)
-------------------------------------------------------------------------------

VERIFICATION STATUS (honest, per Rule 19)
- VERIFIED LOCALLY by the project owner on 2026-07-14:
    Environment: Windows, Python 3.14.2, pytest 9.1.1, disposable
    PostgreSQL 16 Docker container, no production Supabase credentials,
    no production patient data, no real SMS or email provider executed.
    The Docker test container was removed after verification.
- Final run: 190 collected; 190 passed; 0 failed; 0 skipped; 0 errors.
    No PostgreSQL test was skipped. Completion time: not recorded (the
    closure instruction's duration placeholder was left unfilled; a
    timing addendum may be appended if supplied).
- Collection matched the parametrize-aware expectation exactly
    (190 -> 190: one existing test strengthened, no test function added
    or removed, no parametrization change).

VERIFIED BEHAVIOR (test-proven on PostgreSQL)
- Pending appointment confirmation still succeeds (pending -> confirmed).
- Repeated confirmation remains an idempotent success.
- confirmed_at is created ONLY by the pending -> confirmed transition and
  is preserved byte-for-byte during repeated confirmation.
- Cancelled, completed, and no_show appointments remain not confirmable.
- Controlled statuses appear UNCHANGED in the 409 response detail
  ("Appointment is cancelled / completed / no_show and cannot be
  confirmed." — exact wordings asserted).
- Malformed or legacy stored statuses (values outside
  AppointmentStatus.ALL; the status column has no CHECK constraint) are
  rejected by DEFAULT.
- Malformed stored status text never reaches BookingResult.detail and
  never reaches the HTTP response; malformed statuses are externally
  represented ONLY as the fixed sentinel "unsupported"
  ("Appointment is unsupported and cannot be confirmed." — exact wording
  asserted; raw stored value asserted absent).
- The malformed stored value remains unchanged in the database — it is
  never repaired, rewritten, normalized, or truncated.
- Rejected confirmation does not alter the appointment (status,
  confirmed_at, notification bookkeeping all snapshot-proven unchanged).
- Rejected confirmation does not alter the slot or its hold fields.
- Rejected confirmation does not alter office_sms_sent,
  office_email_sent, patient_sms_sent, or notify_error.
- Confirmation sends no office SMS, no office email, and no patient SMS
  (both provider send functions trapped; zero invocations on every path);
  no patient email was introduced.
- Missing and REAL foreign-tenant appointment ids remain
  indistinguishable (identical 404 wording, mutation-free probes).
- Tenant authentication (Patch 5 per-office credentials) and
  tenant-scoped SELECT ... FOR UPDATE row locking remain unchanged.
- Transaction boundaries and rollback behavior remain unchanged: the
  pending path commits once; every other path rolls back having written
  nothing.
- The Patch 7 cancellation branch remains unchanged (byte-verified at
  implementation delivery against the Patch 7 checkpoint).
- All Patch 1 through Patch 7 regression tests passed in the same suite.

DEPLOYMENT NOTES (restated at closure)
- Patch 8 required no database migration; no model, repository, route,
  or database change was required. The deploy is code-only.
- Production migrations 001 through 005 remain deployment prerequisites,
  in order, each applied before its patch's code.
- The manual staging/widget regression checklist earlier in this report
  remains a pre-production requirement.

STATUS
- Patch 8 is VERIFIED LOCALLY / CLOSED 2026-07-14. The KNOWN OPEN ITEM
  recorded at Patch 7 closure (confirm_appointment reflecting its stored
  status directly into detail without the "unsupported" fallback) is
  closed at the code level: the verified behavior is exactly the
  approved response-safety contract, implemented as the approved inline
  mirror of the cancellation path's correction pass 1.
- CHECKPOINT (Rule 18): Patch 8 closed 2026-07-14 with owner approval.
  Rollback point: the verified Patch 7 checkpoint file versions of the
  three Patch 8 code/doc files (booking_service.py, test_booking_db.py,
  INTEGRATION.md; no migration involved). WARNING: rolling back REOPENS
  the raw malformed-status confirmation response issue — a rolled-back
  state must not be deployed to production.
- Patch 9 and all remaining open audit findings: NOT started — awaiting
  explicit approval and scope.

================================================================================
# PATCH 9A — SYNCHRONOUS NOTIFICATION ATTEMPT LEDGER AND
# DUPLICATE SUPPRESSION (Senior Audit Recommended #1)
# STATUS: IMPLEMENTED — LOCAL VERIFICATION PENDING
================================================================================

Implemented 2026-07-14 against the Patch 8 checkpoint (190/190 verified).
NO VERIFICATION CLAIM IS MADE: the full local PostgreSQL suite has NOT been
run in the authoring environment (Rule 19). Static checks are complete
(py_compile on all changed Python files; parametrization-aware AST
collection count; convention greps: no app.config imports, no unique=True
column duplication beside a named index, no CHAR(n), no IF NOT EXISTS in
the up migration, forbidden-column absence in 006, migration/ORM
constraint-name parity). SQLAlchemy import-level validation and the
PostgreSQL run remain OUTSTANDING and must complete in Kevin's environment
before any closure claim.

GOAL
Make office-notification duplicate suppression a database invariant: an
atomic per-channel claim (INSERT ... SELECT ... ON CONFLICT DO NOTHING
RETURNING) into the new notification_attempts ledger precedes every
provider execution; each outcome (sending -> sent/unknown) commits
atomically with the fully recomputed appointment projection under the
appointment row lock; providers run transaction-free and lock-free on an
immutable scalar snapshot; legacy pre-006 appointments are protected at
runtime (approved Option B, no backfill); malformed legacy notify_error is
preserved byte-identically and never echoed. NOT in 9A: retry, recovery,
stale-claim processing, workers, cron, provider idempotency keys, payload
or provider-message-ID storage, patient messaging, 9B, 9C, Patch 10. 9A is
not a transactional outbox.

FILES CHANGED (10 — exactly the approved allowed list; conftest.py NOT
modified, no fixture change proved necessary)
1. migrations/006_notification_attempts_up.sql            (new)
2. migrations/006_notification_attempts_down.sql          (new)
3. app/calendar_models.py                                 (additive)
4. app/repositories/notification_attempt_repository.py    (new)
5. app/services/notification_service.py                   (reworked)
6. calendar_tests/test_notification_idempotency.py        (new)
7. calendar_tests/test_migration_schema.py                (additive)
8. docs/INTEGRATION.md                                    (additive, §9)
9. CHANGE_REPORT.md                                       (this entry)
10. delivery artifacts (diff, file set, this record)

FUNCTIONS CHANGED / ADDED
- calendar_models: NotificationChannel, NotificationAttemptStatus,
  NotificationAttempt (mirrors 006 exactly).
- notification_attempt_repository (new; sole ledger SQL owner):
  claim_channel_attempt, get_attempts_by_appointment,
  cas_attempt_to_terminal, get_attempt_for_tenant; ClaimDisposition,
  ClaimResult (frozen).
- notification_service: send_booking_notifications (rebuilt around
  entry contract -> snapshot -> per-channel claim -> boundary check ->
  provider -> atomic outcome+projection -> final reconciliation);
  NotificationSnapshot + build_notification_snapshot;
  _appointment_uuid_without_sql; _open_transaction_is_provably_readonly;
  _Projection + _compute_projection (monotonic formula);
  _apply_and_commit_projection; _commit_outcome_and_projection;
  _reconcile_projection. _send_email now transmits pre-rendered HTML
  (render_email_html runs exactly once, at snapshot time; it remains the
  single HTML owner). REMOVED: _record_outcome (replaced by the
  projection recompute — Rule 3, single projection owner).

DATABASE CHANGES
Migration 006 (additive, reversible, documented): notification_attempts
(id, appointment_id, channel, status, created_at, resolved_at) with
fk_notification_attempts_appointment (ON DELETE RESTRICT),
ck_notification_attempt_channel (office_sms/office_email only — no
patient channel representable), ck_notification_attempt_status
(sending/sent/unknown), ck_notification_attempt_resolution (sending <=>
resolved_at IS NULL), uq_notification_attempt_per_channel
(appointment_id, channel — the claim arbiter). No client_id column by
design (derived tenancy through appointments). Up fails loudly on
reapplication; down drops only 006's table (IF EXISTS no-op semantics).
No existing object or row is touched; no backfill.

BEHAVIOR ADDED
- At-most-one post-cutover provider execution per appointment/channel
  (database-arbitrated; repeat/concurrent invocations suppress).
- Honest three-state ledger; provider exceptions recorded as unknown;
  "sent" documented as API-success-only, never delivery.
- Atomic outcome+projection commits; monotonic sent flags (a true flag
  can never become false); fixed SMS-first error composition preserved.
- Mandatory entry session contract with safe abstention; transaction-free
  provider boundary with safe abstention (claim stays honestly sending).
- Runtime legacy suppression; malformed notify_error byte-preservation,
  flags-only updates, [] outcome errors, one controlled withheld event.
- Controlled events added (fixed names + channel/UUID only):
  entry_contract_violation, transaction_boundary_violation,
  in_flight_suppressed, projection_inconsistency, legacy_error_withheld,
  outcome_appointment_missing. channel_send_failed and
  outcome_record_failed keep their exact pre-9A formats.

BEHAVIOR INTENTIONALLY UNCHANGED
- Booking success remains notification-independent. Patient SMS remains
  disabled (Patch 2D); patient_sms_sent is no longer rewritten by the
  projection (server default False; honest legacy history preserved).
- Patch 6 vocabulary, grammar, field limits, AppointmentView withheld
  marker, HTML escaping, and log-field policy unchanged.
- booking_service, booking_conversation, appointment_repository,
  calendar.py, chat.py, auth, availability, holds, settings, migrations
  001–005, providers/recipients, widget files: untouched.

DEVIATION FLAG D1 (requires Kevin's explicit confirmation)
The approved entry contract reads "session.in_transaction() is False"
with abstention otherwise. Implemented as specified EXCEPT: an open
transaction that PostgreSQL proves has performed reads only (no xact id
assigned — pg_current_xact_id_if_assigned() IS NULL; writes AND row locks
both assign one) is safely released and the invocation proceeds. Reason:
the verified Patch 2D/6 regression tests (unmodifiable) evaluate
arguments (settings load) against expired ORM attributes immediately
before invoking the service, which autobegins exactly such a read-only
transaction; literal abstention would fail ~15 verified Patch 1–8 tests,
contradicting the approval's mandatory all-tests-pass requirement. The
release is conditional, provably safe, fail-closed on any error, and
covered by four dedicated entry tests (pending write DML, row lock, dirty
identity map: abstain without rollback; proven-read-only: release and
proceed).

TEST COUNTS (parametrization-aware AST, Patch 3 convention)
- Baseline: 190 collected (Patch 8 checkpoint, verified by Kevin).
- New: test_notification_idempotency.py — 39 functions, 43 collected
  (4 functions parametrized x2); test_migration_schema.py — +5 functions,
  +5 collected.
- EXPECTED COLLECTED: 238. The 253 design estimate is superseded: several
  Revision-4 matrix rows were folded into parametrized or combined
  functions during implementation (permitted by the approval's
  do-not-force-253 instruction). Every bullet of the approved required
  coverage list is implemented; the exact count must be confirmed by
  pytest collection in Kevin's environment.

RISKS
- SQLAlchemy construct pg_insert().from_select().on_conflict_do_nothing()
  .returning() requires SQLAlchemy 2.x semantics; not import-validated in
  the authoring environment (no SQLAlchemy available). First local run
  will surface any construct issue immediately in the claim tests.
- pg_current_xact_id_if_assigned() requires PostgreSQL 13+ (local 16 and
  Supabase both qualify).
- Mixed-version overlap is NOT duplicate-safe; the documented cutover in
  docs/INTEGRATION.md §9 is mandatory.

TESTS TO PERFORM (Kevin's environment)
docker-based disposable PostgreSQL 16, then:
  python -m pytest calendar_tests/ -q
Expect 238 collected / 238 passed / 0 failed / 0 skipped / 0 errors,
including all 190 Patch 1–8 tests. Then the staging cutover drill
(INTEGRATION.md §9 steps 7–9).

ROLLBACK
Code: revert notification_service.py, calendar_models.py, delete
notification_attempt_repository.py and the two new/extended test
sections, restore INTEGRATION.md §9 removal — Patch 8 behavior returns
(it never reads the ledger). Database: run
006_notification_attempts_down.sql AFTER the code revert (drops the
ledger; appointment projections untouched). Order: code first, then
migration down.

DEPLOYMENT ORDER
001 -> 002 -> 003 -> 004 -> 005 -> 006 -> Patch 9A code, with the
REQUIRED no-overlap cutover (stop/drain all pre-9A instances before the
9A code serves traffic). 9B, 9C, and Patch 10 are NOT started.

--------------------------------------------------------------------------------
# PATCH 9A — CORRECTION PASS 1 (STATIC REVIEW), 2026-07-14
# STATUS: IMPLEMENTED — LOCAL VERIFICATION PENDING
--------------------------------------------------------------------------------

PACKAGE 1 RESULT (recorded honestly): STATIC-REVIEW FAILURE ONLY. The
PostgreSQL suite was never run; no pass/fail claim ever existed; five
blockers were found by static review before installation. Patch 9A remains
LOCAL VERIFICATION PENDING.

CORRECTIONS APPLIED (allowed correction files only; migrations proven
correct and untouched; notification_attempt_repository.py untouched — no
repository defect was involved):

1. _format_local RESTORED at module scope in notification_service.py,
   byte-identical formatter wording and timezone behavior. Root cause: the
   package-1 snapshot-section edit consumed the function header, orphaning
   its body as unreachable code after "return assigned is None" inside the
   (now deleted) entry helper. The orphaned lines are removed. A static
   AST regression test now proves _format_local exists at module scope and
   is called by build_office_sms, build_office_email_body, and
   build_patient_sms.

2. EXACT PATCH 6 SKIPPED VOCABULARY. The idempotency tests used invalid
   shortened strings ("office_sms: skipped" / "office_email: skipped").
   Every affected seed and assertion (zero-claim reconciliation, legacy
   skipped, both recipients missing, recipient configured later, the
   combined value) now imports and uses the production constants
   OFFICE_SMS_SKIPPED / OFFICE_EMAIL_SKIPPED. No string duplication in the
   test file; no production vocabulary change.

3. ORM/MIGRATION TYPE PARITY. NotificationAttempt.channel and .status are
   now Column(Text) — TEXT exactly, matching migration 006 (String/VARCHAR
   removed). test_006_matches_orm_model_exactly now compiles every ORM
   column type on the PostgreSQL dialect and compares it against
   information_schema's actual data_type (uuid / text / timestamp with
   time zone), so a TEXT->VARCHAR drift fails the suite. Constraint and
   index names unchanged.

4. UNAPPROVED DEVIATION D1 REMOVED. The entry contract is now STRICT:
   new/dirty/deleted empty AND in_transaction() False, or the service
   abstains — no PostgreSQL classification query, no rollback, all
   caller-owned state preserved, one controlled event only.
   _open_transaction_is_provably_readonly and every
   pg_current_xact_id_if_assigned usage are deleted; the release-approving
   test is replaced by test_entry_readonly_transaction_abstains_strictly;
   the D1 claims in code, docs, and delivery are withdrawn (the package-1
   report entry above is superseded by this record — append-only, not
   edited). A new test proves the PRODUCTION path satisfies the strict
   contract: immediately after finalize_booking's commit (place_hold ->
   finalize_booking on the real services), the session is clean and
   transaction-free, both channels execute, and the session returns clean.
   NEW-FILE test helpers now end their own read work (settings evaluated,
   then rollback) before invoking the service — mirroring production.

   SCOPE-EXPANSION REQUEST (pending Kevin's decision — no verified file
   was modified): calendar_tests/test_notification_policy.py's module
   helper _send (line 113) evaluates _settings(client) inline, which lazily
   refreshes the expired client row (SessionLocal defaults
   expire_on_commit=True) and autobegins a read-only transaction that is
   still open at the service call. Under the strict contract its 16 _send
   call sites will abstain and fail. Requested narrow adaptation, exactly
   per the correction directive (end test-owned read work; no assertion
   removed or weakened):
       def _send(db, client, appointment):
           from app.services import notification_service
           settings = _settings(client)
           db.rollback()   # end test-owned read work (strict entry contract)
           return notification_service.send_booking_notifications(
               db, client, appointment, settings)
   Local verification MUST wait for this decision; without it the suite
   cannot pass.

5. CLAIM DATABASE FAILURES ISOLATED. claim_channel_attempt and the claim
   commit are wrapped per channel: on any exception the notification
   transaction rolls back, the provider is NOT called (never send without
   a committed claim), no false outcome exists, one controlled
   claim_record_failed event is logged (fixed name, channel, sanitized
   exception class, appointment UUID — raw text withheld), the other
   channel continues from a clean transaction-free session, final
   reconciliation still runs, and the function returns normally. Three
   deterministic tests added (repository exception; claim-commit failure
   incl. later clean re-claim; full finalize-path booking survival with
   both claims failing), plus the formatter AST regression from item 1.

RECALCULATED EXPECTED COLLECTION (parametrization-aware AST)
- test_notification_idempotency.py: 43 functions -> 47 collected
  (4 functions parametrized x2).
- test_migration_schema.py: +5 (unchanged count, one test strengthened).
- EXPECTED: 190 + 52 = 242 collected — superseding 238 — CONTINGENT on
  the scope-expansion request in item 4; without it, the 16 policy-file
  _send sites fail by strict-contract abstention.

STATIC CHECKS RE-RUN: py_compile clean on all changed files; vocabulary
grep clean; no pg_current_xact/provably_readonly/D1 references remain;
convention greps unchanged-clean. PostgreSQL suite still NOT run; no
verification is claimed. 9B, 9C, and Patch 10 remain not started.

--------------------------------------------------------------------------------
# PATCH 9A — TRANSMISSION-INCIDENT RECORD, 2026-07-14
--------------------------------------------------------------------------------
An internet interruption caused a STALE response (the old Revision 4
planning document) to be transmitted after Correction Pass 1 had already
been applied to the working tree. The stale transmission MADE NO FILE
CHANGES. The working tree was re-verified afterwards, blocker by blocker,
against the correction directive: (1) _format_local at module scope, no
unreachable code after any top-level return anywhere in the module;
(2) zero shortened skipped strings, 11 production-constant usages;
(3) NotificationAttempt.channel/.status are Text, dialect-compiled type
parity test in place; (4) strict single-line entry contract, zero
D1/provably_readonly/pg_current_xact traces in code, tests, or docs, strict
read-only-abstention and production-finalize-path tests present; (5)
EVENT_CLAIM_RECORD_FAILED isolation in the claim path with its three
deterministic tests. py_compile clean; parametrization-aware count
unchanged at 190 + 47 + 5 = 242 expected (contingent on the pending
test_notification_policy._send scope-expansion decision recorded above).
Patch 9A remains LOCAL VERIFICATION PENDING; the PostgreSQL suite has not
been run and no verification is claimed. 9B, 9C, and Patch 10 not started.

--------------------------------------------------------------------------------
# PATCH 9A — NARROW TEST-SCOPE EXPANSION APPLIED (v3), 2026-07-14
# STATUS: IMPLEMENTED — LOCAL VERIFICATION PENDING
--------------------------------------------------------------------------------
Approved and applied: calendar_tests/test_notification_policy.py's module
helper _send opened a TEST-OWNED read-only transaction while loading
settings (lazy refresh of the expired client row, expire_on_commit=True),
which the strict Patch 9A entry contract would correctly reject. The
helper now evaluates settings first, ends only that test-owned read
transaction (db.rollback()), then calls send_booking_notifications with
the precomputed settings — entering the service with the same
clean-session contract as the production caller. STRICT PRODUCTION ENTRY
BEHAVIOR IS UNCHANGED; only the test helper was adapted; no assertion,
provider fake, fixture, test name, or parametrization was removed,
weakened, or changed; conftest.py untouched. Minor documentation cleanup:
the stale "xact-id entry proof" reference in the
test_notification_idempotency.py header was removed (no replacement
database-classification mechanism was added). Expected collection remains
242 (no test added or removed). The PostgreSQL suite has NOT run; Patch 9A
remains LOCAL VERIFICATION PENDING. 9B, 9C, and Patch 10 not started.

--------------------------------------------------------------------------------
# PATCH 9A — TENANT-SOURCE CORRECTION (v4), 2026-07-14
# STATUS: IMPLEMENTED — LOCAL VERIFICATION PENDING
--------------------------------------------------------------------------------
v3 passed the five previous static-blocker checks and the remaining static
package checks; one tenant-isolation defect remained: the SOURCE of
NotificationSnapshot.client_id. build_notification_snapshot set
client_id=appointment.client_id, which made a mismatched call (Office A's
appointment with Office B's client object and recipients) authenticate the
claim's tenant join with the appointment's OWN tenant id — defeating the
check and allowing Office A patient details to be sent to Office B's
recipients.

CORRECTION (production, one line + documentation):
build_notification_snapshot now sets client_id=client.id — always the id
of the SUPPLIED client whose notification configuration and recipients are
used. snapshot.appointment_id remains appointment.id. No second tenant id
was introduced, no pre-query was added, and the repository tenant join is
unchanged: the existing atomic claim (appointments.id = appointment_id AND
appointments.client_id = client_id) now genuinely enforces ownership. With
a mismatch: zero rows inserted, no provider called, no attempt row exists,
and reconciliation cannot lock or mutate the foreign appointment
(get_appointment_for_update returns None for the foreign tenant; the
mutation-free controlled-log path handles it).

SECURITY TEST ADDED:
test_service_mismatched_client_and_appointment_suppresses_all_channels —
Office A appointment + Office B client with distinct recipients, provider
traps, clean-session entry (test-owned snapshot/settings-read transaction
ended before invocation): zero SMS/email calls, zero attempt rows, Office
A's appointment field-for-field unchanged (flags, patient_sms_sent False,
notify_error, status, patient fields, start), empty NotificationOutcome
with no raw detail, normal return, and a direct assertion that
build_notification_snapshot(office_b, office_a_appointment, settings)
.client_id == office_b.id. Existing repository-level tenant tests are
untouched.

COUNT (parametrization-aware AST, actual result): idempotency file 44
functions -> 48 collected (4 parametrized x2); migration 5; baseline 190.
EXPECTED COLLECTION: 243.

FILES CHANGED IN v4: app/services/notification_service.py,
calendar_tests/test_notification_idempotency.py, CHANGE_REPORT.md,
delivery artifacts — nothing else. PostgreSQL verification had not begun
and is not claimed; Patch 9A remains LOCAL VERIFICATION PENDING. 9B, 9C,
and Patch 10 not started.

--------------------------------------------------------------------------------
# PATCH 9A — LOCAL RUN 1 + TEST-HARNESS CORRECTION (v5), 2026-07-14
# STATUS: IMPLEMENTED — LOCAL VERIFICATION PENDING (rerun required)
--------------------------------------------------------------------------------
FIRST POSTGRESQL RUN (PostgreSQL 16, Kevin's environment): 242 passed /
1 failed (14.76s). Failed:
test_booking_survives_total_claim_persistence_failure.

ROOT CAUSE (test-owned, not production): after finalize_booking's commit,
the test read result.appointment.id — an EXPIRED ORM attribute — which
autobegan a read transaction. send_booking_notifications then CORRECTLY
detected db.in_transaction() and abstained with entry_contract_violation,
so the intentionally failing claim fake was never reached and the expected
two claim_record_failed events never occurred. The separate
production-clean-session test passed in the same run: strict production
entry behavior was correct and is UNCHANGED.

CORRECTION (test-only): the appointment identity is now read WITHOUT SQL
through SQLAlchemy inspection —
    appointment_id = sa_inspect(result.appointment).identity[0]
    assert appointment_id is not None
    assert not db.in_transaction()
— an identity-map lookup that issues no SQL and begins no transaction.
Deliberately NO db.rollback() was added at that point: the test proves the
exact production post-finalize invocation contract and must not repair an
accidentally opened transaction after the fact. No other assertion was
modified; no production file, repository, model, migration, policy test,
migration-schema test, or conftest was touched.

COUNT: unchanged — 44 functions -> 48 collected in the idempotency file;
EXPECTED COLLECTION remains 243. Patch 9A remains LOCAL VERIFICATION
PENDING until the corrected package is rerun (previously failing test
first, then the complete suite; expected 243 passed — not claimed until
observed). 9B, 9C, and Patch 10 remain unstarted.

================================================================================
# PATCH 9A — VERIFIED LOCALLY / CLOSED 2026-07-14
================================================================================

FINAL VERIFICATION RECORD

PostgreSQL 16 local verification history:

- First complete run: 242 passed / 1 failed.
- The single failure was caused by test-owned expired ORM attribute access
  opening a read transaction after finalize_booking committed.
- Patch 9A's strict production entry contract behaved correctly and remained
  unchanged.
- The correction was test-only and used SQLAlchemy inspection to read the
  appointment identity without issuing SQL or opening a transaction.
- Corrected focused test: 1 passed in 1.14s.
- Corrected complete PostgreSQL 16 suite:
  243 passed in 15.58s.
- Final result:
  243 collected / 243 passed / 0 failed / 0 skipped / 0 errors.

PATCH 9A IS OFFICIALLY VERIFIED AND CLOSED.

VERIFIED CHECKPOINT:

C:\Users\kalva\Desktop\ai-dental-chatbot\backend-calendar-patch9a-test

PRODUCTION STATUS:

- Patch 9A has not been deployed to production.
- Migration 006 has not been applied to production.
- The documented no-overlap deployment cutover remains required before the
  Patch 9A notification guarantee begins.

NEXT-PHASE STATUS:

- Patch 9B has not started.
- Patch 9C has not started.
- Patch 10 has not started.
