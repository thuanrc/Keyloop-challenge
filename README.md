# Unified Service Scheduler

[![CI](https://github.com/thuanrc/Keyloop-challenge/actions/workflows/ci.yml/badge.svg)](https://github.com/thuanrc/Keyloop-challenge/actions/workflows/ci.yml)

Keyloop technical assessment — **Scenario A**, backend implementation.

A REST API that books a vehicle service appointment only when a **qualified technician** and a
**service bay** are both free for the **whole service duration**, and stores the confirmed
appointment. Double-booking is prevented by PostgreSQL exclusion constraints, so it holds even
under concurrent requests.

- System design: [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) (architecture, data flow,
  technology choices, observability, assumptions)
- Implementation plan the AI agent executed: [`docs/PLAN.md`](docs/PLAN.md)
- API contract: generated OpenAPI at http://localhost:8000/docs (`/openapi.json`) once running,
  including the error responses and their `ErrorResponse` schema
- CI: every push runs the test suite against PostgreSQL 17 (`.github/workflows/ci.yml`)

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (runs PostgreSQL, and optionally the API)
- [uv](https://docs.astral.sh/uv/) (only for running locally / tests). Python 3.14 is fetched by uv.

## Run (everything in Docker)

```bash
docker compose up --build
```

API on http://localhost:8000 — Swagger UI at http://localhost:8000/docs. Demo data is seeded on
start-up.

## Run locally

```bash
docker compose up -d db          # PostgreSQL only
uv sync
uv run python -m keyloop_test.seed
uv run keyloop-test              # http://localhost:8000
```

Configuration (env vars): `DATABASE_URL`, `DB_POOL_SIZE` (default 10), `LOG_LEVEL` (default INFO).

## Test

```bash
docker compose up -d db   # tests use the scheduler_test database on the same server
uv run pytest -v
```

| Test file | What it proves |
|---|---|
| `test_rules.py` | duration, not-in-past, business hours across time zones and DST (no DB) |
| `test_constraints.py` | the database itself rejects overlapping technician, bay and vehicle bookings, cross-dealership resources and invalid intervals; back-to-back bookings are allowed |
| `test_booking.py` | allocation (skill match, fallback, dealership scoping), every error path, retry after a lost race, **two truly concurrent requests → exactly one booking** |
| `test_idempotency.py` | `Idempotency-Key`: a retry replays without re-booking, key reuse with another body is rejected, a failed attempt doesn't consume the key, **two concurrent same-key requests → one booking** |
| `test_api.py` | HTTP status codes, error shape, time-zone handling, 503 on DB failure, idempotency header |
| `test_observability.py` | request-id propagation, JSON logs without PII, metrics |
| `test_seed.py` | seed command is idempotent |

## API examples

Seed data (ids):

| Entity | Data |
|---|---|
| Dealerships | 1 London (`Europe/London`, 08:00–18:00) · 2 Hanoi (`Asia/Ho_Chi_Minh`, 08:00–17:00) |
| Service types | 1 Oil change 60 min `general` · 2 Brake replacement 120 min `brakes` · 3 EV battery check 90 min `ev` |
| Technicians | 1 Alice `[general, brakes]` (London) · 2 Bob `[general]` (London) · 3 Chen `[ev, general, brakes]` (Hanoi) |
| Service bays | 1 Bay A, 2 Bay B (London) · 3 Bay 1 (Hanoi) |
| Customers / vehicles | 1 Jane Doe: vehicle 1 VW Golf · 2 John Smith: vehicle 2 Toyota Corolla, vehicle 3 Tesla Model 3 |

```bash
# 1. Book a brake job → 201, Alice + Bay A, 09:00–11:00
curl -X POST localhost:8000/appointments -H 'content-type: application/json' \
  -d '{"vehicle_id":1,"service_type_id":2,"dealership_id":1,"start_at":"2030-01-07T09:00:00+00:00"}'

# 2. Same request again → 409 vehicle_already_booked (a resent request can't double-book the car)

# 3. Another car wants a brake job at 9 → 409 no_technician_available (only Alice does brakes)
curl -X POST localhost:8000/appointments -H 'content-type: application/json' \
  -d '{"vehicle_id":2,"service_type_id":2,"dealership_id":1,"start_at":"2030-01-07T09:00:00+00:00"}'

#    ...but an oil change for that car at 9 → 201, falls back to Bob + Bay B
curl -X POST localhost:8000/appointments -H 'content-type: application/json' \
  -d '{"vehicle_id":2,"service_type_id":1,"dealership_id":1,"start_at":"2030-01-07T09:00:00+00:00"}'

# 4. Time without an offset → 422 validation_error ("Input should have timezone info")
curl -X POST localhost:8000/appointments -H 'content-type: application/json' \
  -d '{"vehicle_id":1,"service_type_id":1,"dealership_id":1,"start_at":"2030-01-07T09:00:00"}'

# 5. Safe retry: send the same request twice with one Idempotency-Key.
#    The second call returns the SAME appointment (header Idempotent-Replayed: true) instead of
#    booking again. Without the key, the retry would get 409 vehicle_already_booked.
curl -i -X POST localhost:8000/appointments -H 'content-type: application/json' \
  -H 'Idempotency-Key: 7f3c9a2e-retry-demo' \
  -d '{"vehicle_id":1,"service_type_id":1,"dealership_id":1,"start_at":"2030-01-08T09:00:00+00:00"}'

# 6. Read back
curl localhost:8000/appointments/1
curl "localhost:8000/appointments?dealership_id=1&date=2030-01-07"

# 7. Operations
curl localhost:8000/health/ready
curl localhost:8000/metrics | grep appointment
```

Error codes: `not_found` 404 · `validation_error`, `start_in_past`, `outside_business_hours`,
`idempotency_key_reused` 422 ·
`vehicle_already_booked`, `no_technician_available`, `no_bay_available`, `slot_taken` 409 · `internal_error` 500 ·
`service_unavailable` 503. All errors share one shape: `{"error": {"code": ..., "message": ...}}`.

## Project layout

```
src/keyloop_test/
  api.py            FastAPI routes, error mapping, Idempotency-Key handling, /health, /metrics
  booking.py        business rules + transactional booking (allocation, retry, idempotent replay)
  models.py         SQLAlchemy models, exclusion constraints, idempotency keys
  schemas.py        request/response/error models
  observability.py  JSON logging, request-id middleware, Prometheus metrics
  db.py             engine (pool + connect/lock/statement timeouts), sessions, init_db
  seed.py           demo data + `python -m keyloop_test.seed`
tests/              pytest suite (real PostgreSQL)
docs/               system design (SYSTEM_DESIGN.md) and the implementation plan (PLAN.md)
```

## Assumptions

See [System Design §2](docs/SYSTEM_DESIGN.md#2-assumptions). Highlights: duration comes from the
service type; technicians qualify by skill; the system picks the technician and bay; times must
carry a UTC offset; intervals are half-open, so back-to-back bookings are allowed; one car can't
have two overlapping appointments.

## Known limitations

These are deliberate cuts or found-but-deferred items, not surprises:

- **Overlap queries don't use the GiST index.** They filter on `start_at`/`end_at` rather than
  `period`, and the list endpoint has no `(dealership_id, start_at)` index. That's fine at demo
  scale; switch to `period && tstzrange(...)` and add the index before real load.
- **One extra SELECT per booking response,** to reload the server-generated `created_at`
  (`eager_defaults` would remove it).
- **`X-Request-ID` is echoed as sent** (length and characters aren't validated).
- **Idempotency keys never expire** and are global (there's no auth to scope them per client).
- **`create_all` instead of migrations:** the schema is created on startup, so an existing
  database doesn't pick up new constraints until it's recreated. Alembic is the next step.
- **Untested paths:** a real `lock_timeout` firing (the 503 mapping is tested with a simulated
  error), a retry that loses a race and then *succeeds* with another technician, and a booking
  on a DST changeover day.
- **Not built:** auth, cancellation and rescheduling, slot search, tracing (designed in
  [§8](docs/SYSTEM_DESIGN.md#8-observability)).

## AI Collaboration Narrative

I built this with Claude Code as the implementing agent. I treated it like a fast engineer who
needs a clear spec and whose output has to be proven, not trusted. The full plan it executed is
kept unedited in [`docs/PLAN.md`](docs/PLAN.md).

### Strategy

- **Decide before building.** I gave the agent the brief and had it compare the four scenarios
  against the evaluation criteria. I picked **Scenario A** because it has a real concurrency
  problem, where B and C are mostly CRUD. I kept the scope to the three requirements: no auth,
  cancellation or slot search.
- **Spec → review → plan → code → test, in that order.** The agent first wrote the system design. I
  then had it **review its own design as a separate pass** before any code existed. Next came a
  task-by-task, test-first implementation plan with exact interfaces and test cases, and only then
  execution, with one commit per task.
- **Keep decisions visible.** Wherever the agent deviated from the plan, it had to record the
  decision and its cost, so nothing changed silently.
- **Stay in control of side effects.** 

### Verification process

- **Test-first, every task:** each test was run and seen to *fail* before the code existed, so
  every test is proven able to catch its bug.
- **Proving the concurrency test is real:** it passed 20/20 runs. But a passing concurrency test
  can also mean the requests never actually overlapped, so a separate probe confirmed that 30/30
  runs hit a real lost race (a PostgreSQL `23P01` exclusion violation, then a retry).
- **An independent final review:** a fresh agent with no memory of writing the code reviewed the
  whole branch. It found inputs that produced bare 500s (ids beyond the integer range, dates
  near year 9999), unhandled errors losing their request id, and framework 404/405s with a
  different error shape. Each fix got a failing test first.
- **Testing the real thing, not just the harness:** the stack was run in Docker and driven with
  curl, and the whole repo was re-tested from a **fresh clone** before submission. I also ran
  the GitHub Actions CI myself.

### Where the AI was wrong, and how it was caught

- **The plan predicted the wrong error.** It expected an inverted time range to hit the CHECK
  constraint. Running the test showed PostgreSQL rejects it earlier, while building the
  generated range (`DataError`). The test was split: zero-length → CHECK, inverted → `DataError`.
- **`/metrics` returned a 307 redirect.** All in-process tests passed, because the test client
  silently follows redirects. Only the curl demo against the real container exposed it. A
  regression test came first, then the fix.
- **The first design had real gaps,** caught in the review pass:
  - a liveness check that would restart-loop pods during a database outage;
  - an alert on a metric that would almost never fire;
  - no lock or pool timeouts;
  - no database guard against cross-dealership resources;
  - a test fixture the plan used but never defined.
- **A resent request double-booked the car.** When I asked whether an idempotency key was worth
  adding, the agent first *checked whether the problem existed*, by sending the same booking
  twice to the running stack. It created two appointments for one car: the retry silently took
  Bob after the first request took Alice. I had it fixed in two layers:
  - a per-vehicle exclusion constraint, so a retry can never double-book;
  - an `Idempotency-Key` header, so a retry gets the original booking back.
- **A test run hung when the database was down.** That exposed a missing connect timeout. An
  unreachable host hung a connection for over 90 s; with `connect_timeout=3` it now fails in
  3.0 s with a clean 503.

### Ensuring final quality

- Every invariant that can be declared lives in PostgreSQL:
  - no overlap per technician, bay or vehicle;
  - resources belong to the appointment's dealership;
  - valid intervals.

  So a future bug can't break them.
- There are 65 tests against a real PostgreSQL (no mocks of the core guarantee), and CI runs them on
  every push.
- One error shape everywhere, documented in the OpenAPI contract. Every database wait has a
  timeout. Logs carry a request id and no customer PII.
- What I'd do next is in [Known limitations](#known-limitations) and
  [System Design §12](docs/SYSTEM_DESIGN.md#12-future-work).
