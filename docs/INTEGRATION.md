# INTEGRATION GUIDE — wiring the calendar into existing Mia files

Three existing files need small, surgical additions. Every changed line
serves the approved feature (Rule 12); nothing else in those files changes.

> **DATABASE ROLLOUT ORDER (required).** The integrated Calendar code
> depends on schema that the migrations create. Before deploying the
> integrated code to production, apply — in order — `migrations/001`
> (calendar tables + booking columns), `migrations/002` (appointment
> partial unique indexes), `migrations/003` (conversation offer-expiration
> columns), and `migrations/004` (appointments.confirmed_at).
> **003 must be live before any code that writes the offer-expiration
> columns runs** (the offer lifecycle in `booking_conversation.py` writes
> them from Patch 2C onward), and **004 must be live before any Patch 4
> code deploys** — the ORM model, the confirm service, and the admin
> appointment views reference `confirmed_at`, so code-before-migration
> fails on the first appointment query. Deploying code before its
> migration is a hidden failure waiting to happen (Rule 16).

---

## 1. `app/models.py` — add booking-state columns to `Conversation`

The dialog state machine persists its state on the existing `conversations`
table (one conversation-state system — Rule 3). Add these five columns inside
`class Conversation`, right after the existing `booking_link_sent` line:

```python
    # -----------------------------
    # CALENDAR BOOKING STATE (Calendar MVP)
    # Owned exclusively by app/services/booking_conversation.py.
    # Valid booking_state values: see app/calendar_models.py BookingState.
    # -----------------------------
    booking_state = Column(String, nullable=False, server_default="none", default="none")
    booking_preferred_date = Column(String, nullable=True)      # ISO date "2026-07-16"
    booking_time_preference = Column(String, nullable=True)     # morning/afternoon/evening/any
    booking_offered_slot_ids = Column(JSONB, nullable=True)     # ["uuid", ...] in display order
    booking_selected_slot_id = Column(UUID(as_uuid=True), nullable=True)
```

`JSONB` and `UUID` are already imported at the top of models.py — no new
imports needed.

---

## 2. `app/main.py` — mount the admin route *(APPLIED in Patch 3)*

```python
from app.routes import calendar as calendar_routes
app.include_router(calendar_routes.router)
```

Also import the calendar models once at startup so SQLAlchemy knows the
tables (place next to the existing `from app.models import ...` import):

```python
import app.calendar_models  # noqa: F401  (registers calendar tables)
```

---

## 3. `app/routes/chat.py` — the integration contract *(APPLIED in Patch 3)*

Patch 3 implemented this section. What follows documents the contract as
built (the earlier draft block with its `booking_should_run` trigger and
"office has your details" fallback was superseded — that fallback could
claim office follow-up without checking that any notification succeeded,
which violates Rule 16, so it was never inserted).

### Ownership contract (one owner per message — Rule 3)

Resolved **fresh from current settings on every message**, never from
`booking_link_sent`:

1. `external_calendar` — iff `has_external_booking(client)` (an active
   booking URL). The single owner is `send_external_booking_handoff`.
2. `internal_calendar` — else iff strict `settings.calendar.booking_enabled`
   is `true`. The owners are `begin_booking_after_intake` (start) and
   `handle_booking_message` (continuation) in `booking_conversation.py`.
3. `lead_capture_only` — else. Behavior is byte-identical to pre-Patch-3.

Emergency-flagged conversations never book. Priority NON-emergency leads may
book; the appointment `urgency` stays `"priority"`.

### Where chat.py touches the Calendar (all applied)

- **Imports**: `BookingState` (state constants only) and the three
  `booking_conversation` entry points. chat.py never mutates a `booking_*`
  field itself; `booking_dialog_active()` is its only reader of
  `booking_state`.
- **Continuation hook** (before the Operational override, after every
  safety/emergency guard): while a dialog is active it delegates to
  `handle_booking_message(..., information_interruption=
  is_information_interruption(user_text))`. `handled=False` falls through to
  the existing flow (that is how hours/location/insurance/pricing/phone
  questions get answered mid-booking — the dialog state is left
  byte-unchanged and the next scheduling message resumes it). Medical-advice
  questions (`looks_like_medical_advice`) make the whole hook yield the same
  way: state and any held slot stay unchanged and the existing
  medical-advice guard answers — safety always wins.
- **Ownership transition**: if a dialog is active but the office NOW has an
  external URL, the hook cancels the internal dialog (`cancel_active_booking`
  — hold released first, state cleared) and the shared external owner
  answers the same request. If the cancellation FAILS, no handoff is sent
  and `booking_link_sent` is untouched (a handoff over live internal state
  would create two owners); the honest fallback answers and the next
  message retries the transition. A medical-advice message also defers the
  transition to the next appropriate message.
