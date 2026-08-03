"""The heart of the system: seat allocation under concurrency.

THE ONE-SENTENCE VERSION (interview answer):
  Every booking runs inside a single database transaction that takes a row-level lock on the event
  row (`SELECT ... FOR UPDATE`) before checking and decrementing available_seats, so two
  simultaneous requests for the last seat are serialised by the database and the second one loses.

WHY pessimistic locking (FOR UPDATE) rather than optimistic locking (a version column + retry):
  - Seat contention on a hot event is HIGH, so optimistic locking would fail and retry constantly;
    pessimistic locking makes each waiter block once and then succeed.
  - The critical section is microseconds long (one indexed read + one write), so the lock is cheap.
  - Trade-off I'd own up to: it serialises ALL bookings for a given event, so a single event is a
    write bottleneck. The standard fix at scale is to shard the counter into N rows per event and
    have each request lock a random shard, or move allocation into Redis with a durable write-behind.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import Conflict, Forbidden, NotFound
from app.logging_config import get_logger
from app.models import Booking, BookingStatus, Event, User
from app.notifications import notify_booking_cancelled, notify_booking_confirmed

log = get_logger("booking")


def _lock_event(db: Session, event_id: int) -> Event:
    """Load the event row and hold a row-level write lock on it until the transaction ends."""
    stmt = select(Event).where(Event.id == event_id)
    if not settings.is_sqlite:
        # Postgres: takes an exclusive row lock. Other transactions touching THIS event block here;
        # bookings for other events are completely unaffected (row-level, not table-level).
        stmt = stmt.with_for_update()
    # On SQLite the equivalent guarantee comes from BEGIN IMMEDIATE (see database.py).
    event = db.execute(stmt).scalar_one_or_none()
    if not event:
        raise NotFound(f"Event {event_id} not found")
    return event


def _find_by_idempotency_key(db: Session, user_id: int, key: str) -> Booking | None:
    return db.execute(
        select(Booking).where(Booking.user_id == user_id, Booking.idempotency_key == key)
    ).scalar_one_or_none()


def create_booking(
    db: Session,
    *,
    user: User,
    event_id: int,
    seats: int,
    idempotency_key: str | None = None,
) -> tuple[Booking, bool]:
    """Returns (booking, created). `created=False` means an idempotent replay was served.

    WHY idempotency: a client that times out and retries must not be charged twice. The key is
    supplied by the CLIENT (it knows which retries are the "same" logical request) and enforced by
    a UNIQUE index, so correctness doesn't depend on a read-then-write check that could race.
    """
    if idempotency_key:
        # Fast path: an obvious replay, answered without touching the event row at all.
        existing = _find_by_idempotency_key(db, user.id, idempotency_key)
        if existing:
            return existing, False

    try:
        # --- critical section begins: nothing else may modify this event's seat count ---
        event = _lock_event(db, event_id)

        if event.available_seats < seats:
            # We only get here holding the lock, so this read is guaranteed current.
            raise Conflict(
                f"Only {event.available_seats} seat(s) left for '{event.name}', requested {seats}",
                code="insufficient_seats",
            )

        event.available_seats -= seats
        booking = Booking(
            user_id=user.id,
            event_id=event.id,
            seats=seats,
            status=BookingStatus.CONFIRMED,
            unit_price=event.price,
            idempotency_key=idempotency_key,
        )
        db.add(booking)
        db.commit()  # lock released here, atomically with the seat decrement
        # --- critical section ends ---
    except IntegrityError:
        # Two concurrent retries with the SAME idempotency key: one wins, the other trips the
        # UNIQUE index. That's the correct outcome - we just return the winner's booking.
        db.rollback()
        if idempotency_key:
            existing = _find_by_idempotency_key(db, user.id, idempotency_key)
            if existing:
                return existing, False
        raise
    except Exception:
        db.rollback()  # WHY: a failed transaction must never leave seats "reserved" in limbo.
        raise

    db.refresh(booking)
    log.info("booking_created", booking_id=booking.id, event_id=event_id, seats=seats, user_id=user.id)
    notify_booking_confirmed(booking)
    return booking, True


def modify_booking(db: Session, *, user: User, booking_id: int, new_seats: int) -> Booking:
    """Change seat count on an existing booking, re-validating availability for any increase."""
    try:
        booking = db.get(Booking, booking_id)
        if not booking:
            raise NotFound(f"Booking {booking_id} not found")
        _authorize(user, booking)
        if booking.status == BookingStatus.CANCELLED:
            raise Conflict("Cannot modify a cancelled booking", code="booking_cancelled")

        # WHY lock the event even when shrinking the booking: releasing seats is still a write to
        # the shared counter, and it must not interleave with a concurrent booking's read-modify-write.
        event = _lock_event(db, booking.event_id)

        delta = new_seats - booking.seats
        if delta == 0:
            db.rollback()
            return booking
        if delta > 0 and event.available_seats < delta:
            raise Conflict(
                f"Only {event.available_seats} additional seat(s) available", code="insufficient_seats"
            )

        event.available_seats -= delta  # negative delta correctly gives seats back
        booking.seats = new_seats
        # WHY MODIFIED rather than staying CONFIRMED: the status doubles as an audit trail that this
        # booking was changed after purchase. A fuller design would keep an append-only history table.
        booking.status = BookingStatus.MODIFIED
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(booking)
    log.info("booking_modified", booking_id=booking.id, new_seats=new_seats, delta=delta)
    return booking


def cancel_booking(db: Session, *, user: User, booking_id: int) -> Booking:
    """Cancel and return the seats to inventory, in one transaction."""
    try:
        booking = db.get(Booking, booking_id)
        if not booking:
            raise NotFound(f"Booking {booking_id} not found")
        _authorize(user, booking)
        if booking.status == BookingStatus.CANCELLED:
            # WHY 409 and not a silent 200: cancelling twice is a real client bug worth surfacing,
            # and returning the seats twice would corrupt inventory. This check runs under the lock.
            raise Conflict("Booking is already cancelled", code="already_cancelled")

        event = _lock_event(db, booking.event_id)
        event.available_seats += booking.seats
        booking.status = BookingStatus.CANCELLED
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(booking)
    log.info("booking_cancelled", booking_id=booking.id, seats_released=booking.seats)
    notify_booking_cancelled(booking)
    return booking


def _authorize(user: User, booking: Booking) -> None:
    # WHY an ownership check and not just "is authenticated": otherwise any logged-in user could
    # cancel anyone's booking by guessing an ID (IDOR - the most common real-world API bug).
    if booking.user_id != user.id and not user.is_admin:
        raise Forbidden("You do not own this booking")
