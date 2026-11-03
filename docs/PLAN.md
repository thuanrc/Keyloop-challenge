> **About this document.** This is the implementation plan exactly as it was written *before*
> any code existed, and then executed task by task, test-first, with one commit per task. It's
> kept unedited as a record of how the AI agent was directed. Where execution deviated (for
> example, the plan predicted the wrong PostgreSQL error for an inverted time range, and
> `/metrics` had to move from a mounted app to a route), the commit messages and the README's
> AI Collaboration Narrative explain why. Features added after the plan (strict input types,
> `Idempotency-Key`, the per-vehicle overlap constraint) are documented in
> [`SYSTEM_DESIGN.md`](SYSTEM_DESIGN.md).

# Unified Service Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A REST backend that books a service appointment only when a qualified technician and a service bay are both free for the whole duration, and persists it.

**Architecture:** FastAPI app → booking service (pure rule functions + one transactional `book_appointment`) → SQLAlchemy models on PostgreSQL. Double-booking is made impossible by `EXCLUDE USING gist` constraints. The service retries on lost races (SQLSTATE `23P01`).

**Tech Stack:** Python 3.14, uv, FastAPI, Pydantic v2, SQLAlchemy 2.0 (sync), psycopg 3 (binary), PostgreSQL 17, prometheus-client, tzdata, pytest, httpx (TestClient), Docker Compose.

**Spec:** `docs/SYSTEM_DESIGN.md`

## Global Constraints

- Package stays `src/keyloop_test/` (existing uv layout); the entry point `keyloop-test` runs the API.
- Python `>=3.14` (from `pyproject.toml`). If a dependency has no 3.14 wheel during Task 1, drop to 3.13 in both `.python-version` and `requires-python`, and note it in the README.
- All timestamps are `timestamptz`, stored in UTC. API inputs must be timezone-aware; naive inputs → 422.
- Intervals are half-open `[start_at, end_at)`.
- Error body is always `{"error": {"code": <str>, "message": <str>}}` with codes and statuses exactly as in spec §5.
- Max booking attempts per request: `3`.
- The DB URL comes from env `DATABASE_URL`, default `postgresql+psycopg://scheduler:scheduler@localhost:5432/scheduler`. Tests use `TEST_DATABASE_URL`, default `postgresql+psycopg://scheduler:scheduler@localhost:5432/scheduler_test`.
- Engine settings: `pool_size=int(env DB_POOL_SIZE or 10)`, `max_overflow=10`, `pool_timeout=5`, `pool_pre_ping=True`, `connect_args={"options": "-c lock_timeout=2000 -c statement_timeout=5000"}`.
- Test dates use the year 2030 so "not in the past" holds. Tests pass an explicit `now` wherever the function accepts one.
- Logs contain ids only, never customer name or email.
- No auth, cancellation, slot search or customer/vehicle CRUD (spec §1 out of scope).

## Review Focus

1. **Back-to-back bookings** (09:00–10:00 then 10:00–11:00, same technician) must both succeed. Pinned in Task 1 (DB level) and Task 3 (service level).
2. **Truly concurrent identical requests** with one qualified technician → exactly one 201 and one 409, never two rows. Pinned in Task 3 `test_concurrent_bookings_one_wins`.
3. **A non-UTC offset input** (`+07:00`) must be converted, not misread as UTC. A **naive datetime** → 422. Pinned in Task 2 and Task 4.
4. **Appointment running past closing time**, or across midnight local time → 422 `outside_business_hours`. Business hours are checked in the dealership's timezone, including across a DST change. Pinned in Task 2.
5. **Resource from another dealership:** a free technician at dealership 2 must not be allocated to a dealership 1 booking. Pinned in Task 3 (allocation) and Task 1 (the composite FK rejects it even if the app gets it wrong).

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | deps, pytest config |
| `docker-compose.yml` | `db` (postgres:17) + `api` |
| `docker/initdb/01-test-db.sql` | creates `scheduler_test` database |
| `Dockerfile` | API image (uv) |
| `src/keyloop_test/__init__.py` | `main()` → uvicorn |
| `src/keyloop_test/db.py` | engine, `SessionLocal`, `get_session`, `init_db` |
| `src/keyloop_test/models.py` | ORM models + constraints |
| `src/keyloop_test/booking.py` | domain errors, pure rules, `book_appointment` |
| `src/keyloop_test/schemas.py` | Pydantic request/response models |
| `src/keyloop_test/api.py` | FastAPI `app`, routes, error handlers |
| `src/keyloop_test/observability.py` | JSON logging, request-id middleware, metrics |
| `src/keyloop_test/seed.py` | demo data with fixed ids |
| `tests/conftest.py` | test DB setup, `session`, `client`, `seeded` fixtures |
| `tests/test_constraints.py` | DB-level overlap guarantees |
| `tests/test_rules.py` | pure rule unit tests (no DB) |
| `tests/test_booking.py` | service-level tests incl. concurrency |
| `tests/test_api.py` | HTTP contract tests |
| `tests/test_observability.py` | request id + metrics |
| `README.md` | build/run/test, curl examples, AI Collaboration Narrative |

