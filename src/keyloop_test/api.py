import logging
from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

import sqlalchemy.exc
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from keyloop_test.booking import BookingError, BookingRequest, NotFound, book_appointment
from keyloop_test.db import get_session
from keyloop_test.models import Appointment, Dealership
from keyloop_test.schemas import AppointmentCreate, AppointmentOut

log = logging.getLogger("keyloop_test.api")

app = FastAPI(title="Unified Service Scheduler", version="0.1.0")
SessionDep = Annotated[Session, Depends(get_session)]


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(BookingError)
def _booking_error(_: Request, e: BookingError):
    return _error(e.status, e.code, e.message)


@app.exception_handler(RequestValidationError)
def _validation_error(_: Request, e: RequestValidationError):
    first = e.errors()[0]
    loc = ".".join(str(p) for p in first["loc"])
    return _error(422, "validation_error", f"{loc}: {first['msg']}")


@app.exception_handler(sqlalchemy.exc.OperationalError)
@app.exception_handler(sqlalchemy.exc.TimeoutError)
def _db_unavailable(_: Request, e: Exception):
    log.error("database unavailable", exc_info=e)
    return _error(503, "service_unavailable", "Database temporarily unavailable; please retry")


@app.post("/appointments", status_code=201, response_model=AppointmentOut)
def create_appointment(body: AppointmentCreate, session: SessionDep):
    return book_appointment(session, BookingRequest(**body.model_dump()))


@app.get("/appointments/{appointment_id}", response_model=AppointmentOut)
def get_appointment(appointment_id: int, session: SessionDep):
    appointment = session.get(Appointment, appointment_id)
    if appointment is None:
        raise NotFound(f"Appointment {appointment_id} not found")
    return appointment


@app.get("/appointments", response_model=list[AppointmentOut])
def list_appointments(
    session: SessionDep, dealership_id: Annotated[int, Query()], date: Annotated[date, Query()]
):
    dealership = session.get(Dealership, dealership_id)
    if dealership is None:
        raise NotFound(f"Dealership {dealership_id} not found")
    zone = ZoneInfo(dealership.timezone)
    day_start = datetime.combine(date, datetime.min.time(), zone)
    day_end = datetime.combine(date + timedelta(days=1), datetime.min.time(), zone)
    return session.scalars(
        select(Appointment).where(
            Appointment.dealership_id == dealership_id,
            Appointment.start_at >= day_start,
            Appointment.start_at < day_end,
        ).order_by(Appointment.start_at)
    ).all()


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(session: SessionDep):
    try:
        session.execute(text("SELECT 1"))
    except sqlalchemy.exc.SQLAlchemyError:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}