- **Fresh post-completion routing point** (immediately after the hook): a
  COMPLETED lead with no active dialog and no external URL may start a NEW
  internal dialog on a scheduling/date message — ownership is resolved
  fresh every time, `booking_link_sent` is not an ownership signal, and the
  duplicate-appointment defense in the Calendar start still applies.
- **Five completion call sites** invoke `route_completed_lead` immediately
  **after** their existing `mark_completed_and_notify_office` call runs
  unchanged: short-symptom completion, patient-type completion, priority
  time-window completion, the `lead_capture_complete` block, and the
  priority receptionist-bypass completion. `None` means "keep today's
  reply".
- **Guard gates**: the time-only outside-hours guard and the intake
  time-window capture guard are skipped while a dialog is active, so
  booking answers ("tomorrow", "morning", "2") reach the state machine and
  `lead_time_window` is never overwritten mid-booking.
- **Conversation-ending guard**: at `WAITING_FOR_CONFIRMATION` only, the
  normalized replies `no` / `no thanks` / `no thank you` bypass the ending
  guard and reach the Calendar rejection path. Every genuine ending during a
  dialog calls `cancel_active_booking` and then keeps Mia's existing ending
  reply unchanged.
- **Emergency cleanup**: the dangerous-dental (true-emergency), urgent-
  trauma, and emergency-routing guards cancel an active dialog in the SAME
  request. A cleanup failure is logged and rolled back; the emergency reply
  is always still returned (never a 500, never a false success claim).

### Notification behavior (approved temporary MVP)

