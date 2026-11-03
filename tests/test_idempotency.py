import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from keyloop_test import db
from keyloop_test.booking import (
    BookingRequest, IdempotencyKeyReused, OutsideBusinessHours, book_idempotently,
)
from keyloop_test.models import Appointment

NOW = datetime(2030, 1, 7, tzinfo=UTC)
OIL_9AM = BookingRequest(vehicle_id=1, service_type_id=1, dealership_id=1,
                         start_at=datetime(2030, 1, 7, 9, tzinfo=UTC))


def appointment_count():
    with db.SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(Appointment))


def test_same_key_replays_without_second_booking(session, seeded):
    first, replayed1 = book_idempotently(session, OIL_9AM, "k-1", "hash-a", now=NOW)
    again, replayed2 = book_idempotently(session, OIL_9AM, "k-1", "hash-a", now=NOW)
    assert (replayed1, replayed2) == (False, True)
    assert again.id == first.id
    assert appointment_count() == 1  # without the key, the retry would book Bob as well


def test_key_reused_with_different_request_rejected(session, seeded):
    book_idempotently(session, OIL_9AM, "k-1", "hash-a", now=NOW)
    with pytest.raises(IdempotencyKeyReused) as e:
        book_idempotently(session, OIL_9AM, "k-1", "hash-b", now=NOW)
    assert (e.value.code, e.value.status) == ("idempotency_key_reused", 422)


def test_failed_booking_does_not_consume_key(session, seeded):
    late = BookingRequest(1, 1, 1, datetime(2030, 1, 7, 17, 30, tzinfo=UTC))
    with pytest.raises(OutsideBusinessHours):
        book_idempotently(session, late, "k-1", "hash-late", now=NOW)
    _, replayed = book_idempotently(session, OIL_9AM, "k-1", "hash-a", now=NOW)
    assert replayed is False


def test_concurrent_same_key_books_once(engine, seeded):
    barrier, results = threading.Barrier(2), []

    def worker():
        with db.SessionLocal() as s:
            barrier.wait()
            appointment, replayed = book_idempotently(s, OIL_9AM, "k-1", "hash-a", now=NOW)
            results.append((appointment.id, replayed))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r[1] for r in results) == [False, True]
    assert results[0][0] == results[1][0]
    assert appointment_count() == 1
