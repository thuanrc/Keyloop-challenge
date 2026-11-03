"""Booking domain: errors, pure scheduling rules, and the transactional booking service."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


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