The completed-lead office notification runs **first**, then the Calendar
dialog begins; a later successful booking sends the separate booking
notification from `notification_service`. Two office messages for one
booked patient is the accepted temporary cost — an abandoned booking dialog
can never become a lost lead. Deduplication/outbox work stays out of scope
(Senior Audit Recommended #1).

### Honest failure fallback (Rule 16)

If Calendar delegation raises, chat.py logs the real error, rolls back, and
consults `finalize_and_notify_if_ready` (per-channel idempotent — a channel
whose sent flag is already `True` is NEVER re-sent):

- at least one office channel recorded success →
  `"I'm sorry, I couldn't open the booking calendar right now. The office
  has your request and will follow up."`
- no channel succeeded → the reply directs the patient to the office phone
  and makes **no** claim that the office received anything.

### Post-link external ownership

Once `booking_link_sent` is `True`, a later message gets the truthful
acknowledgment `"The online booking link is still available below."` (with
the existing booking button/meta, mode `external_booking_link_reminder`)
**only when that message itself expresses scheduling or service-selection
intent** — the stored `lead_reason` never hijacks unrelated messages, which
keep flowing to the normal paths (hours, FAQ, and so on). No second "first"
handoff is ever sent and no internal dialog starts while the URL is
active.

---

## 4. Enable booking for one office (opt-in — Rule 4, no hidden behavior)

In Supabase, edit that client's `settings` JSONB:

```json
{
  "timezone": "America/New_York",
  "calendar": {
    "booking_enabled": true,
    "hold_minutes": 5,
    "minimum_notice_minutes": 60,
    "max_offered_slots": 3,
    "max_booking_days": 30,
    "require_staff_confirmation": true
  }
}
```

Every office without `"booking_enabled": true` keeps today's behavior bit-for-bit.

---

## 5. Notification policy (current MVP — Patch 2D, Senior Audit Critical #3)

Calendar booking notifications currently go to **authorized dental-office
staff only**. The supported channels are:

- **Office SMS** — to the client's `notification_phone`
- **Office email** — to the client's `notification_email`

**Patient SMS is disabled.** Mia collects the patient's phone number so the
office can follow up; collecting a phone number does **not** itself authorize
automated text messages to the patient, and the booking flow never sends
one. `send_booking_notifications` never uses `appointment.patient_phone` as
a destination, `appointment.patient_sms_sent` is always `False` in the
current MVP, and the intentional disablement is **not** recorded as a
failure in `notify_error`.

A future patient-SMS feature requires a separately approved, consent-enabled
build: explicit patient SMS consent storage (timestamp and source), widget
consent wording, STOP/HELP handling, and messaging-provider configuration
that accurately covers patient appointment notifications, plus tests proving
no patient text is sent without consent. Until then, the
`build_patient_sms` formatter in `notification_service.py` is retained as
documented FUTURE-ONLY architecture with no production call site.

### Output hardening (Patch 6 — Senior Audit Recommended #7)

`notification_service.py` is the single owner of four cross-cutting
notification-output concerns, applied to all four staff outputs (calendar
office email, calendar office SMS, lead office email, lead office SMS):

- **Plain-text field normalization** — `normalize_notification_field`
  flattens control characters (0–31, 127, 128–159) to spaces, collapses
  whitespace, strips, and bounds each untrusted value at the output
  boundary (name 120, phone 32, email 254, free text 300, enums 16,
  source 32, practice name 120; complete email subject 160, truncation as
  limit − 1 characters plus `…`). Stored business values are never
  modified. Template structure (newlines, labels, the lead-SMS `" | "`
  separators, wording, field order) is unchanged.
- **HTML escaping exactly once, at the email boundary only** —
  `render_email_html` applies `html.escape(..., quote=True)` to the plain
  body text and places it inside the fixed `<pre>` wrapper. Nothing is
  HTML-escaped before database storage, in ordinary JSON API business
  values, or in SMS text.
- **Fixed stored-error vocabulary** — `appointments.notify_error` only ever
  contains `office_sms: send_failed`, `office_email: send_failed`,
  `office_sms: skipped (no notification_phone configured)`,
  `office_email: skipped (no notification_email configured)`, or an
  SMS-then-email pair joined with `"; "` (max valid length 112). The admin
  `AppointmentView` returns any stored value outside this grammar (e.g.
  legacy raw provider text) as `notification_error: detail_withheld` —
  stored data is never rewritten.
- **Safe logging** — server logs for the covered notification and Calendar
  error paths carry only fixed event names, channels, fixed codes,
  sanitized exception class names, configuration booleans, and UUIDs.
  Never `str(exc)`/`repr(exc)`, tracebacks, patient values, message
  bodies, provider URLs, headers, credentials, or SQL parameters.

Patient-facing surfaces: the Calendar booking reply meta no longer contains
`notify_errors`; the completed-lead meta keys `lead_email_error` /
`lead_sms_error` are `None` on success and exactly `send_failed` on
provider failure — raw provider details never enter `ChatResponse`.

---

## 6. Publish slots (example)

> **Patch 5:** `$CALENDAR_ADMIN_KEY` is the office's own per-tenant Calendar
> admin key (see §8). The global `ADMIN_API_KEY` **no longer works on any
> `/admin/calendar/*` route** (401) — it still works on the non-calendar
> `/admin` routes, which are unchanged.

```bash
curl -X POST https://YOUR_HOST/admin/calendar/slots \
  -H "X-Admin-Key: $CALENDAR_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{
    "client_id": "YOUR-CLIENT-UUID",
    "slots": [
      {"start_datetime": "2026-07-16T10:00:00-04:00", "end_datetime": "2026-07-16T10:45:00-04:00"},
      {"start_datetime": "2026-07-16T13:30:00-04:00", "end_datetime": "2026-07-16T14:15:00-04:00"},
      {"start_datetime": "2026-07-16T15:45:00-04:00", "end_datetime": "2026-07-16T16:30:00-04:00"}
    ]
  }'
```

---

## 7. Staff confirmation (Patch 4 — Senior Audit Critical #4)

When an office has `require_staff_confirmation: true` (the safe default),
Mia saves appointments as **pending** and the office SMS says
"NEEDS CONFIRMATION". The supported way to confirm is:

```bash
curl -X POST "https://YOUR_HOST/admin/calendar/appointments/APPOINTMENT-UUID/confirm?client_id=YOUR-CLIENT-UUID" \
  -H "X-Admin-Key: $CALENDAR_ADMIN_KEY"
```

Behavior:

- **pending → confirmed** — the only transition this endpoint performs. The
  appointment row is locked (`SELECT ... FOR UPDATE`), re-checked, updated,
  and committed once.
- **200** for a fresh confirmation **and** for re-confirming an
  already-confirmed appointment (idempotent success — repeat clicks and
  retries have no duplicate effects).
- **404** `Appointment not found.` for unknown ids **and** for another
  office's ids — deliberately indistinguishable (tenant isolation; same
  wording as cancel).
- **409** `Appointment is cancelled and cannot be confirmed.` /
  `Appointment is completed and cannot be confirmed.` /
  `Appointment is no_show and cannot be confirmed.` — finished or dead
  appointments cannot be confirmed. Controlled terminal statuses are
  returned in the detail **unchanged**.
