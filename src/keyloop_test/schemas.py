from datetime import UTC, date, datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer

MAX_ID = 2**31 - 1  # Postgres integer
# far-future bound so date arithmetic can't overflow; business horizons are out of scope
MAX_START = datetime(9999, 1, 1, tzinfo=UTC)
MAX_DATE = date(9999, 1, 1)
DbId = Annotated[int, Field(gt=0, le=MAX_ID)]


class AppointmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: DbId
    service_type_id: DbId
    dealership_id: DbId
    start_at: Annotated[AwareDatetime, Field(lt=MAX_START)]


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
