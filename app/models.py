# WHY these three tables: they are the minimum that makes the seat-count invariant enforceable
# inside a single transaction. Everything else (payments, seat maps, venues) is deliberately out
# of scope for a 2-hour build.
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BookingStatus(str, enum.Enum):
    # WHY str+Enum: serialises straight to JSON and is validated by Pydantic for free.
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    MODIFIED = "MODIFIED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    # WHY store only the hash: a DB leak must not expose passwords. Hashing is in security.py.
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    # WHY a boolean flag not a full RBAC table: one privileged action (create events) doesn't
    # justify a roles/permissions schema. I'd introduce roles the moment there were 2+ of them.
    is_admin: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    bookings: Mapped[list["Booking"]] = relationship(back_populates="user")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    venue: Mapped[str] = mapped_column(String(200), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    total_seats: Mapped[int] = mapped_column(Integer, nullable=False)
    # WHY a denormalised available_seats column instead of deriving SUM(bookings.seats) every time:
    # it turns "is there room?" into a single row I can lock. Deriving it would mean locking every
    # booking row for the event. The cost is that it must ONLY ever be mutated inside the locked
    # transaction in services/booking.py - the CheckConstraint below is the safety net.
    available_seats: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # WHY a DB-level constraint: the last line of defence. Even if application logic had a
        # concurrency bug, Postgres itself refuses to persist an oversold event.
        CheckConstraint("available_seats >= 0", name="ck_events_available_non_negative"),
        CheckConstraint("available_seats <= total_seats", name="ck_events_available_lte_total"),
        CheckConstraint("total_seats > 0", name="ck_events_total_positive"),
    )

    bookings: Mapped[list["Booking"]] = relationship(back_populates="event")


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False, index=True)
    seats: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, native_enum=False, length=20), default=BookingStatus.CONFIRMED, nullable=False
    )
    # WHY snapshot the unit price on the booking: if an event's price changes later, historical
    # revenue must not change with it. This is the standard "record the price at time of sale" rule.
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # WHY nullable + unique-per-user: idempotency key sent by the client on retry. See bookings.py.
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint("seats > 0", name="ck_bookings_seats_positive"),
        # WHY scoped to user_id: two different users may legitimately generate the same key.
        # This UNIQUE index is what actually makes retries safe under concurrency - the DB rejects
        # the duplicate, we don't rely on a read-then-write check that could race.
        UniqueConstraint("user_id", "idempotency_key", name="uq_bookings_user_idempotency"),
        Index("ix_bookings_event_status", "event_id", "status"),
    )

    user: Mapped["User"] = relationship(back_populates="bookings")
    event: Mapped["Event"] = relationship(back_populates="bookings")

    @property
    def total_price(self) -> float:
        return float(self.unit_price) * self.seats
