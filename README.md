# Unified Service Scheduler

[![CI](https://github.com/thuanrc/Keyloop-challenge/actions/workflows/ci.yml/badge.svg)](https://github.com/thuanrc/Keyloop-challenge/actions/workflows/ci.yml)

Keyloop technical assessment — **Scenario A**, backend implementation.

A REST API that books a vehicle service appointment only when a **qualified technician** and a
**service bay** are both free for the **whole service duration**, and stores the confirmed
appointment. Double-booking is prevented by PostgreSQL exclusion constraints, so it holds even
under concurrent requests.

- System design: [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) (architecture, data flow,
  technology choices, observability, assumptions)
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
| `test_constraints.py` | the database itself rejects overlapping bookings, cross-dealership resources and invalid intervals; back-to-back bookings are allowed |
| `test_booking.py` | allocation (skill match, fallback, dealership scoping), every error path, retry after a lost race, **two truly concurrent requests → exactly one booking** |
| `test_api.py` | HTTP status codes, error shape, time-zone handling, 503 on DB failure |
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
| Customer / vehicle | 1 Jane Doe / 1 VW Golf `WVWZZZ1JZXW000001` |

```bash
# 1. Book a brake job → 201, Alice + Bay A, 09:00–11:00
curl -X POST localhost:8000/appointments -H 'content-type: application/json' \
  -d '{"vehicle_id":1,"service_type_id":2,"dealership_id":1,"start_at":"2030-01-07T09:00:00+00:00"}'

# 2. Same request again → 409 {"error":{"code":"no_technician_available",...}} (only Alice does brakes)

# 3. Oil change at the same time → 201, falls back to Bob + Bay B
curl -X POST localhost:8000/appointments -H 'content-type: application/json' \
  -d '{"vehicle_id":1,"service_type_id":1,"dealership_id":1,"start_at":"2030-01-07T09:00:00+00:00"}'

# 4. Time without an offset → 422 validation_error ("Input should have timezone info")
curl -X POST localhost:8000/appointments -H 'content-type: application/json' \
  -d '{"vehicle_id":1,"service_type_id":1,"dealership_id":1,"start_at":"2030-01-07T09:00:00"}'

# 5. Read back
curl localhost:8000/appointments/1
curl "localhost:8000/appointments?dealership_id=1&date=2030-01-07"

# 6. Operations
curl localhost:8000/health/ready
curl localhost:8000/metrics | grep appointment
```

Error codes: `not_found` 404 · `validation_error`, `start_in_past`, `outside_business_hours` 422 ·
`no_technician_available`, `no_bay_available`, `slot_taken` 409 · `internal_error` 500 ·
`service_unavailable` 503. All errors share one shape: `{"error": {"code": ..., "message": ...}}`.

## Project layout

```
src/keyloop_test/
  api.py            FastAPI routes, error mapping, /health, /metrics
  booking.py        business rules + transactional book_appointment (allocation, retry)
  models.py         SQLAlchemy models, exclusion constraints
  schemas.py        request/response models
  observability.py  JSON logging, request-id middleware, Prometheus metrics
  db.py             engine (pool + lock/statement timeouts), sessions, init_db
  seed.py           demo data + `python -m keyloop_test.seed`
tests/              pytest suite (real PostgreSQL)
docs/               system design
```

## Assumptions

See [System Design §2](docs/SYSTEM_DESIGN.md#2-assumptions). Highlights: duration comes from the
service type; technicians qualify by skill; the system picks the technician and bay; times must
carry a UTC offset; intervals are half-open, so back-to-back bookings are allowed.

## AI Collaboration Narrative

> **TODO(author):** write this in your own words — it is a primary evaluation criterion. The
> facts below are from the AI session log, to jog your memory; keep, cut or expand them.

### Strategy

- TODO(author): how you directed the agent (Claude Code): scenario analysis → design doc → review
  pass → test-first, task-by-task plan → inline execution with a commit per task.
- Scope was set explicitly and kept small (no auth, cancellation or slot search).

### Verification process

- Every task was test-first: each test was run and seen to fail before the code existed.
- The concurrency test passed 20/20 runs. Because a passing concurrency test can also mean the
  requests never actually overlapped, a separate probe confirmed that 30/30 runs hit a real
  lost race (a PostgreSQL `23P01` exclusion violation followed by a retry).
- The finished stack was run in Docker and exercised with curl, not only through the in-process
  test client.

### Where the AI was wrong and how it was caught

- **Plan assumed the wrong error:** the plan expected an inverted time range to hit the CHECK
  constraint. Running the test showed PostgreSQL rejects it earlier (building the generated
  range, `DataError`). The test was split: zero-length → CHECK, inverted → `DataError`.
- **`/metrics` redirected (307):** all in-process tests passed, because the test client follows
  redirects. The curl demo against the real container exposed it. A regression test was added
  first, then the endpoint was fixed.
- **Design review of the AI's first draft** found a liveness check that would restart-loop pods
  during a DB outage, an alert on a metric that would rarely fire, missing lock/pool timeouts and
  no DB guard against cross-dealership resources. All were fixed before coding.
- TODO(author): anything you caught yourself.

### Ensuring final quality

- TODO(author): your own review of the code, what you changed, and what you would do next.
