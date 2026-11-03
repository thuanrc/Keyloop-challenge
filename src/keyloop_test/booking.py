"""Booking domain: errors, pure scheduling rules, and the transactional booking service."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import any_, exists, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from keyloop_test.models import (
    Appointment, Dealership, IdempotencyKey, ServiceBay, ServiceType, Technician, Vehicle,
)

MAX_ATTEMPTS = 3


class BookingError(Exception):
    code = "booking_error"
    status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(BookingError):
    code, status = "not_found", 404


class StartInPast(BookingError):
    code, status = "start_in_past", 422


class OutsideBusinessHours(BookingError):
    code, status = "outside_business_hours", 422


class NoTechnicianAvailable(BookingError):
    code, status = "no_technician_available", 409


class NoBayAvailable(BookingError):
    code, status = "no_bay_available", 409


class SlotTaken(BookingError):
    code, status = "slot_taken", 409


class IdempotencyKeyReused(BookingError):
    code, status = "idempotency_key_reused", 422


def compute_end(start_at: datetime, duration_minutes: int) -> datetime:
    return start_at + timedelta(minutes=duration_minutes)


def check_not_past(start_at: datetime, now: datetime) -> None:
    if start_at <= now:
        raise StartInPast("Appointments must start in the future")


def check_business_hours(
    start_at: datetime, end_at: datetime, tz: str, opens_at: time, closes_at: time
) -> None:
    zone = ZoneInfo(tz)
    local_start, local_end = start_at.astimezone(zone), end_at.astimezone(zone)
    if not (
        local_start.date() == local_end.date()
        and opens_at <= local_start.time()
        and local_end.time() <= closes_at
    ):
        raise OutsideBusinessHours(
            f"Appointment {local_start:%Y-%m-%d %H:%M}–{local_end:%H:%M} ({tz}) is outside "
            f"business hours {opens_at:%H:%M}–{closes_at:%H:%M}"
        )


@dataclass(frozen=True)
class BookingRequest:
    vehicle_id: int
    service_type_id: int
    dealership_id: int
    start_at: datetime


def _get(session: Session, model, id_: int):
    row = session.get(model, id_)
    if row is None:
        raise NotFound(f"{model.__name__} {id_} not found")
    return row


def _busy(fk_col, resource_id, start_at: datetime, end_at: datetime):
    """An appointment on this resource overlapping [start_at, end_at)."""
    return exists().where(fk_col == resource_id, Appointment.start_at < end_at,
                          Appointment.end_at > start_at)


def book_appointment(
    session: Session,
    req: BookingRequest,
    now: datetime | None = None,
    *,
    on_retry: Callable[[], None] | None = None,
    on_booked: Callable[[Appointment], None] | None = None,
) -> Appointment:
    try:
        vehicle = _get(session, Vehicle, req.vehicle_id)
        service_type = _get(session, ServiceType, req.service_type_id)
        dealership = _get(session, Dealership, req.dealership_id)

        end_at = compute_end(req.start_at, service_type.duration_minutes)
        check_not_past(req.start_at, now or datetime.now(UTC))
        check_business_hours(req.start_at, end_at, dealership.timezone,
                             dealership.opens_at, dealership.closes_at)

        for _ in range(MAX_ATTEMPTS):
            technician = session.scalars(
                select(Technician).where(
                    Technician.dealership_id == req.dealership_id,
                    literal(service_type.required_skill) == any_(Technician.skills),
                    ~_busy(Appointment.technician_id, Technician.id, req.start_at, end_at),
                ).order_by(Technician.id).limit(1)
            ).first()
            if technician is None:
                raise NoTechnicianAvailable(
                    f"No technician with skill '{service_type.required_skill}' is free "
                    f"{req.start_at:%Y-%m-%d %H:%M}–{end_at:%H:%M} UTC")
            bay = session.scalars(
                select(ServiceBay).where(
                    ServiceBay.dealership_id == req.dealership_id,
                    ~_busy(Appointment.service_bay_id, ServiceBay.id, req.start_at, end_at),
                ).order_by(ServiceBay.id).limit(1)
            ).first()
            if bay is None:
                raise NoBayAvailable(
                    f"No service bay is free {req.start_at:%Y-%m-%d %H:%M}–{end_at:%H:%M} UTC")

            appointment = Appointment(
                dealership_id=req.dealership_id, customer_id=vehicle.customer_id,
                vehicle_id=vehicle.id, service_type_id=service_type.id,
                technician_id=technician.id, service_bay_id=bay.id,
                start_at=req.start_at, end_at=end_at,
            )
            try:
                with session.begin_nested():
                    session.add(appointment)
                    session.flush()
            except IntegrityError as e:
                if getattr(e.orig, "sqlstate", None) != "23P01":
                    raise
                if on_retry:
                    on_retry()  # lost a race to a concurrent booking; re-query
                continue
            if on_booked:
                on_booked(appointment)  # runs inside the booking transaction, before commit
            session.commit()
            return appointment
        raise SlotTaken("Resources were taken by a concurrent booking; please retry")
    except BookingError:
        session.rollback()
        raise


def book_idempotently(
    session: Session,
    req: BookingRequest,
    key: str,
    request_hash: str,
    now: datetime | None = None,
    *,
    on_retry: Callable[[], None] | None = None,
) -> tuple[Appointment, bool]:
    """Book once per key. Returns (appointment, replayed)."""
    # Claim the key in the booking's transaction. A concurrent request with the same key blocks
    # on the primary key until this one commits (then replays) or rolls back (then books).
    claimed = session.execute(
        insert(IdempotencyKey).values(key=key, request_hash=request_hash)
        .on_conflict_do_nothing().returning(IdempotencyKey.key)
    ).first()
    if claimed is None:
        existing = session.get(IdempotencyKey, key)
        session.rollback()
        if existing.request_hash != request_hash:
            raise IdempotencyKeyReused(
                "Idempotency-Key was already used with a different request body")
        return session.get(Appointment, existing.appointment_id), True

    def link(appointment: Appointment) -> None:
        session.execute(update(IdempotencyKey).where(IdempotencyKey.key == key)
                        .values(appointment_id=appointment.id))

    # a failed booking rolls back the claim too, so the client can retry with the same key
    return book_appointment(session, req, now, on_retry=on_retry, on_booked=link), False