- **409** `Appointment is unsupported and cannot be confirmed.` — a stored
  status outside the controlled vocabulary (a malformed, legacy, manually
  edited, or mixed-version row; the status column has no database CHECK
  constraint) is also rejected mutation-free (Patch 8, mirroring the cancel
  endpoint). An uncontrolled stored value is represented externally
  **only** as `unsupported`: the raw stored value is never returned, it is
  never repaired or rewritten, and no migration or data cleanup occurs. The
  409 detail therefore always carries only a controlled word — a member of
  the status vocabulary, or the fixed `unsupported` sentinel.
- **No notifications are sent.** Authorized office staff are performing the
  action, so no additional office SMS/email is triggered, and patient
  messaging remains disabled per the §5 policy. Notification flags and
  `notify_error` on the appointment are untouched.

### `confirmed_at` semantics

`AppointmentView` (the confirm response and the appointment list) exposes a
nullable `confirmed_at`:

- It records the UTC instant of the **first successful staff
  pending → confirmed action** — nothing else sets it.
- `null` means "never staff-confirmed". In particular, appointments created
  directly as `confirmed` because `require_staff_confirmation` is `false`
  keep `confirmed_at = null` on purpose.
- Re-confirming preserves the original value byte-for-byte, and a later
  cancellation keeps it on the cancelled row for the audit trail.

### Staff cancellation lifecycle (Patch 7 — Senior Audit Recommended #6)

```bash
curl -X POST "https://YOUR_HOST/admin/calendar/appointments/APPOINTMENT-UUID/cancel?client_id=YOUR-CLIENT-UUID" \
  -H "X-Admin-Key: $CALENDAR_ADMIN_KEY"
```

Cancellation follows an explicit allow-list — **only `pending` and
`confirmed` appointments can be cancelled**:

- **pending → cancelled** and **confirmed → cancelled** — **200**. The
  appointment row is locked, re-checked, updated, and its booked slot is
  released back to `available` (hold fields cleared) in the **same
  transaction**. `confirmed_at` is preserved on the cancelled row for the
  audit trail.
- **409** `Appointment is already cancelled.` — repeating a cancellation is
  mutation-free: nothing is rewritten, the slot is untouched, and no side
  effect repeats.
- **409** `Appointment is completed and cannot be cancelled.` /
  `Appointment is no_show and cannot be cancelled.` — finished appointments
  are terminal: the record is never rewritten and the historical slot is
  **never reopened**. Any status outside the allow-list (including future
  ones) is rejected by default.
- **409** `Appointment is unsupported and cannot be cancelled.` — a stored
  status outside the controlled vocabulary (a malformed, legacy, manually
  edited, or mixed-version row; the status column has no database CHECK
  constraint) is also rejected mutation-free. An uncontrolled stored value
  is represented externally **only** as `unsupported`: the raw stored
  value is never returned, and it is never repaired or rewritten. The 409
  detail therefore always carries only a controlled word — a member of the
  status vocabulary, or the fixed `unsupported` sentinel.
- **404** `Appointment not found.` for unknown ids **and** for another
  office's ids — deliberately indistinguishable (tenant isolation; same
  wording as confirm).
- **No notifications are sent** on any cancellation path (success or
  rejection), and patient messaging remains disabled per the §5 policy.

---

## 8. Per-office Calendar admin credentials (Patch 5 — Senior Audit Critical #2)

Every `/admin/calendar/*` route authenticates a **per-office credential**.
The credential — not the request — determines which office is being managed:
the request's `client_id` must equal the authenticated office's id, or the
route answers `404 Client not found.` exactly as if the id did not exist.
The global `ADMIN_API_KEY` has **no** Calendar access and there is **no**
fallback of any kind.

### Key format and storage

- Raw key: `mia_cal_` + 43 URL-safe characters (`secrets.token_urlsafe(32)`).
- The database stores **only** the SHA-256 digest (64 lowercase hex chars)
  in `calendar_admin_credentials.key_hash`. Raw keys are shown once at
  provisioning and never persisted, logged, or committed anywhere.
- A CHECK constraint rejects raw-key-shaped values, so accidentally
  inserting the secret instead of the hash fails loudly.

### Provisioning (operator procedure — placeholders only)

Generate a pair (run anywhere with the app code; nothing is written):

```bash
python -c "from app.services.calendar_admin_auth import generate_calendar_admin_key; \
raw, digest = generate_calendar_admin_key(); \
print('RAW (configure in the tool, then close this terminal):', raw); \
print('HASH (store in the database):', digest)"
```

Store **only the hash**:

```sql
INSERT INTO calendar_admin_credentials (client_id, key_hash, label)
VALUES ('YOUR-CLIENT-UUID', 'PASTE-64-HEX-HASH-HERE', 'front-desk tool');
```

Place the raw key in the intended staff tool's secret storage. Do not email
it, do not screenshot it, do not paste it into tickets.

