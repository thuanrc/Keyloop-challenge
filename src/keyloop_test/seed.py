"""Demo data. Ids are assigned in insertion order (1, 2, 3...) on an empty database."""

from datetime import time

from sqlalchemy.orm import Session

from keyloop_test.models import (
    Customer, Dealership, ServiceBay, ServiceType, Technician, Vehicle,
)


def seed(session: Session) -> None:
    groups = [
        [Dealership(name="Keyloop Motors London", timezone="Europe/London",
                    opens_at=time(8), closes_at=time(18)),
         Dealership(name="Keyloop Motors Hanoi", timezone="Asia/Ho_Chi_Minh",
                    opens_at=time(8), closes_at=time(17))],
        [ServiceType(name="Oil change", duration_minutes=60, required_skill="general"),
         ServiceType(name="Brake replacement", duration_minutes=120, required_skill="brakes"),
         ServiceType(name="EV battery check", duration_minutes=90, required_skill="ev")],
        [Technician(dealership_id=1, name="Alice", skills=["general", "brakes"]),
         Technician(dealership_id=1, name="Bob", skills=["general"]),
         Technician(dealership_id=2, name="Chen", skills=["ev", "general", "brakes"])],
        [ServiceBay(dealership_id=1, name="Bay A"),
         ServiceBay(dealership_id=1, name="Bay B"),
         ServiceBay(dealership_id=2, name="Bay 1")],
        [Customer(name="Jane Doe", email="jane@example.com")],
        [Vehicle(customer_id=1, vin="WVWZZZ1JZXW000001", make="VW", model="Golf", year=2021)],
    ]
    for group in groups:
        for row in group:  # one flush per row keeps ids in listed order
            session.add(row)
            session.flush()
    session.commit()  # commit so other sessions (e.g. concurrent bookings) see it