---

### Task 1: Schema, DB setup and constraint guarantees

**Files:**
- Modify: `pyproject.toml`
- Create: `docker-compose.yml` (only the `db` service for now), `docker/initdb/01-test-db.sql`, `src/keyloop_test/db.py`, `src/keyloop_test/models.py`, `src/keyloop_test/seed.py` (only `seed()` for now), `tests/conftest.py`, `tests/test_constraints.py`

**Interfaces:**
- Produces:
  - `db.engine`, `db.SessionLocal` (sessionmaker, `expire_on_commit=False`), `db.get_session() -> Iterator[Session]` (FastAPI dependency), `db.init_db(engine) -> None` (runs `CREATE EXTENSION IF NOT EXISTS btree_gist`, then `Base.metadata.create_all`)
  - `models.Base`, `Dealership(id, name, timezone: str, opens_at: time, closes_at: time)`, `ServiceType(id, name, duration_minutes: int, required_skill: str)`, `Technician(id, dealership_id, name, skills: list[str])` (`ARRAY(Text)`), `ServiceBay(id, dealership_id, name)`, `Customer(id, name, email unique)`, `Vehicle(id, customer_id, vin unique, make, model, year)`, `Appointment(id, dealership_id, customer_id, vehicle_id, service_type_id, technician_id, service_bay_id, start_at, end_at, period, created_at)`
  - `Appointment.period`: `TSTZRANGE`, `Computed("tstzrange(start_at, end_at, '[)')", persisted=True)`
  - `Appointment.__table_args__`: `CheckConstraint("end_at > start_at")`, `ExcludeConstraint(("technician_id", "="), ("period", "&&"), using="gist", name="no_technician_overlap")`, the same for `service_bay_id` named `no_bay_overlap`, and `ForeignKeyConstraint(["technician_id", "dealership_id"], ["technicians.id", "technicians.dealership_id"])` plus the same for `service_bay_id` → `service_bays`
  - `Technician` and `ServiceBay` each get `UniqueConstraint("id", "dealership_id")`, which the composite FKs need
  - conftest fixtures: `engine` (session scope: drop_all + init_db on `TEST_DATABASE_URL`), `session` (function scope; truncates all tables `RESTART IDENTITY CASCADE` after each test), `seeded` (calls `seed(session)`, returns `None`)
  - `seed(session) -> None` **commits**, so that separately opened sessions (the concurrency test) see the data

**Seed dataset** (also used by `seed.py` in Task 6; define it once in `seed.py` as `seed(session) -> None` and have the fixture call it):
- Dealership 1 "Keyloop Motors London", `Europe/London`, 08:00–18:00. Dealership 2 "Keyloop Motors Hanoi", `Asia/Ho_Chi_Minh`, 08:00–17:00.
- ServiceType 1 "Oil change" 60 min `general` · 2 "Brake replacement" 120 min `brakes` · 3 "EV battery check" 90 min `ev`
- Technicians (dealership 1): 1 Alice `[general, brakes]`, 2 Bob `[general]`. (Dealership 2): 3 Chen `[ev, general, brakes]`
- Bays (dealership 1): 1 "Bay A", 2 "Bay B". (Dealership 2): 3 "Bay 1"
- Customer 1 Jane Doe `jane@example.com`; Vehicle 1 (customer 1) VIN `WVWZZZ1JZXW000001` VW Golf 2021

So `seed.py` is created in this task with just `seed()`; Task 6 adds the CLI.

