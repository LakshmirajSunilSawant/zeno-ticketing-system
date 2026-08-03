# WHY: one place that knows about the database engine, so swapping SQLite <-> Postgres is a
# single env var. Sync SQLAlchemy (not async) is a deliberate choice: `SELECT ... FOR UPDATE`
# row locking is straightforward and easy to reason about, and FastAPI runs sync endpoints in a
# threadpool, so we still serve concurrent requests on separate DB connections.
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """WHY DeclarativeBase: SQLAlchemy 2.0 style, gives typed models without extra plumbing."""


def _engine_kwargs() -> dict:
    if settings.is_sqlite:
        # check_same_thread=False: FastAPI's threadpool touches the connection from many threads.
        return {"connect_args": {"check_same_thread": False}}
    # pool_pre_ping: Render's free Postgres drops idle connections; this transparently reconnects
    # instead of surfacing a "server closed the connection unexpectedly" error to the client.
    return {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10}


engine = create_engine(settings.sqlalchemy_url, **_engine_kwargs())


if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        # WHY: SQLite ignores foreign keys unless asked, and WAL + a busy timeout make the
        # concurrency tests behave like a real DB instead of instantly raising "database is locked".
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        # 15s: under the locust run (40 concurrent users) a 5s timeout was exceeded and SQLite
        # returned "database is locked" as a 500. That is purely an artefact of SQLite's ONE
        # database-wide write lock - on Postgres, registrations write to `users` and bookings lock
        # a row in `events`, so they never contend at all. Raising the timeout makes the local
        # load-test demo clean without pretending the two engines behave the same.
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()
        # Disable pysqlite's implicit-BEGIN behaviour so we can issue our own (see below).
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _sqlite_begin_immediate(conn):
        # WHY BEGIN IMMEDIATE: SQLite has no `SELECT ... FOR UPDATE`. A plain transaction starts as
        # a deferred *read* lock, so two bookings could both read "1 seat left" and then both write.
        # BEGIN IMMEDIATE takes the write lock up front, which gives the same mutual exclusion that
        # FOR UPDATE gives us on Postgres. This is what keeps the local locust demo honest.
        #
        # TRADE-OFF I'd own in the interview: this is coarser than Postgres row locking - it
        # serialises EVERY transaction against the whole database file, not just bookings for one
        # event, so a long-running read blocks writers. That's acceptable precisely because SQLite
        # is only the dev/test engine here; production runs Postgres, where `with_for_update()`
        # locks a single event row and bookings for different events never contend at all.
        conn.exec_driver_sql("BEGIN IMMEDIATE")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """WHY a dependency: FastAPI opens one session per request and always closes it, even on error."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    # WHY create_all instead of Alembic: this is a 2-hour build with one schema version.
    # In production I'd use Alembic migrations so schema changes are versioned and reviewable.
    from app import models  # noqa: F401  (import registers the mappers before create_all)

    Base.metadata.create_all(bind=engine)
