import io
import json
import logging

from prometheus_client import REGISTRY

from keyloop_test.observability import JsonFormatter

BODY = {"vehicle_id": 1, "service_type_id": 2, "dealership_id": 1,
        "start_at": "2030-01-07T09:00:00+00:00"}


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_request_id_generated_and_echoed(client):
    r = client.get("/health/live")
    assert len(r.headers["X-Request-ID"]) == 32


def test_request_id_propagated(client):
    r = client.get("/health/live", headers={"X-Request-ID": "abc"})
    assert r.headers["X-Request-ID"] == "abc"


def test_metrics_count_booking(client, seeded):
    booked = sample("appointments_booked_total")
    rejected = sample("appointment_booking_rejected_total", reason="no_technician_available")
    client.post("/appointments", json=BODY)
    client.post("/appointments", json=BODY)  # Alice busy → 409
    assert sample("appointments_booked_total") == booked + 1
    assert sample("appointment_booking_rejected_total",
                  reason="no_technician_available") == rejected + 1


def test_latency_uses_route_template(client, seeded):
    client.get("/appointments/12345")
    labels = dict(method="GET", route="/appointments/{appointment_id}", status="404")
    assert sample("http_request_duration_seconds_count", **labels) >= 1
    assert "appointments_booked_total" in client.get("/metrics").text


def test_json_log_has_request_id(client, seeded):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        client.post("/appointments", json=BODY, headers={"X-Request-ID": "t-1"})
    finally:
        root.removeHandler(handler)
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    booked = next(line for line in lines if line["msg"] == "appointment_booked")
    assert booked["request_id"] == "t-1" and "appointment_id" in booked
    assert any(line["msg"] == "request" and line["status"] == 201 for line in lines)
    assert "jane" not in buf.getvalue().lower()  # no PII
