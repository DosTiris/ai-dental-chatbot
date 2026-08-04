# Demo Dental — Controlled Production Validation Closure

Repository: `C:\Users\kalva\Desktop\ai-dental-chatbot\backend-checkpointb-main-integration`  
Branch: `main`  
Authoritative implementation commit at activation: `000d3d471caa30130e508a10d053df62100d7507`

## Provenance

This closure combines two evidence classes:

1. **Machine-generated production-operation evidence** from the reviewed dry-run and guarded-apply artifacts.
2. **Owner-observed end-to-end validation evidence** from Kevin's controlled production booking test.

The documentation drafter did not directly operate Kevin's local repository or production user interface. Commit and push authority remain with the owner.

## Scope

Controlled production validation of the Demo Dental Calendar activation:

- one approved production availability batch for August 5–6, 2026; and
- one controlled live booking through the patient-facing Mia flow.

## Availability activation evidence

1. The authenticated, non-mutating dry run resolved the pinned Demo Dental tenant and reported `READY`.
2. The guarded apply completed with exit code `0`.
3. Exactly 32 thirty-minute slots were created.
4. The schedule covered Wednesday, August 5 and Thursday, August 6, 2026.
5. Approved local starts were 09:00 through 16:30 on each day in `America/New_York`.
6. The API returned HTTP `200` with 32 unique slot IDs.
7. Post-apply discovery found exactly the same 32 rows.
8. Every created row had the approved start time, approved end time, and `available` status.
9. No duplicate starts, unexpected rows, holds, or appointments were present immediately after the batch apply.
10. A rollback manifest was sealed for exactly the 32 created slot IDs.
11. The repository remained on branch `main`, with HEAD and `origin/main` both pinned to `000d3d471caa30130e508a10d053df62100d7507`; the working tree remained clean and the index empty.

## Controlled patient-facing booking validation

The owner then completed one controlled production booking for Wednesday, August 5, 2026 at 09:30 `America/New_York`.

Owner-observed results:

1. Mia displayed the correct patient-facing date and local time.
2. The selected 09:30 local slot mapped to `2026-08-05T13:30:00+00:00`.
3. Exactly one appointment/request row was created.
4. The selected slot transitioned from `available` to `booked`.
5. Exactly one office email was delivered.
6. Exactly one office SMS was delivered.
7. No patient SMS was sent.
8. The universal request-received confirmation wording appeared correctly.

## Patient-SMS source reconciliation

The stale project snapshot previously reviewed by the drafting assistant was not authoritative for the live activation.

The exact committed source at `000d3d471caa30130e508a10d053df62100d7507` showed:

- `build_patient_sms()` exists only as a dormant builder;
- no production caller uses that builder;
- `send_booking_notifications()` sends only the approved office channels:
  - office SMS; and
  - office email;
- no provider send targets the patient; and
- the controlled production test independently confirmed that no patient SMS was sent.

Therefore, there is no unresolved contradiction requiring this closure to remain open.

## Final status

**Demo Dental's controlled Calendar availability activation and one end-to-end production booking validation are formally complete.**

## Change boundary

This closure update is documentation only. It introduces no executable-code change, schema change, configuration change, credential change, deployment change, or additional production mutation.
