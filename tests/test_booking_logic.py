"""Unit tests for seat allocation - the part of the system where a bug costs real money.

WHY these test the service layer directly rather than going through HTTP: it isolates the
concurrency behaviour from routing, auth and serialisation, so a failure here is unambiguous.
"""

import threading

import pytest

from app.database import SessionLocal
from app.errors import Conflict, Forbidden, NotFound
from app.models import Booking, BookingStatus, Event, User
from app.services import booking as svc


def test_booking_decrements_seats(db, user, event):
    bk, created = svc.create_booking(db, user=user, event_id=event.id, seats=3)
    assert created is True
    assert bk.status == BookingStatus.CONFIRMED
    assert bk.seats == 3
    assert db.get(Event, event.id).available_seats == 7


def test_booking_snapshots_price(db, user, event):
    """Changing the event price later must not rewrite the price of an existing booking."""
    bk, _ = svc.create_booking(db, user=user, event_id=event.id, seats=2)
    db.get(Event, event.id).price = 999.0
    db.commit()
    assert float(db.get(Booking, bk.id).unit_price) == 100.0


def test_cannot_book_more_than_available(db, user, event):
    with pytest.raises(Conflict) as exc:
        svc.create_booking(db, user=user, event_id=event.id, seats=11)
    assert exc.value.code == "insufficient_seats"
    # The failed attempt must leave inventory untouched - no partial allocation.
    assert db.get(Event, event.id).available_seats == 10


def test_booking_unknown_event_raises_404(db, user):
    with pytest.raises(NotFound):
        svc.create_booking(db, user=user, event_id=99999, seats=1)


def test_cancel_restores_seats(db, user, event):
    bk, _ = svc.create_booking(db, user=user, event_id=event.id, seats=4)
    assert db.get(Event, event.id).available_seats == 6
    svc.cancel_booking(db, user=user, booking_id=bk.id)
    assert db.get(Event, event.id).available_seats == 10
    assert db.get(Booking, bk.id).status == BookingStatus.CANCELLED


def test_double_cancel_rejected(db, user, event):
    """Guards the specific bug where cancelling twice would return the seats to inventory twice."""
    bk, _ = svc.create_booking(db, user=user, event_id=event.id, seats=4)
    svc.cancel_booking(db, user=user, booking_id=bk.id)
    with pytest.raises(Conflict):
        svc.cancel_booking(db, user=user, booking_id=bk.id)
    assert db.get(Event, event.id).available_seats == 10  # not 14


def test_modify_increase_and_decrease(db, user, event):
    bk, _ = svc.create_booking(db, user=user, event_id=event.id, seats=2)
    svc.modify_booking(db, user=user, booking_id=bk.id, new_seats=5)
    assert db.get(Event, event.id).available_seats == 5
    svc.modify_booking(db, user=user, booking_id=bk.id, new_seats=1)
    assert db.get(Event, event.id).available_seats == 9
    assert db.get(Booking, bk.id).status == BookingStatus.MODIFIED


def test_modify_beyond_capacity_rejected(db, user, event):
    bk, _ = svc.create_booking(db, user=user, event_id=event.id, seats=2)
    with pytest.raises(Conflict):
        svc.modify_booking(db, user=user, booking_id=bk.id, new_seats=50)
    assert db.get(Event, event.id).available_seats == 8  # unchanged by the failed modify


def test_modify_cancelled_booking_rejected(db, user, event):
    bk, _ = svc.create_booking(db, user=user, event_id=event.id, seats=2)
    svc.cancel_booking(db, user=user, booking_id=bk.id)
    with pytest.raises(Conflict):
        svc.modify_booking(db, user=user, booking_id=bk.id, new_seats=3)


def test_cannot_touch_another_users_booking(db, user, other_user, event):
    """IDOR guard: owning the booking ID is not the same as owning the booking."""
    bk, _ = svc.create_booking(db, user=user, event_id=event.id, seats=1)
    with pytest.raises(Forbidden):
        svc.cancel_booking(db, user=other_user, booking_id=bk.id)


