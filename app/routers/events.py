# WHY reads are public but writes are admin-only: browsing the catalogue is the highest-traffic,
# lowest-risk operation, and requiring a login to see what's on sale would be hostile.
from fastapi import APIRouter, Query, status
from sqlalchemy import select

from app.deps import AdminUser, DbSession
from app.errors import NotFound
from app.models import Event
from app.schemas import EventCreate, EventOut

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def create_event(payload: EventCreate, db: DbSession, _: AdminUser):
    # available_seats starts equal to total_seats; from here on ONLY the booking service moves it.
    event = Event(**payload.model_dump(), available_seats=payload.total_seats)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@router.get("", response_model=list[EventOut])
def list_events(
    db: DbSession,
    # WHY pagination from day one: an unbounded list endpoint is a guaranteed outage once the table
    # grows. Cheap to add now, painful to retrofit after clients depend on the un-paginated shape.
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    stmt = select(Event).order_by(Event.starts_at).limit(limit).offset(offset)
    return db.execute(stmt).scalars().all()


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: int, db: DbSession):
    event = db.get(Event, event_id)  # primary-key lookup, uses the PK index
    if not event:
        raise NotFound(f"Event {event_id} not found")
    return event