- [ ] **Step 1: Add dependencies**

Run: `uv add fastapi "uvicorn[standard]" "sqlalchemy>=2.0" "psycopg[binary]>=3.2" prometheus-client tzdata` and `uv add --dev pytest httpx`. Add `[tool.pytest.ini_options] testpaths = ["tests"]` to `pyproject.toml`.
Expected: `uv sync` succeeds. (If it fails on 3.14 wheels, apply the Global Constraints fallback.)

- [ ] **Step 2: Compose DB**

`docker-compose.yml` `db` service: `postgres:17`, env `POSTGRES_USER/PASSWORD/DB = scheduler`, port `5432:5432`, mount `./docker/initdb:/docker-entrypoint-initdb.d`, healthcheck `pg_isready -U scheduler`. `01-test-db.sql`: `CREATE DATABASE scheduler_test;`
Run: `docker compose up -d db`, then `docker compose exec db psql -U scheduler -lqt`
Expected: both `scheduler` and `scheduler_test` are listed.

- [ ] **Step 3: Write failing constraint tests** in `tests/test_constraints.py`

```python
def _appt(tech, bay, start, end):  # helper building Appointment for dealership 1, vehicle 1, customer 1, service type 1
    ...

def test_overlapping_technician_rejected(session, seeded):
    session.add(_appt(1, 1, T(9), T(10))); session.commit()
    session.add(_appt(1, 2, T(9, 30), T(10, 30)))
    with pytest.raises(IntegrityError) as e: session.commit()
    assert e.value.orig.sqlstate == "23P01"

def test_overlapping_bay_rejected(session, seeded):   # same bay 1, technicians 1 and 2 → 23P01

def test_back_to_back_allowed(session, seeded):       # tech 1 bay 1: 09–10 then 10–11 → both commit

def test_end_before_start_rejected(session, seeded):  # → IntegrityError sqlstate "23514"

def test_cross_dealership_technician_rejected(session, seeded):
    # dealership 1 appointment with technician 3 (Chen, dealership 2) → IntegrityError sqlstate "23503"
```

where `T(h, m=0)` returns `datetime(2030, 1, 7, h, m, tzinfo=UTC)`.

- [ ] **Step 4: Run tests — expect failure** — `uv run pytest tests/test_constraints.py -v` → errors on import (`models` missing).

- [ ] **Step 5: Implement `db.py`, `models.py`, `seed.seed()`** per Interfaces.

- [ ] **Step 6: Run tests — expect pass** — `uv run pytest tests/test_constraints.py -v` → 5 passed. This proves the generated `tstzrange` column, both exclusion constraints and the dealership FK work.

- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat: schema with exclusion constraints against double-booking"`

---

### Task 2: Pure booking rules

**Files:**
- Create: `src/keyloop_test/booking.py` (errors + pure functions only), `tests/test_rules.py`

**Interfaces:**
- Produces:
  - `class BookingError(Exception)` with class attrs `code: str`, `status: int`, and an instance `message`. Subclasses: `NotFound("not_found", 404)`, `StartInPast("start_in_past", 422)`, `OutsideBusinessHours("outside_business_hours", 422)`, `NoTechnicianAvailable("no_technician_available", 409)`, `NoBayAvailable("no_bay_available", 409)`, `SlotTaken("slot_taken", 409)`
  - `compute_end(start_at: datetime, duration_minutes: int) -> datetime`
  - `check_not_past(start_at: datetime, now: datetime) -> None` raises `StartInPast` if `start_at <= now`
  - `check_business_hours(start_at: datetime, end_at: datetime, tz: str, opens_at: time, closes_at: time) -> None` raises `OutsideBusinessHours` unless the local start and end fall on the same local date and `opens_at <= local_start.time()` and `local_end.time() <= closes_at` (convert with `zoneinfo.ZoneInfo(tz)`)

- [ ] **Step 1: Write failing tests** in `tests/test_rules.py`

