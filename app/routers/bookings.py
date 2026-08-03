# WHY the router is this thin: all the concurrency-sensitive logic lives in services/booking.py so
# it can be unit-tested without HTTP, and so there's exactly one place that mutates seat counts.
from typing import Annotated

from fastapi import APIRouter, Header, Response, status
from sqlalchemy import select

from app.deps import CurrentUser, DbSession
from app.errors import Forbidden, NotFound
from app.models import Booking
from app.schemas import BookingCreate, BookingOut, BookingUpdate
from app.services import booking as booking_service

router = APIRouter(tags=["bookings"])


@router.post("/bookings", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
def create_booking(
    payload: BookingCreate,
    db: DbSession,
    user: CurrentUser,
    response: Response,
    # WHY a header and not a body field: Idempotency-Key is the industry convention (Stripe, PayPal)
    # and it keeps the retry-control plane separate from the business payload.
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=80)] = None,
):
    bk, created = booking_service.create_booking(
        db, user=user, event_id=payload.event_id, seats=payload.seats, idempotency_key=idempotency_key
    )
    if not created:
        # 200 instead of 201 tells the client "this already existed, I didn't create anything new".
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"
    return bk


@router.patch("/bookings/{booking_id}", response_model=BookingOut)
def modify_booking(booking_id: int, payload: BookingUpdate, db: DbSession, user: CurrentUser):
    # WHY PATCH not PUT: we're partially updating one field, not replacing the whole resource.
    return booking_service.modify_booking(db, user=user, booking_id=booking_id, new_seats=payload.seats)


@router.delete("/bookings/{booking_id}", response_model=BookingOut)
def cancel_booking(booking_id: int, db: DbSession, user: CurrentUser):
    # WHY a soft cancel (status=CANCELLED) instead of deleting the row: bookings are financial
    # records. You need them for refunds, disputes and revenue reporting long after cancellation.
    # We return the updated booking rather than 204 so the client can see the new state immediately.
    return booking_service.cancel_booking(db, user=user, booking_id=booking_id)


@router.get("/bookings/{booking_id}", response_model=BookingOut)
def get_booking(booking_id: int, db: DbSession, user: CurrentUser):
    bk = db.get(Booking, booking_id)
    if not bk:
        raise NotFound(f"Booking {booking_id} not found")
    if bk.user_id != user.id and not user.is_admin:
        raise Forbidden("You do not own this booking")
    return bk


@router.get("/users/{user_id}/bookings", response_model=list[BookingOut])
def list_user_bookings(user_id: int, db: DbSession, user: CurrentUser):
    # Same IDOR guard as above: you may only read your own bookings unless you're an admin.
    if user_id != user.id and not user.is_admin:
        raise Forbidden("You may only view your own bookings")
    stmt = select(Booking).where(Booking.user_id == user_id).order_by(Booking.created_at.desc())
    return db.execute(stmt).scalars().all()
