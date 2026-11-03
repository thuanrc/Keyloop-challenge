import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

import prometheus_client
import sqlalchemy.exc
from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from keyloop_test.booking import (
    BookingError, BookingRequest, NotFound, book_appointment, book_idempotently,
)
from keyloop_test.db import engine, get_session, init_db
from keyloop_test.observability import (
    BOOKED, REJECTED, REPLAYED, RETRIES, ObservabilityMiddleware, configure_logging,
)
from keyloop_test.models import Appointment, Dealership
from keyloop_test.schemas import (
    MAX_DATE, MAX_ID, AppointmentCreate, AppointmentOut, ErrorResponse,
)

log = logging.getLogger("keyloop_test.api")



@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    init_db(engine)
    yield


app = FastAPI(title="Unified Service Scheduler", version="0.1.0", lifespan=lifespan)
app.add_middleware(ObservabilityMiddleware)
SessionDep = Annotated[Session, Depends(get_session)]


def _errors(*codes: int) -> dict:
    """OpenAPI docs for error responses, so the contract shows the real error shape."""
    return {code: {"model": ErrorResponse} for code in (*codes, 422, 500, 503)}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(BookingError)
def _booking_error(_: Request, e: BookingError):
    return _error(e.status, e.code, e.message)


@app.exception_handler(StarletteHTTPException)
def _http_error(_: Request, e: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "http_error")
    return _error(e.status_code, code, str(e.detail))


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


@app.post("/appointments", status_code=201, response_model=AppointmentOut,
          responses=_errors(404, 409))
def create_appointment(
    body: AppointmentCreate,
    session: SessionDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(
        alias="Idempotency-Key", min_length=1, max_length=255, pattern=r"^[!-~]+$",
        description="Optional. Retrying with the same key and body returns the original "
                    "appointment instead of booking again.",
    )] = None,
):
    req = BookingRequest(**body.model_dump())
    try:
        if idempotency_key is None:
            appointment = book_appointment(session, req, on_retry=RETRIES.inc)
        else:
            request_hash = hashlib.sha256(
                json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
            appointment, replayed = book_idempotently(
                session, req, idempotency_key, request_hash, on_retry=RETRIES.inc)
            if replayed:
                REPLAYED.inc()
                response.headers["Idempotent-Replayed"] = "true"
                log.info("appointment_replayed", extra={"appointment_id": appointment.id})
                return appointment
    except BookingError as e:
        REJECTED.labels(e.code).inc()
        log.info("appointment_rejected", extra={"reason": e.code, **body.model_dump(mode="json")})
        raise
    BOOKED.inc()
    log.info("appointment_booked", extra={
        "appointment_id": appointment.id, "dealership_id": appointment.dealership_id,
        "technician_id": appointment.technician_id, "service_bay_id": appointment.service_bay_id,
        "start_at": appointment.start_at, "end_at": appointment.end_at,
    })
    return appointment


@app.get("/appointments/{appointment_id}", response_model=AppointmentOut,
         responses=_errors(404))
def get_appointment(appointment_id: Annotated[int, Path(gt=0, le=MAX_ID)], session: SessionDep):
    appointment = session.get(Appointment, appointment_id)
    if appointment is None:
        raise NotFound(f"Appointment {appointment_id} not found")
    return appointment


@app.get("/appointments", response_model=list[AppointmentOut], responses=_errors(404))
def list_appointments(
    session: SessionDep,
    dealership_id: Annotated[int, Query(gt=0, le=MAX_ID)],
    date: Annotated[date, Query(lt=MAX_DATE)],
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


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(prometheus_client.generate_latest(), media_type=prometheus_client.CONTENT_TYPE_LATEST)


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
