"""Analytics computed with SQL aggregation directly against the OLTP tables.

WHY that's the right call at this size: the data is small, it's already relational, and the DB does
GROUP BY far faster than pulling rows into Python. No pipeline to maintain, no staleness.

WHY it would NOT survive scale (the honest follow-up): these queries do full scans over `bookings`
and compete with customer-facing writes for the same connections and buffer cache. The evolution
path is: (1) point them at a read replica, (2) CDC the tables into a warehouse (Snowflake/BigQuery)
with Debezium, (3) model them as dbt tables on a schedule, (4) serve BI from there. The endpoint
contract below wouldn't have to change - only what's behind it.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import Date, cast, func, select

from app.config import settings
from app.deps import AdminUser, DbSession
from app.models import Booking, BookingStatus, Event
from app.schemas import BookingsOverTimeRow, OccupancyRow, RevenueRow

router = APIRouter(prefix="/analytics", tags=["analytics"])

# WHY exclude CANCELLED everywhere: cancelled seats were released, so counting them would overstate
# both revenue and occupancy. MODIFIED bookings are live bookings, so they stay in.
ACTIVE = Booking.status != BookingStatus.CANCELLED


def _day_expr():
    """Portability seam: the one piece of SQL that genuinely differs between our two engines."""
    if settings.is_sqlite:
        return func.date(Booking.created_at)
    return cast(Booking.created_at, Date)


@router.get("/revenue", response_model=list[RevenueRow])
def revenue(db: DbSession, _: AdminUser):
    """Gross revenue per event. Uses the price snapshotted on each booking, not the event's current
    price, so re-pricing an event never rewrites history."""
    stmt = (
        select(
            Event.id,
            Event.name,
            func.coalesce(func.sum(Booking.seats), 0),
            func.coalesce(func.sum(Booking.seats * Booking.unit_price), 0),
        )
        # LEFT JOIN so events that sold nothing still show up as a zero row - a report that silently
        # drops the worst-performing events is a misleading report.
        .outerjoin(Booking, (Booking.event_id == Event.id) & ACTIVE)
        .group_by(Event.id, Event.name)
        .order_by(func.coalesce(func.sum(Booking.seats * Booking.unit_price), 0).desc())
    )
    return [
        RevenueRow(event_id=r[0], event_name=r[1], tickets_sold=int(r[2]), revenue=float(r[3]))
        for r in db.execute(stmt).all()
    ]


@router.get("/occupancy", response_model=list[OccupancyRow])
def occupancy(db: DbSession, _: AdminUser):
    """Seats sold vs capacity. Derived from the booking rows rather than from Event.available_seats
    on purpose: bookings are the source of truth, so if this ever disagreed with the denormalised
    counter it would tell me the counter had drifted - a built-in consistency check."""
    stmt = (
        select(Event.id, Event.name, Event.total_seats, func.coalesce(func.sum(Booking.seats), 0))
        .outerjoin(Booking, (Booking.event_id == Event.id) & ACTIVE)
        .group_by(Event.id, Event.name, Event.total_seats)
        .order_by(Event.id)
    )
    rows = []
    for event_id, name, total, sold in db.execute(stmt).all():
        sold = int(sold)
        rows.append(
            OccupancyRow(
                event_id=event_id,
                event_name=name,
                total_seats=total,
                seats_sold=sold,
                occupancy_pct=round(sold / total * 100, 2) if total else 0.0,
            )
        )
    return rows


@router.get("/bookings-over-time", response_model=list[BookingsOverTimeRow])
def bookings_over_time(db: DbSession, _: AdminUser, days: int = Query(30, ge=1, le=365)):
    """Daily booking counts for a trend line.

    WHY bounded by `days`: an unbounded time-series query gets slower forever. The index on
    bookings.created_at makes this a range scan rather than a full table scan.
    """
    since = date.today() - timedelta(days=days)
    day = _day_expr()
    stmt = (
        select(day.label("day"), func.count(Booking.id), func.coalesce(func.sum(Booking.seats), 0))
        .where(ACTIVE, Booking.created_at >= since)
        .group_by(day)
        .order_by(day)
    )
    return [
        BookingsOverTimeRow(date=str(d)[:10], bookings=int(c), seats=int(s))
        for d, c, s in db.execute(stmt).all()
    ]
