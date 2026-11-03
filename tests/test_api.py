import sqlalchemy.exc


def body(service_type_id=2, start_at="2030-01-07T09:00:00+00:00", **kw):
    return {"vehicle_id": 1, "service_type_id": service_type_id, "dealership_id": 1,
            "start_at": start_at, **kw}


def error_code(r):
    return r.json()["error"]["code"]


def test_create_returns_201_and_body(client, seeded):
    r = client.post("/appointments", json=body())
    assert r.status_code == 201
    assert r.json().items() >= {"technician_id": 1, "service_bay_id": 1, "customer_id": 1}.items()
    assert r.json()["end_at"] == "2030-01-07T11:00:00Z"


def test_get_by_id(client, seeded):
    created = client.post("/appointments", json=body()).json()
    r = client.get(f"/appointments/{created['id']}")
    assert r.status_code == 200 and r.json()["id"] == created["id"]
    r = client.get("/appointments/999")
    assert r.status_code == 404 and error_code(r) == "not_found"


def test_list_by_dealership_date(client, seeded):
    client.post("/appointments", json=body(start_at="2030-01-07T13:00:00+00:00"))
    client.post("/appointments", json=body(start_at="2030-01-07T09:00:00+00:00"))
    client.post("/appointments", json=body(start_at="2030-01-08T09:00:00+00:00"))  # other day
    r = client.get("/appointments", params={"dealership_id": 1, "date": "2030-01-07"})
    assert r.status_code == 200
    assert [a["start_at"] for a in r.json()] == ["2030-01-07T09:00:00Z", "2030-01-07T13:00:00Z"]


def test_naive_datetime_422(client, seeded):
    r = client.post("/appointments", json=body(start_at="2030-01-07T09:00:00"))
    assert r.status_code == 422 and error_code(r) == "validation_error"


def test_offset_datetime_stored_as_utc(client, seeded):
    r = client.post("/appointments", json=body(start_at="2030-01-07T16:00:00+07:00"))
    assert r.status_code == 201 and r.json()["start_at"] == "2030-01-07T09:00:00Z"


def test_conflict_409(client, seeded):
    client.post("/appointments", json=body())
    r = client.post("/appointments", json=body(vehicle_id=2))
    assert r.status_code == 409 and error_code(r) == "no_technician_available"


def test_outside_hours_422(client, seeded):
    r = client.post("/appointments", json=body(start_at="2030-01-07T17:30:00+00:00"))
    assert r.status_code == 422 and error_code(r) == "outside_business_hours"


def test_past_422(client, seeded):
    r = client.post("/appointments", json=body(start_at="2020-01-01T09:00:00+00:00"))
    assert r.status_code == 422 and error_code(r) == "start_in_past"


def test_unknown_service_type_404(client, seeded):
    r = client.post("/appointments", json=body(service_type_id=99))
    assert r.status_code == 404 and error_code(r) == "not_found"


def test_unknown_field_422(client, seeded):
    r = client.post("/appointments", json=body(start_time="2030-01-07T09:00:00+00:00"))
    assert r.status_code == 422 and error_code(r) == "validation_error"


def test_list_requires_params(client):
    assert client.get("/appointments").status_code == 422


def test_health_live_and_ready(client):
    for path in ("/health/live", "/health/ready"):
        r = client.get(path)
        assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_db_error_maps_to_503(client, seeded, monkeypatch):
    def boom(*a, **kw):
        raise sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("lock timeout"))

    monkeypatch.setattr("keyloop_test.api.book_appointment", boom)
    r = client.post("/appointments", json=body())
    assert r.status_code == 503 and error_code(r) == "service_unavailable"
    assert "lock timeout" not in r.text


def test_out_of_range_ids_422(client, seeded):
    r = client.post("/appointments", json=body(vehicle_id=2**70))
    assert r.status_code == 422 and error_code(r) == "validation_error"
    assert client.get("/appointments/99999999999999").status_code == 422
    r = client.get("/appointments", params={"dealership_id": 2**70, "date": "2030-01-07"})
    assert r.status_code == 422 and error_code(r) == "validation_error"


