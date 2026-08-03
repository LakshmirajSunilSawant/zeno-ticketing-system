# WHY a seed script instead of hand-rolling curl calls: the demo, the load test and the Postman
# collection all need the same known-good starting state. Idempotent, so re-running is safe.
# Run: python seed.py    (Render runs it automatically as part of the start command)
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import Event, User
from app.security import hash_password

# WHY read credentials from env with a dev fallback: the deployed instance gets real values from
# Render's env vars, so no password is ever committed to git.
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@zeno.dev")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin12345")
DEMO_EMAIL = os.getenv("DEMO_EMAIL", "demo@zeno.dev")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "demo12345")


def get_or_create_user(db, email: str, password: str, is_admin: bool) -> User:
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user:
        return user
    user = User(email=email, hashed_password=hash_password(password), is_admin=is_admin)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        get_or_create_user(db, ADMIN_EMAIL, ADMIN_PASSWORD, True)
        get_or_create_user(db, DEMO_EMAIL, DEMO_PASSWORD, False)

        if db.execute(select(Event)).first():
            print("Events already seeded, skipping.")
            return

        now = datetime.now(timezone.utc)
        events = [
            # The 5-seat event exists specifically so the load test can drive a hot event to
            # exhaustion in seconds and prove nothing oversells.
            Event(name="Arijit Singh Live", venue="DY Patil Stadium, Mumbai",
                  starts_at=now + timedelta(days=30), total_seats=500, available_seats=500, price=2499.00),
            Event(name="India vs Australia T20", venue="Chinnaswamy Stadium, Bengaluru",
                  starts_at=now + timedelta(days=14), total_seats=200, available_seats=200, price=1800.00),
            Event(name="Zakir Khan: Haq Se Single", venue="Siri Fort Auditorium, Delhi",
                  starts_at=now + timedelta(days=7), total_seats=120, available_seats=120, price=999.50),
            Event(name="SOLD-OUT-DEMO: Last 5 Seats", venue="Phoenix Marketcity, Pune",
                  starts_at=now + timedelta(days=3), total_seats=5, available_seats=5, price=500.00),
        ]
        db.add_all(events)
        db.commit()
        print(f"Seeded {len(events)} events.")
        print(f"  admin: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
        print(f"  user:  {DEMO_EMAIL} / {DEMO_PASSWORD}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
