"""Structured JSON logs, request-id correlation and Prometheus metrics."""

import json
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from prometheus_client import Counter, Histogram

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

BOOKED = Counter("appointments_booked", "Appointments successfully booked")
REJECTED = Counter("appointment_booking_rejected", "Booking requests rejected", ["reason"])
RETRIES = Counter("appointment_booking_retries", "Bookings retried after losing a race")
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency",
                         ["method", "route", "status"])

_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        entry |= {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS}
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    if not any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)


access_log = logging.getLogger("keyloop_test.access")


class ObservabilityMiddleware:
    """Pure ASGI so the request-id contextvar reaches sync endpoints and exception handlers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        rid = headers.get(b"x-request-id", b"").decode("latin-1") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        status, started = 500, time.perf_counter()

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            elapsed = time.perf_counter() - started
            route = getattr(scope.get("route"), "path", "unmatched")
            HTTP_LATENCY.labels(scope["method"], route, str(status)).observe(elapsed)
            access_log.info("request", extra={"method": scope["method"], "path": scope["path"],
                                              "status": status,
                                              "duration_ms": round(elapsed * 1000, 1)})
            request_id_var.reset(token)