### Rotation and revocation

Multiple active credentials per office are allowed **by design**:

1. Provision the new credential (both keys now work — the overlap window).
2. Switch the staff tool to the new raw key and verify.
3. Revoke the old one:

```sql
UPDATE calendar_admin_credentials
SET active = false, revoked_at = now()
WHERE client_id = 'YOUR-CLIENT-UUID' AND label = 'front-desk tool';
```

Revocation is effective on the next request. Deactivating the office itself
(`clients.active = false`) kills all of its Calendar credentials at once.

### Safe cutover (approved rollout order)

1. Apply migration 005 (after 001–004).
2. Provision credentials; store only their hashes.
3. Securely configure the raw keys in the intended tools.
4. Verify the entire cutover in **staging** first.
5. Deploy Patch 5 production code during a controlled cutover.
6. Immediately test: own-tenant success (200) · cross-tenant `client_id`
   (404) · old global key on a Calendar route (401) · non-calendar `/admin`
   still accepts the global key.
7. If provisioned credentials fail, immediately revert to Patch 4 code —
   it never reads the credential table, so global-key access resumes at
   once; migration 005 can stay applied or be rolled back afterward with
   `005_calendar_admin_credentials_down.sql`.

## 9. Notification duplicate suppression (Patch 9A — Recommended #1)

Patch 9A makes office-notification duplicate suppression a **database
invariant**. Every provider execution is preceded by an atomic per-channel
claim into the `notification_attempts` ledger (migration 006; unique per
appointment/channel), and each outcome commits atomically with the
recomputed appointment projection. The ledger is honest: `sent` means only
that the provider API call returned successfully — never that anything was
delivered or opened; a caught provider error is recorded as `unknown`,
never as definite non-delivery. Patch 9A has **no retry, no recovery, no
worker, no cron** — a permanent `sending` row after a crash is an honest
unresolved state until Patch 9B.

### The caller contract (documented and tested)

The only production caller is `_finalize_and_reply` in
`booking_conversation.py`, which invokes `send_booking_notifications`
**immediately after `finalize_booking` commits and before any conversation
mutation**. The service verifies this at entry, strictly: the session must
carry no pending identity-map state (`new`/`dirty`/`deleted` empty) **and
no open transaction of any kind** (`in_transaction()` False). Any
violation causes a safe abstention — nothing is sent, nothing is claimed,
nothing is rolled back, all caller-owned state is preserved, and one
controlled event is logged. The service never queries the database to
classify an open transaction. Do **not** add callers that invoke this
service while holding an open transaction or pending session state; end
your own read work (commit or rollback) before the call, exactly as the
production path does.

### Legacy appointments (approved Option B — no backfill)

Migration 006 writes no data. Pre-006 appointments are protected at
runtime, atomically inside the claim SQL: a true sent flag or a legacy
`send_failed` entry blocks the channel's claim; a malformed legacy
`notify_error` blocks both no-row channels and is never rewritten, echoed,
or recomposed (the admin API keeps returning the fixed withheld marker).

### Mixed-version deployment cutover (REQUIRED — no overlap)

Overlapping old (pre-9A) and new notification executors are **not
duplicate-safe**: pre-9A code neither claims nor consults the ledger, so
one appointment could be notified by both. The at-most-once guarantee
begins **only after this cutover is complete**:

1. Apply migration 006 (after 001–005; each patch's code never deploys
   before its migration).
2. Prevent new booking-notification execution (pause booking traffic or
   hold deploys at the load balancer).
3. Stop or fully drain **every** pre-9A application instance.
4. Deploy the Patch 9A application code.
5. Verify **no pre-9A instance remains active** (instance list + version
   endpoint/build tag).
6. Resume traffic.
7. Run one controlled staging booking end to end.
8. Explicitly invoke notification execution **again** for that same
   appointment.
9. Prove exactly one provider execution per configured channel (provider
   dashboards + a single `sent` ledger row per channel).

**Exact guarantee after cutover:** at most one post-cutover 9A provider
execution per appointment/channel; zero 9A executions for legacy-protected
channels. Pre-006 execution history is outside the guarantee; mixed-code
overlap is outside the guarantee and must be prevented by this procedure.

### Rollback

Revert (or stop) the Patch 9A application code **first** — Patch 8 code
never references the ledger, so pre-9A notification behavior resumes
immediately. Then, if desired, run
`006_notification_attempts_down.sql` (drops the ledger and its history;
the appointments' own projection columns are untouched and remain the
staff-visible record). Re-applying 006 later starts with an empty ledger;
already-notified appointments stay protected by their projection flags.
