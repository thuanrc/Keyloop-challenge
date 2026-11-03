import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from keyloop_test import db
from keyloop_test.booking import (
    BookingRequest, NoBayAvailable, NoTechnicianAvailable, NotFound, SlotTaken,
    VehicleAlreadyBooked, book_appointment,
)
from keyloop_test.models import Appointment, Technician


def T(h: int, m: int = 0) -> datetime:
    return datetime(2030, 1, 7, h, m, tzinfo=UTC)  # January: London == UTC


NOW = T(0)


def req(service_type_id: int, start: datetime, vehicle_id: int = 1, dealership_id: int = 1):
    return BookingRequest(vehicle_id=vehicle_id, service_type_id=service_type_id,
                          dealership_id=dealership_id, start_at=start)


@pytest.fixture
def book(session, seeded):
    return lambda st, start, **kw: book_appointment(session, req(st, start, **kw), now=NOW)


def test_books_with_qualified_tech_and_bay(session, book):
    a = book(2, T(9))
    assert (a.technician_id, a.service_bay_id, a.customer_id) == (1, 1, 1)  # only Alice has "brakes"
    assert a.end_at == T(11)
    assert session.get(Appointment, a.id) is not None


def test_second_overlapping_brake_job_no_technician(book):
    book(2, T(9))
    with pytest.raises(NoTechnicianAvailable):
        book(2, T(10), vehicle_id=2)


def test_oil_change_falls_back_to_other_tech(book):
    book(2, T(9))  # Alice + Bay A busy 9–11
    a = book(1, T(9), vehicle_id=2)
    assert (a.technician_id, a.service_bay_id) == (2, 2)


def test_no_bay_available(session, book):
    session.add(Technician(dealership_id=1, name="Dana", skills=["general"]))
    session.commit()
    book(1, T(9), vehicle_id=1)
    book(1, T(9), vehicle_id=2)
    with pytest.raises(NoBayAvailable):
        book(1, T(9), vehicle_id=3)


def test_back_to_back_same_resources(book):
    a, b = book(2, T(9)), book(2, T(11))
    assert a.technician_id == b.technician_id == 1


def test_other_dealership_resources_not_used(book):
    book(2, T(9))  # Alice busy; Chen (dealership 2) has brakes and is free
    with pytest.raises(NoTechnicianAvailable):
        book(2, T(9, 30), vehicle_id=2)


def test_unknown_vehicle_not_found(book):
    with pytest.raises(NotFound) as e:
        book(1, T(9), vehicle_id=999)
    assert e.value.code == "not_found"


def test_concurrent_bookings_one_wins(engine, seeded):
    barrier, results = threading.Barrier(2), []

    def worker(vehicle_id):
        with db.SessionLocal() as s:
            barrier.wait()
            try:
                results.append(book_appointment(s, req(2, T(9), vehicle_id=vehicle_id), now=NOW))
            except (NoTechnicianAvailable, SlotTaken) as e:
                results.append(e)

    threads = [threading.Thread(target=worker, args=(v,)) for v in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    names = sorted(type(r).__name__ for r in results)
    assert names in (["Appointment", "NoTechnicianAvailable"], ["Appointment", "SlotTaken"])
    with db.SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Appointment)) == 1


def test_retry_on_lost_race(session, seeded, monkeypatch):
    real_flush, raced = session.flush, []

    def flush_after_rival_commits(*a, **kw):
        # first flush of our appointment: a rival commits Alice 09:00–11:00 just before us
        if not raced and any(isinstance(o, Appointment) for o in session.new):
            raced.append(1)
            with db.SessionLocal() as rival:
                rival.add(Appointment(dealership_id=1, customer_id=2, vehicle_id=2,
                                      service_type_id=2, technician_id=1, service_bay_id=2,
                                      start_at=T(9), end_at=T(11)))
                rival.commit()
        return real_flush(*a, **kw)

    monkeypatch.setattr(session, "flush", flush_after_rival_commits)
    retries = []
    with pytest.raises(NoTechnicianAvailable):
        book_appointment(session, req(2, T(9)), now=NOW, on_retry=lambda: retries.append(1))
    assert retries == [1]


def test_vehicle_already_booked(book):
    book(1, T(9))  # oil change 9–10, Alice + Bay A
    with pytest.raises(VehicleAlreadyBooked) as e:
        book(2, T(9, 30))  # same car, overlapping
    assert (e.value.code, e.value.status) == ("vehicle_already_booked", 409)


def test_vehicle_back_to_back_allowed(book):
    book(1, T(9))
    assert book(1, T(10)).vehicle_id == 1


def test_lost_race_on_vehicle_is_not_retried(session, seeded, monkeypatch):
    # a rival books the SAME car (Bob, Bay B) just before our flush: retrying can't help
    real_flush, raced = session.flush, []

    def flush_after_rival_commits(*a, **kw):
        if not raced and any(isinstance(o, Appointment) for o in session.new):
            raced.append(1)
            with db.SessionLocal() as rival:
                rival.add(Appointment(dealership_id=1, customer_id=1, vehicle_id=1,
                                      service_type_id=1, technician_id=2, service_bay_id=2,
                                      start_at=T(9), end_at=T(10)))
                rival.commit()
        return real_flush(*a, **kw)

    monkeypatch.setattr(session, "flush", flush_after_rival_commits)
    retries = []
    with pytest.raises(VehicleAlreadyBooked):
        book_appointment(session, req(1, T(9)), now=NOW, on_retry=lambda: retries.append(1))
    assert retries == []