def test_extreme_dates_422(client, seeded):
    r = client.post("/appointments", json=body(start_at="9999-12-31T23:30:00+00:00"))
    assert r.status_code == 422 and error_code(r) == "validation_error"
    r = client.get("/appointments", params={"dealership_id": 1, "date": "9999-12-31"})
    assert r.status_code == 422 and error_code(r) == "validation_error"


def test_unhandled_error_has_shape_and_request_id(client, seeded, monkeypatch):
    def bug(*a, **kw):
        raise RuntimeError("secret internals")

    monkeypatch.setattr("keyloop_test.api.book_appointment", bug)
    r = client.post("/appointments", json=body(), headers={"X-Request-ID": "err-1"})
    assert r.status_code == 500 and error_code(r) == "internal_error"
    assert r.headers["X-Request-ID"] == "err-1"
    assert "secret internals" not in r.text


def test_framework_errors_use_error_shape(client):
    r = client.get("/nope")
    assert r.status_code == 404 and error_code(r) == "not_found"
    r = client.delete("/appointments/1")
    assert r.status_code == 405 and error_code(r) == "method_not_allowed"


def test_openapi_documents_error_shape(client):
    spec = client.get("/openapi.json").json()
    expected = {
        ("/appointments", "post"): {"404", "409", "422", "500", "503"},
        ("/appointments/{appointment_id}", "get"): {"404", "422", "500", "503"},
        ("/appointments", "get"): {"404", "422", "500", "503"},
    }
    for (path, method), codes in expected.items():
        responses = spec["paths"][path][method]["responses"]
        assert codes <= responses.keys(), (path, method)
        for code in codes:
            ref = responses[code]["content"]["application/json"]["schema"]["$ref"]
            assert ref.endswith("/ErrorResponse"), (path, method, code)
    assert spec["components"]["schemas"]["ErrorResponse"]["properties"].keys() == {"error"}


def test_strict_types_reject_coercion(client, seeded):
    r = client.post("/appointments", json=body(vehicle_id=True))
    assert r.status_code == 422 and error_code(r) == "validation_error"
    r = client.post("/appointments", json=body(start_at=1893920400))  # epoch number
    assert r.status_code == 422 and error_code(r) == "validation_error"


def test_idempotency_key_replays_201(client, seeded):
    oil = body(service_type_id=1)
    first = client.post("/appointments", json=oil, headers={"Idempotency-Key": "abc-123"})
    again = client.post("/appointments", json=oil, headers={"Idempotency-Key": "abc-123"})
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers


def test_idempotency_key_reused_with_other_body_422(client, seeded):
    client.post("/appointments", json=body(service_type_id=1), headers={"Idempotency-Key": "k"})
    r = client.post("/appointments", json=body(service_type_id=2), headers={"Idempotency-Key": "k"})
    assert r.status_code == 422 and error_code(r) == "idempotency_key_reused"


def test_idempotency_key_invalid_422(client, seeded):
    for bad in ("x" * 256, "has space"):
        r = client.post("/appointments", json=body(), headers={"Idempotency-Key": bad})
        assert r.status_code == 422 and error_code(r) == "validation_error", bad


def test_openapi_documents_idempotency_header(client):
    params = client.get("/openapi.json").json()["paths"]["/appointments"]["post"]["parameters"]
    assert any(p["name"] == "Idempotency-Key" and p["in"] == "header" for p in params)


def test_retry_without_key_does_not_double_book(client, seeded):
    oil = body(service_type_id=1)
    assert client.post("/appointments", json=oil).status_code == 201
    r = client.post("/appointments", json=oil)  # resent: Bob is free, but it's the same car
    assert r.status_code == 409 and error_code(r) == "vehicle_already_booked"


def test_idempotency_same_instant_other_offset_replays(client, seeded):
    key = {"Idempotency-Key": "same-instant"}
    first = client.post("/appointments", json=body(service_type_id=1,
                        start_at="2030-01-07T09:00:00+00:00"), headers=key)
    again = client.post("/appointments", json=body(service_type_id=1,
                        start_at="2030-01-07T16:00:00+07:00"), headers=key)  # same instant
    assert first.status_code == again.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert again.headers["Idempotent-Replayed"] == "true"