def test_idempotency_key_prevents_double_booking(db, user, event):
    first, c1 = svc.create_booking(db, user=user, event_id=event.id, seats=2, idempotency_key="abc")
    second, c2 = svc.create_booking(db, user=user, event_id=event.id, seats=2, idempotency_key="abc")
    assert c1 is True and c2 is False
    assert first.id == second.id
    assert db.get(Event, event.id).available_seats == 8  # charged once, not twice


def test_different_users_may_reuse_the_same_key(db, user, other_user, event):
    """The uniqueness constraint is scoped per user - two clients generating "req-1" must not clash."""
    a, _ = svc.create_booking(db, user=user, event_id=event.id, seats=1, idempotency_key="req-1")
    b, _ = svc.create_booking(db, user=other_user, event_id=event.id, seats=1, idempotency_key="req-1")
    assert a.id != b.id
    assert db.get(Event, event.id).available_seats == 8


# --------------------------------------------------------------------------------------
# THE headline test: concurrent bookings must never oversell.
# --------------------------------------------------------------------------------------
def test_concurrent_bookings_never_oversell(db, user, event):
    """20 threads race for 10 seats, 1 seat each. Exactly 10 must win.

    Each thread gets its OWN SQLAlchemy session (i.e. its own DB connection), which is what makes
    this a real concurrency test rather than a sequential loop. Without the row lock this races:
    several threads read available_seats=1 and all of them decrement it.
    """
    THREADS, SEATS_EACH = 20, 1
    results: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(THREADS)  # release all threads at the same instant to maximise overlap

    # WHY we snapshot plain integers here: a SQLAlchemy Session (and every ORM object attached to
    # it) is NOT thread-safe. Handing `user`/`event` to 20 threads would have them all lazy-loading
    # through the main thread's session. Each worker must own its session end to end.
    user_id, event_id, total_seats = user.id, event.id, event.total_seats

    # WHY this rollback matters: the fixtures above left this session holding an open transaction,
    # and on SQLite that transaction holds the database-wide write lock (see database.py). Releasing
    # it first is the difference between measuring contention between the 20 workers and measuring
    # all 20 of them queueing behind the test itself.
    db.rollback()

    def attempt():
        session = SessionLocal()
        try:
            start.wait(timeout=10)
            me = session.get(User, user_id)
            svc.create_booking(session, user=me, event_id=event_id, seats=SEATS_EACH)
            outcome = "ok"
        except Conflict:
            outcome = "sold_out"
        except Exception as e:  # noqa: BLE001 - surfaced in the assertion message below
            outcome = f"error:{type(e).__name__}"
        finally:
            session.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    successes = results.count("ok")
    errors = [r for r in results if r.startswith("error")]

    assert not errors, f"unexpected errors during concurrent booking: {errors}"
    assert successes == 10, f"expected exactly 10 winners, got {successes}: {results}"

    db.expire_all()  # WHY: this session cached the event row before the threads ran.
    fresh = db.get(Event, event_id)
    assert fresh.available_seats == 0, "seats went negative or were leaked"

    # Cross-check the denormalised counter against the booking rows - they must agree.
    booked = sum(b.seats for b in db.query(Booking).filter(Booking.event_id == event_id).all())
    assert booked == total_seats == 10


def test_concurrent_idempotent_retries_book_once(db, user, event):
    """The nastier race: 10 threads replay the SAME idempotency key simultaneously.
    Only one booking row may exist, and only its seats may be deducted."""
    THREADS = 10
    start = threading.Barrier(THREADS)
    ids: list[int] = []
    lock = threading.Lock()
    user_id, event_id = user.id, event.id  # see the threading note in the test above
    db.rollback()  # release the fixture's write lock - see the note in the test above

    def attempt():
        session = SessionLocal()
        try:
            start.wait(timeout=10)
            bk, _ = svc.create_booking(
                session, user=session.get(User, user_id), event_id=event_id,
                seats=2, idempotency_key="retry-me",
            )
            with lock:
                ids.append(bk.id)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(set(ids)) == 1, f"idempotency key produced multiple bookings: {set(ids)}"
    db.expire_all()
    assert db.get(Event, event_id).available_seats == 8
