from datetime import datetime, time

from sqlalchemy import (
    ARRAY, CheckConstraint, Computed, DateTime, ForeignKey, ForeignKeyConstraint, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Dealership(Base):
    __tablename__ = "dealerships"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    opens_at: Mapped[time]
    closes_at: Mapped[time]


class ServiceType(Base):
    __tablename__ = "service_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    duration_minutes: Mapped[int] = mapped_column(CheckConstraint("duration_minutes > 0"))
    required_skill: Mapped[str] = mapped_column(Text)


class Technician(Base):
    __tablename__ = "technicians"
    __table_args__ = (UniqueConstraint("id", "dealership_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id"))
    name: Mapped[str] = mapped_column(Text)
    skills: Mapped[list[str]] = mapped_column(ARRAY(Text))


class ServiceBay(Base):
    __tablename__ = "service_bays"
    __table_args__ = (UniqueConstraint("id", "dealership_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id"))
    name: Mapped[str] = mapped_column(Text)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True)


class Vehicle(Base):
    __tablename__ = "vehicles"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    vin: Mapped[str] = mapped_column(Text, unique=True)
    make: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    year: Mapped[int]


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="end_after_start"),
        ExcludeConstraint(("technician_id", "="), ("period", "&&"), using="gist",
                          name="no_technician_overlap"),
        ExcludeConstraint(("service_bay_id", "="), ("period", "&&"), using="gist",
                          name="no_bay_overlap"),
        ForeignKeyConstraint(["technician_id", "dealership_id"],
                             ["technicians.id", "technicians.dealership_id"]),
        ForeignKeyConstraint(["service_bay_id", "dealership_id"],
                             ["service_bays.id", "service_bays.dealership_id"]),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"))
    service_type_id: Mapped[int] = mapped_column(ForeignKey("service_types.id"))
    technician_id: Mapped[int]
    service_bay_id: Mapped[int]
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period: Mapped[Range[datetime]] = mapped_column(
        TSTZRANGE, Computed("tstzrange(start_at, end_at, '[)')", persisted=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IdempotencyKey(Base):
    """Client-supplied key → the appointment its first successful request created."""

    __tablename__ = "idempotency_keys"
    # shortcut: keys never expire; add a TTL cleanup (e.g. delete rows older than 24 h) when volume grows
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(Text)
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
