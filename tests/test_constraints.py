from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import DataError, IntegrityError

from keyloop_test.models import Appointment


def T(h: int, m: int = 0) -> datetime:
    return datetime(2030, 1, 7, h, m, tzinfo=UTC)


def _appt(tech, bay, start, end, dealership=1):
    return Appointment(
        dealership_id=dealership, customer_id=1, vehicle_id=1, service_type_id=1,
        technician_id=tech, service_bay_id=bay, start_at=start, end_at=end,
    )


def _commit_fails(session, sqlstate):
    with pytest.raises(IntegrityError) as e:
        session.commit()
    assert e.value.orig.sqlstate == sqlstate
    session.rollback()


def test_overlapping_technician_rejected(session, seeded):
    session.add(_appt(1, 1, T(9), T(10)))
    session.commit()
    session.add(_appt(1, 2, T(9, 30), T(10, 30)))
    _commit_fails(session, "23P01")


def test_overlapping_bay_rejected(session, seeded):
    session.add(_appt(1, 1, T(9), T(10)))
    session.commit()
    session.add(_appt(2, 1, T(9, 30), T(10, 30)))
    _commit_fails(session, "23P01")


def test_back_to_back_allowed(session, seeded):
    session.add(_appt(1, 1, T(9), T(10)))
    session.commit()
    session.add(_appt(1, 1, T(10), T(11)))
    session.commit()


def test_zero_length_rejected(session, seeded):
    # an empty range overlaps nothing, so only the CHECK stops it
    session.add(_appt(1, 1, T(10), T(10)))
    _commit_fails(session, "23514")


def test_end_before_start_rejected(session, seeded):
    session.add(_appt(1, 1, T(10), T(9)))
    with pytest.raises(DataError):  # the generated tstzrange refuses inverted bounds
        session.commit()


def test_cross_dealership_technician_rejected(session, seeded):
    session.add(_appt(3, 1, T(9), T(10)))  # Chen works at dealership 2
    _commit_fails(session, "23503")
