# WHY the env var is set BEFORE importing app: app.config reads settings at import time, so this
# is the only reliable way to point the whole app at a throwaway database.
#
# WHY a temp FILE rather than ":memory:": an in-memory SQLite database lives inside a single
# connection, so every thread would have to share one connection - which would make the concurrency
# test pass for the wrong reason. A temp file gives each connection its own real handle, so the
# oversell test actually exercises the locking. It's still created and destroyed per test session,
# so isolation is unchanged.
import os
import tempfile
from pathlib import Path

import pytest

_TMP_DB = Path(tempfile.gettempdir()) / "zeno_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ["JWT_SECRET"] = "test-secret"
os.environ["RATE_LIMIT"] = "10000/minute"  # WHY: rate limiting is tested on its own, not everywhere.

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Event, User  # noqa: E402
from app.security import hash_password  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    """WHY drop+create per test: every test starts from a known-empty schema, so tests can't leak
    state into each other and can be run in any order (or in parallel with pytest-xdist)."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


def _insert(obj):
    """Create a row in its OWN short-lived session, then close it.

    WHY this matters and isn't just style: on SQLite every transaction takes the database write
    lock (see database.py). A fixture that kept its session open would hold that lock for the whole
    test, and any HTTP request the test then made would block until it timed out. Committing and
    closing immediately is the correct discipline anyway - a fixture should leave no transaction
    open. The returned object is detached but still readable because the session is configured with
    expire_on_commit=False.
    """
    session = SessionLocal()
    try:
        session.add(obj)
        session.commit()
        session.expunge(obj)
        return obj
    finally:
        session.close()


@pytest.fixture
def admin() -> User:
    return _insert(User(email="admin@test.com", hashed_password=hash_password("admin12345"), is_admin=True))


@pytest.fixture
def user() -> User:
    return _insert(User(email="user@test.com", hashed_password=hash_password("user12345"), is_admin=False))


@pytest.fixture
def other_user() -> User:
    return _insert(User(email="other@test.com", hashed_password=hash_password("other12345"), is_admin=False))


@pytest.fixture
def event() -> Event:
    from datetime import datetime, timedelta, timezone

    return _insert(
        Event(
            name="Test Concert",
            venue="Test Arena",
            starts_at=datetime.now(timezone.utc) + timedelta(days=10),
            total_seats=10,
            available_seats=10,
            price=100.0,
        )
    )


def auth_header(client: TestClient, email: str, password: str) -> dict[str, str]:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
