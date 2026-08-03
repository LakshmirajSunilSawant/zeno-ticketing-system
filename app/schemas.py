# WHY Pydantic schemas separate from ORM models: the DB shape and the public API contract should
# be able to change independently, and it guarantees I never accidentally serialise hashed_password.
# Every constraint here (gt, le, max_length) is a validation FastAPI turns into an automatic 422.
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import BookingStatus

# ---------------------------------------------------------------- auth


class UserCreate(BaseModel):
    email: EmailStr  # WHY EmailStr: format validation for free, rejected at the edge with 422.
    # WHY max_length=72: bcrypt silently truncates beyond 72 bytes. Rejecting loudly is safer than
    # letting a user think 100 chars of password are protecting them.
    password: str = Field(min_length=8, max_length=72)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: EmailStr
    is_admin: bool
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


# ---------------------------------------------------------------- events


class EventCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    venue: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    total_seats: int = Field(gt=0, le=1_000_000)
    # WHY float on the wire but Numeric(10,2) in the DB: JSON has no decimal type. The DB stays
    # exact so money maths never drifts. At real scale I'd send integer minor units (paise) instead.
    price: float = Field(ge=0)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    venue: str
    starts_at: datetime
    total_seats: int
    available_seats: int
    price: float


# ---------------------------------------------------------------- bookings


class BookingCreate(BaseModel):
    event_id: int
    # WHY an upper bound: without le=, a single request for 10**9 seats is a trivial abuse vector.
    seats: int = Field(gt=0, le=10)


class BookingUpdate(BaseModel):
    seats: int = Field(gt=0, le=10)


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    event_id: int
    seats: int
    status: BookingStatus
    unit_price: float
    total_price: float
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------- analytics


class RevenueRow(BaseModel):
    event_id: int
    event_name: str
    tickets_sold: int
    revenue: float


class OccupancyRow(BaseModel):
    event_id: int
    event_name: str
    total_seats: int
    seats_sold: int
    occupancy_pct: float


class BookingsOverTimeRow(BaseModel):
    date: str
    bookings: int
    seats: int


# ---------------------------------------------------------------- errors


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """WHY a single envelope: clients parse one shape for every failure, and it documents itself
    in the OpenAPI spec instead of clients guessing."""

    error: ErrorBody
