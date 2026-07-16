# app/repositories/appointment_repository.py
#
# OWNER OF: every read and write against appointment_slots and appointments.
#
# Rule 15 (Database Discipline):
#   - No other module may issue queries against these tables.
#   - EVERY function here takes client_id and filters by it. One office can
#     never see or touch another office's calendar.
#   - Row locking (SELECT ... FOR UPDATE) is exposed via *_for_update
#     functions so the hold/booking services can build safe transactions.
#
# Transaction ownership: this layer does NOT commit. The calling service owns
# the transaction boundary (begin/commit/rollback), because a booking is a
# multi-step unit of work. The one exception is create_* helpers used by the
# admin route, which flush (to get IDs) but still leave commit to the caller.

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.calendar_models import (
    Appointment,
    AppointmentSlot,
    AppointmentStatus,
    SlotStatus,
)


def create_slot(
    db: Session,
    client_id: uuid.UUID,
    start_datetime: datetime,
    end_datetime: datetime,
    provider_name: Optional[str] = None,
    service_key: Optional[str] = None,
) -> AppointmentSlot:
    """
    Purpose: Insert one staff-published bookable slot.
    Inputs:  aware UTC datetimes; optional display provider / service key.
    Returns: the new AppointmentSlot (id populated via flush).
    Database effects: INSERT into appointment_slots (uncommitted).
    Possible failures: raises ValueError on end <= start (caller shows the
        staff member a clear error instead of storing a nonsense slot).
    """
    if end_datetime <= start_datetime:
        raise ValueError("Slot end_datetime must be after start_datetime.")
    slot = AppointmentSlot(
        client_id=client_id,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        provider_name=provider_name,
        service_key=service_key,
        status=SlotStatus.AVAILABLE,
    )
    db.add(slot)
    db.flush()  # Assigns slot.id so the admin route can return it.
    return slot


def list_slots_between(
    db: Session,
    client_id: uuid.UUID,
    start_utc: datetime,
    end_utc: datetime,
) -> List[AppointmentSlot]:
    """
    Purpose: Read all slots (any status) for one client in a UTC window.
             Availability filtering happens in availability_service — this
             function deliberately returns raw rows so filtering logic stays
             in one testable place.
    Database effects: SELECT only.
    Possible failures: none beyond database errors (propagated, not hidden).
    """
    return (
        db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.client_id == client_id,
            AppointmentSlot.start_datetime >= start_utc,
            AppointmentSlot.start_datetime < end_utc,
        )
        .order_by(AppointmentSlot.start_datetime.asc())
        .all()
    )


def get_slots_by_ids(
    db: Session,
    client_id: uuid.UUID,
    slot_ids: List[uuid.UUID],
) -> List[AppointmentSlot]:
    """
    Purpose: Re-read the exact slots that were offered to a patient, so slot
             selection matches against CURRENT rows (staff may have blocked
             one since it was displayed).
    Returns: matching rows for THIS client only; unknown ids simply drop out.
    Database effects: SELECT only.
    """
    if not slot_ids:
        return []
    return (
        db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.client_id == client_id,
            AppointmentSlot.id.in_(slot_ids),
        )
        .all()
    )


def get_slot_for_update(
    db: Session,
    client_id: uuid.UUID,
    slot_id: uuid.UUID,
) -> Optional[AppointmentSlot]:
    """
    Purpose: Load one slot WITH a row lock, so concurrent hold/booking
             attempts for the same slot are serialized by the database.
             This is the core double-booking defense (Rule 15: the final
             booking must recheck availability under a lock).
    Returns: the locked slot, or None if it doesn't exist FOR THIS CLIENT.
    Database effects: SELECT ... FOR UPDATE (lock released at commit/rollback).
    Note: SQLite (used by the local test suite) ignores FOR UPDATE; its
          whole-database write lock provides equivalent serialization there.
    """
    return (
        db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.client_id == client_id,
            AppointmentSlot.id == slot_id,
        )
        .with_for_update()
        .first()
    )


def get_appointment_by_conversation(
    db: Session,
    client_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> Optional[Appointment]:
    """
    Purpose: Find an existing non-cancelled appointment created by this
             conversation. Used to stop one conversation from booking twice
             (Rule 10: "Can it create duplicate database records?").
    Database effects: SELECT only.
    """
    return (
        db.query(Appointment)
        .filter(
            Appointment.client_id == client_id,
            Appointment.conversation_id == conversation_id,
            Appointment.status != AppointmentStatus.CANCELLED,
        )
        .first()
    )


def create_appointment_from_slot(
    db: Session,
    slot: AppointmentSlot,
    conversation_id: Optional[uuid.UUID],
    patient_name: str,
    patient_phone: str,
    patient_email: Optional[str],
    new_or_returning: Optional[str],
    reason: Optional[str],
    urgency: str,
    status: str,
    source: str = "mia_widget",
) -> Appointment:
    """
    Purpose: Insert the appointment row for an already-locked, verified slot.
             Callers (booking_service ONLY) must hold the slot's row lock and
             have re-verified its hold state before calling.
    Returns: the new Appointment (id populated via flush).
    Database effects: INSERT into appointments (uncommitted).
    Possible failures: raises ValueError on missing name/phone or invalid
        status — these indicate a bug upstream, and hiding them would create
        unreachable appointment rows (Rule 16).
    """
    if not (patient_name or "").strip() or not (patient_phone or "").strip():
        raise ValueError("Appointment requires patient_name and patient_phone.")
    if status not in AppointmentStatus.ALL:
        raise ValueError(f"Invalid appointment status: {status!r}")

    appointment = Appointment(
        client_id=slot.client_id,
        slot_id=slot.id,
        conversation_id=conversation_id,
        patient_name=patient_name.strip(),
        patient_phone=patient_phone.strip(),
        patient_email=(patient_email or "").strip() or None,
        new_or_returning=new_or_returning,
        reason=reason,
        urgency=urgency or "routine",
        start_datetime=slot.start_datetime,  # Copied on purpose; see model comment.
        end_datetime=slot.end_datetime,
        status=status,
        source=source,
    )
    db.add(appointment)
    db.flush()
    return appointment


def list_appointments_between(
    db: Session,
    client_id: uuid.UUID,
    start_utc: datetime,
    end_utc: datetime,
) -> List[Appointment]:
    """Purpose: admin view of appointments in a window. SELECT only."""
    return (
        db.query(Appointment)
        .filter(
            Appointment.client_id == client_id,
            Appointment.start_datetime >= start_utc,
            Appointment.start_datetime < end_utc,
        )
        .order_by(Appointment.start_datetime.asc())
        .all()
    )


def get_appointment_for_update(
    db: Session,
    client_id: uuid.UUID,
    appointment_id: uuid.UUID,
) -> Optional[Appointment]:
    """
    Purpose: Load one appointment with a row lock for cancellation, so a
             cancel and any concurrent change are serialized.
    Database effects: SELECT ... FOR UPDATE.
    """
    return (
        db.query(Appointment)
        .filter(
            Appointment.client_id == client_id,
            Appointment.id == appointment_id,
        )
        .with_for_update()
        .first()
    )
