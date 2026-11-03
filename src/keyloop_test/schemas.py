from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, PositiveInt, field_serializer


class AppointmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: PositiveInt
    service_type_id: PositiveInt
    dealership_id: PositiveInt
    start_at: AwareDatetime


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    dealership_id: int
    customer_id: int
    vehicle_id: int
    service_type_id: int
    technician_id: int
    service_bay_id: int
    start_at: datetime
    end_at: datetime
    created_at: datetime

    @field_serializer("start_at", "end_at", "created_at")
    def _utc(self, value: datetime) -> datetime:
        return value.astimezone(UTC)