```python
LON = "Europe/London"; OPEN, CLOSE = time(8), time(18)

def test_compute_end_adds_duration():
    assert compute_end(dt("2030-01-07T09:00+00:00"), 120) == dt("2030-01-07T11:00+00:00")

def test_past_start_rejected():          # start == now → StartInPast; start = now + 1 min → no raise

def test_within_hours_ok():              # 08:00–18:00 London winter (UTC+0) → no raise
def test_ends_after_close_rejected():    # 17:30–18:30 London → OutsideBusinessHours
def test_starts_before_open_rejected():  # 07:30–08:30 → OutsideBusinessHours
def test_crosses_midnight_rejected():    # 23:00–01:00 with opens 00:00 closes 23:59 → OutsideBusinessHours
def test_offset_input_converted():       # "2030-01-07T15:00+07:00" == 08:00 London → no raise
def test_dst_summer_time():              # "2030-07-08T07:00+00:00" == 08:00 BST → no raise;
                                         # "2030-07-08T17:30+00:00" + 60 min → 19:30 BST → raises
```

- [ ] **Step 2: Run — expect fail** — `uv run pytest tests/test_rules.py -v` → ImportError.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run — expect pass** — 8 passed.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: booking rules (duration, past, business hours)"`

---

### Task 3: `book_appointment` — allocation, atomic insert, retry

**Files:**
- Modify: `src/keyloop_test/booking.py`
- Create: `tests/test_booking.py`

**Interfaces:**
- Consumes: models (Task 1), rules and errors (Task 2)
- Produces:
  - `@dataclass(frozen=True) class BookingRequest: vehicle_id: int; service_type_id: int; dealership_id: int; start_at: datetime`
  - `book_appointment(session: Session, req: BookingRequest, now: datetime | None = None, *, on_retry: Callable[[], None] | None = None) -> Appointment`. Commits on success. On a `BookingError` it rolls back and re-raises. `now` defaults to `datetime.now(UTC)`. `on_retry` is called once per lost race; Task 5 passes a metric increment here, so booking has no dependency on observability.
  - `MAX_ATTEMPTS = 3`

