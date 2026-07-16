# Mia Calendar MVP

Controlled-slot booking for Mia. Start here:

1. `CHANGE_REPORT.md` — what was built, decisions, risks, rollback.
2. `docs/INTEGRATION.md` — the exact small edits to models.py / main.py / chat.py.
3. `migrations/001_calendar_mvp_up.sql` — run in Supabase SQL editor.
4. Tests:
   - Pure (no deps):   `python3 calendar_tests/test_appointment_intent.py`
                       `python3 calendar_tests/test_availability_rules.py`
   - Database (throwaway Postgres, never production):
     `TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/postgres pytest calendar_tests/test_booking_db.py -v`

Booking is OFF for every office until you set `settings.calendar.booking_enabled = true`.