**Algorithm** (the signature doesn't determine this):
1. `session.get` the vehicle, service type and dealership; any missing → `NotFound(f"{entity} {id} not found")`.
2. `end_at = compute_end(...)`; `check_not_past`; `check_business_hours` using the dealership's fields.
3. Up to `MAX_ATTEMPTS` times:
   ```python
   overlap = lambda col, rid: exists().where(col == rid, Appointment.start_at < end_at, Appointment.end_at > req.start_at)
   tech = session.scalars(select(Technician).where(
       Technician.dealership_id == req.dealership_id,
       literal(st.required_skill) == any_(Technician.skills),
       ~overlap(Appointment.technician_id, Technician.id)).order_by(Technician.id).limit(1)).first()
   # same for ServiceBay with Appointment.service_bay_id
   ```
   No tech → `NoTechnicianAvailable`; no bay → `NoBayAvailable`. Then, inside `with session.begin_nested():`, add the Appointment (`customer_id=vehicle.customer_id`) and flush. Catch `IntegrityError` with `e.orig.sqlstate == "23P01"` → call `on_retry`, continue. Any other `IntegrityError` → re-raise.
4. Attempts exhausted → `SlotTaken("Resources were taken by a concurrent booking; please retry")`.
5. `session.commit()`; return the appointment.

- [ ] **Step 1: Write failing tests** in `tests/test_booking.py` (use `now=T(0)` on 2030-01-07 everywhere; times in UTC = London winter)

```python
def test_books_with_qualified_tech_and_bay(session, seeded):
    a = book_appointment(session, req(service_type_id=2, start=T(9)), now=NOW)
    assert (a.technician_id, a.service_bay_id, a.customer_id) == (1, 1, 1)   # only Alice has "brakes"
    assert a.end_at == T(11)
    assert session.get(Appointment, a.id) is not None

def test_second_overlapping_brake_job_no_technician(session, seeded):
    book(service_type_id=2, start=T(9)); 
    with pytest.raises(NoTechnicianAvailable): book(service_type_id=2, start=T(10))

def test_oil_change_falls_back_to_other_tech(session, seeded):
    book(2, T(9))  # Alice busy 9–11
    a = book(1, T(9)); assert (a.technician_id, a.service_bay_id) == (2, 2)

def test_no_bay_available(session, seeded):
    # add Technician 4 "Dana" [general] at dealership 1, so technicians outnumber the 2 bays
    book(1, T(9)); book(1, T(9))
    with pytest.raises(NoBayAvailable): book(1, T(9))

def test_back_to_back_same_resources(session, seeded):
    a = book(2, T(9)); b = book(2, T(11)); assert a.technician_id == b.technician_id == 1

def test_other_dealership_resources_not_used(session, seeded):
    book(2, T(9))  # Alice busy; Chen (dealership 2) has brakes and is free
    with pytest.raises(NoTechnicianAvailable): book(2, T(9, 30))

def test_unknown_vehicle_not_found(session, seeded):   # vehicle_id=999 → NotFound, code "not_found"

def test_concurrent_bookings_one_wins(engine, seeded):
    # two threads, each with its own SessionLocal bound to engine, threading.Barrier(2) before calling
    # book_appointment(service_type_id=2, start=T(9)); collect results/exceptions
    assert sorted(type(r).__name__ for r in results) == ["Appointment", "NoTechnicianAvailable"] \
        or sorted(...) == ["Appointment", "SlotTaken"]
    assert count(appointments) == 1

def test_retry_on_lost_race(session, seeded, monkeypatch):
    # Simulate losing the race deterministically: monkeypatch session.flush so that its FIRST call
    # commits a conflicting Alice 09:00–11:00 appointment through a separate SessionLocal, then calls the
    # real flush (→ 23P01). The retry re-queries, sees Alice busy, and raises NoTechnicianAvailable.
    retries = []
    with pytest.raises(NoTechnicianAvailable):
        book_appointment(session, req(2, T(9)), now=NOW, on_retry=lambda: retries.append(1))
    assert retries == [1]
```

- [ ] **Step 2: Run — expect fail** — `uv run pytest tests/test_booking.py -v`
- [ ] **Step 3: Implement** per Interfaces + Algorithm.
- [ ] **Step 4: Run — expect pass** — `uv run pytest tests/test_booking.py -v` → 9 passed. Run the concurrency test 20× to check it isn't flaky: `for i in $(seq 20); do uv run pytest -q tests/test_booking.py::test_concurrent_bookings_one_wins || break; done` → all pass.
- [ ] **Step 5: Commit** — `git commit -m "feat: atomic booking with allocation and race retry"`

---

### Task 4: HTTP API

**Files:**
- Create: `src/keyloop_test/schemas.py`, `src/keyloop_test/api.py`, `tests/test_api.py`
- Modify: `src/keyloop_test/__init__.py` (`main()` runs `uvicorn.run("keyloop_test.api:app", host="0.0.0.0", port=8000)`), `tests/conftest.py` (add `client` fixture: `TestClient(app)` with `app.dependency_overrides[get_session]` yielding the test `session`)

**Interfaces:**
- Consumes: `book_appointment`, `BookingRequest`, `BookingError`, `get_session`, models
- Produces:
  - `schemas.AppointmentCreate(vehicle_id: PositiveInt, service_type_id: PositiveInt, dealership_id: PositiveInt, start_at: AwareDatetime)`, with `model_config = ConfigDict(extra="forbid")` so typos such as `start_time` → 422 instead of being silently ignored
  - `schemas.AppointmentOut` (all Appointment fields except `period`; `model_config = ConfigDict(from_attributes=True)`; datetimes serialised in UTC)
  - `api.app: FastAPI` with routes from spec §5: `POST /appointments` → 201; `GET /appointments/{appointment_id}`; `GET /appointments?dealership_id=&date=` (both required; date is the local date in the dealership tz, i.e. the range `[local midnight, next local midnight)` converted to UTC, ordered by `start_at`); `GET /health/live` → `{"status": "ok"}`; `GET /health/ready` (`SELECT 1` → `{"status": "ok"}`; on failure 503 `{"status": "unavailable"}`)
  - Exception handlers: `BookingError` → `JSONResponse(status=e.status, {"error": {"code": e.code, "message": e.message}})`; `RequestValidationError` → 422 with code `validation_error` and the message made from the first error's `loc` + `msg`; `sqlalchemy.exc.OperationalError` and `sqlalchemy.exc.TimeoutError` → 503 with code `service_unavailable`, logged with the exception (no stack trace in the response body)
  - API calls `book_appointment(session, BookingRequest(**body.model_dump()))`; `now` is left at its default

- [ ] **Step 1: Write failing tests** in `tests/test_api.py`

```python
def test_create_returns_201_and_body(client, seeded):
    r = client.post("/appointments", json=body(service_type_id=2, start_at="2030-01-07T09:00:00+00:00"))
    assert r.status_code == 201
    assert r.json().items() >= {"technician_id": 1, "service_bay_id": 1, "customer_id": 1}.items()
    assert r.json()["end_at"].startswith("2030-01-07T11:00:00")

def test_get_by_id(client, seeded):        # create then GET → 200, same id; GET /appointments/999 → 404 code "not_found"
def test_list_by_dealership_date(client, seeded):  # two bookings → list returns both, ordered by start_at
def test_naive_datetime_422(client, seeded):       # "2030-01-07T09:00:00" → 422, code "validation_error"
def test_offset_datetime_stored_as_utc(client, seeded):  # "2030-01-07T16:00:00+07:00" → start_at "2030-01-07T09:00:00Z"
def test_conflict_409(client, seeded):             # two brake jobs at 09:00 → second 409 code "no_technician_available"
def test_outside_hours_422(client, seeded):        # 17:30 brake job → 422 "outside_business_hours"
def test_past_422(client, seeded):                 # "2020-01-01T09:00:00+00:00" → 422 "start_in_past"
def test_unknown_service_type_404(client, seeded)
def test_unknown_field_422(client, seeded):        # body with extra "start_time" key → 422 "validation_error"
def test_list_requires_params(client):             # GET /appointments → 422
def test_health_live_and_ready(client):            # both 200 {"status": "ok"}
def test_db_error_maps_to_503(client, seeded, monkeypatch):
    # monkeypatch keyloop_test.api.book_appointment to raise sqlalchemy.exc.OperationalError("x", {}, Exception())
    # → 503, code "service_unavailable"
```

- [ ] **Step 2: Run — expect fail.** `uv run pytest tests/test_api.py -v`
- [ ] **Step 3: Implement** schemas, api, `main()`.
- [ ] **Step 4: Run — expect pass.** `uv run pytest -v` → all tests so far pass.
- [ ] **Step 5: Commit** — `git commit -m "feat: REST API for appointments"`

---

### Task 5: Observability

**Files:**
- Create: `src/keyloop_test/observability.py`, `tests/test_observability.py`
- Modify: `src/keyloop_test/api.py` (install middleware, mount `/metrics`, log + count booking outcomes, pass `on_retry`)

**Interfaces:**
- Produces:
  - `request_id_var: ContextVar[str]`
  - `JsonFormatter(logging.Formatter)`: emits one JSON object per record with `ts` (ISO UTC), `level`, `logger`, `msg`, `request_id`, plus any `extra=` keys
  - `configure_logging(level: str = "INFO") -> None` (root handler to stdout with `JsonFormatter`; level from env `LOG_LEVEL`)
  - `ObservabilityMiddleware` (pure ASGI middleware, so the contextvar is reliably visible inside sync endpoints and exception handlers): reads `X-Request-ID` or generates `uuid4().hex`, sets `request_id_var`, times the request, sets response header `X-Request-ID`, observes `HTTP_LATENCY.labels(method, route_template, status)`, logs `request` with `method, path, status, duration_ms`
  - Metrics: `BOOKED = Counter("appointments_booked_total", ...)`, `REJECTED = Counter("appointment_booking_rejected_total", ..., ["reason"])`, `RETRIES = Counter("appointment_booking_retries_total", ...)`, `HTTP_LATENCY = Histogram("http_request_duration_seconds", ..., ["method", "route", "status"])`
  - In `POST /appointments`: on success `BOOKED.inc()` + log `appointment_booked` with `appointment_id, dealership_id, technician_id, service_bay_id`; on `BookingError` `REJECTED.labels(e.code).inc()` + log `appointment_rejected` with `reason`; `on_retry=RETRIES.inc`
  - `app.mount("/metrics", prometheus_client.make_asgi_app())`. The route label uses the matched route template (`scope["route"].path` after the app has handled the request, e.g. `/appointments/{appointment_id}`), not the raw path, so ids don't explode label cardinality. Unmatched paths use the label `"unmatched"`.

- [ ] **Step 1: Write failing tests**

```python
def test_request_id_generated_and_echoed(client):   # GET /health/live → header X-Request-ID, len 32
def test_request_id_propagated(client):             # send X-Request-ID: abc → response header == "abc"
def test_metrics_count_booking(client, seeded):     # read REJECTED/BOOKED via prometheus_client.REGISTRY.get_sample_value
                                                    # before/after one success + one 409 → booked +1, rejected{reason="no_technician_available"} +1
def test_json_log_has_request_id(client, seeded):
    buf = io.StringIO(); h = logging.StreamHandler(buf); h.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(h)   # remove in finally
    # POST a booking with header X-Request-ID: t-1
    lines = [json.loads(l) for l in buf.getvalue().splitlines()]
    booked = next(l for l in lines if l["msg"] == "appointment_booked")
    assert booked["request_id"] == "t-1" and "appointment_id" in booked
    assert "jane" not in buf.getvalue().lower()         # no PII
```

- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement.** Call `configure_logging()` in the app's lifespan startup.
- [ ] **Step 4: Run — expect pass.** `uv run pytest -v` → all green.
- [ ] **Step 5: Commit** — `git commit -m "feat: structured logs, request ids, prometheus metrics"`

---

### Task 6: Runnable package — seed CLI, Docker, README

**Files:**
- Modify: `src/keyloop_test/seed.py` (add `if __name__ == "__main__":` → `init_db(engine)`, then seed only if the `dealerships` table is empty, print "seeded" or "already seeded"), `docker-compose.yml` (add `api`), `README.md`
- Create: `Dockerfile`

**Interfaces:**
- `Dockerfile`: `FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim`, copy the project, `uv sync --frozen --no-dev`, `CMD ["sh", "-c", "uv run python -m keyloop_test.seed && uv run keyloop-test"]`, `EXPOSE 8000`
- compose `api`: build `.`, `DATABASE_URL=postgresql+psycopg://scheduler:scheduler@db:5432/scheduler`, `depends_on: db: condition: service_healthy`, ports `8000:8000`
- `init_db` also runs on app startup (lifespan) so that `uv run keyloop-test` works without a separate seed step. `# shortcut: create_all instead of migrations; move to Alembic when the schema first changes after release`

- [ ] **Step 1: Write the README** with these sections: Overview (link `docs/SYSTEM_DESIGN.md`); Prerequisites (Docker, uv); **Run** (`docker compose up --build` → http://localhost:8000/docs); **Run locally** (`docker compose up -d db`, `uv run python -m keyloop_test.seed`, `uv run keyloop-test`); **Test** (`docker compose up -d db`, `uv run pytest -v`); **API examples** (curl for: a successful booking, a conflicting booking → 409, a naive time → 422, a GET, the metrics endpoint), plus a seed-id table; Assumptions (link spec §2); **AI Collaboration Narrative** (headings: Strategy, Verification process, Where the AI was wrong and how I caught it, Ensuring final quality; leave the author's content as `TODO(author)` bullets, since this must be the candidate's own account).
- [ ] **Step 2: Verify the end-to-end demo**

Run: `docker compose up --build -d`, then
`curl -s -X POST localhost:8000/appointments -H 'content-type: application/json' -d '{"vehicle_id":1,"service_type_id":2,"dealership_id":1,"start_at":"2030-01-07T09:00:00+00:00"}'`
Expected: 201 JSON with `technician_id: 1`. Repeat the same curl → 409 `no_technician_available`. `curl -s localhost:8000/metrics | grep appointments_booked_total` → `1.0`.
- [ ] **Step 3: Run the full test suite** — `uv run pytest -v` → all pass.
- [ ] **Step 4: Commit** — `git commit -m "feat: docker compose demo, seed CLI, README"`

---

## Self-review notes

- Spec coverage: req 1 → Task 3/4 · req 2 → Task 1 (constraints) + Task 3 (allocation) · req 3 → Task 1/3 · API contract incl. 503 mapping → Task 4 · observability incl. live/ready split → Tasks 4–5 · timeouts/pool → Global Constraints (engine) · testing strategy (spec §11) → Tasks 1–5 · README/run → Task 6. Tracing, idempotency and auth are spec future work and are intentionally not built.
- Type consistency checked: `BookingRequest`, `book_appointment(session, req, now=None, *, on_retry=None)`, error `code`/`status` names match between Tasks 2–5.
- Known untested path: the real `lock_timeout` firing. Simulating it needs a held lock for >2 s, which is slow and flaky; the 503 mapping is tested through the monkeypatched `OperationalError` instead.
