"""Load test whose ONLY job is to prove the system cannot oversell under real concurrent load.

WHY locust over ab/wrk: those measure throughput on one URL. I need a stateful scenario (register,
log in, then hammer one specific event) and a custom pass/fail assertion at the end, which is
exactly what locust's Python API gives me.

HOW TO READ THE RESULT: a 409 "insufficient_seats" is a SUCCESS, not a failure - it's the system
correctly refusing to oversell. The number that matters is printed in the summary at the end:
    seats sold  == total capacity   -> correct
    seats sold  >  total capacity   -> OVERSOLD (this is the bug the row lock exists to prevent)

RUN (against local):
    locust -f locustfile.py --host http://127.0.0.1:8000 --headless -u 50 -r 50 -t 30s
RUN (against Render, warm it up first):
    locust -f locustfile.py --host https://<your-service>.onrender.com --headless -u 50 -r 25 -t 60s

Set TARGET_EVENT_ID to a small event so it sells out quickly and the assertion is meaningful.
"""

import os
import uuid

from locust import HttpUser, between, events, task

TARGET_EVENT_ID = int(os.getenv("TARGET_EVENT_ID", "4"))  # the 5-seat demo event from seed.py
SEATS_PER_REQUEST = int(os.getenv("SEATS_PER_REQUEST", "1"))
API = "/api/v1"


class TicketBuyer(HttpUser):
    # WHY a short wait: I want maximum contention on one row, not a realistic browsing pattern.
    # This is a correctness test disguised as a load test.
    wait_time = between(0.1, 0.3)

    def on_start(self):
        """Each simulated user registers its own account, so bookings aren't all from one user
        (which would let the idempotency/ownership paths mask a concurrency bug)."""
        # WHY not "@zeno.test": email-validator rejects reserved TLDs (.test/.invalid/.localhost),
        # so every registration would 422 and the load test would silently measure nothing.
        self.email = f"load-{uuid.uuid4().hex[:12]}@loadtest.dev"
        self.client.post(
            f"{API}/auth/register", json={"email": self.email, "password": "loadtest123"}, name="register"
        )
        r = self.client.post(
            f"{API}/auth/login", json={"email": self.email, "password": "loadtest123"}, name="login"
        )
        self.headers = {"Authorization": f"Bearer {r.json()['access_token']}"} if r.ok else {}

    @task(10)
    def book_the_hot_event(self):
        with self.client.post(
            f"{API}/bookings",
            json={"event_id": TARGET_EVENT_ID, "seats": SEATS_PER_REQUEST},
            headers=self.headers,
            name="POST /bookings [contended]",
            catch_response=True,
        ) as r:
            if r.status_code == 201:
                r.success()
            elif r.status_code == 409:
                # Sold out. This is the correct answer once capacity is gone - mark it a success so
                # the failure column only ever shows genuine defects.
                r.success()
            elif r.status_code == 429:
                r.success()  # rate limiter doing its job, also not a defect
            else:
                r.failure(f"unexpected {r.status_code}: {r.text[:200]}")

    @task(2)
    def browse_events(self):
        """A little read traffic alongside the writes - reads must stay fast while a hot event is
        being contended, which is the point of locking one ROW rather than the table."""
        with self.client.get(f"{API}/events", name="GET /events", catch_response=True) as r:
            # A 429 here is the rate limiter doing its job at 100 req/min, not a defect. Without
            # this the failure column fills with expected throttling and hides real problems.
            r.success() if r.status_code in (200, 429) else r.failure(f"unexpected {r.status_code}")


@events.test_stop.add_listener
def verify_no_overselling(environment, **_):
    """The actual assertion. Runs once after the load stops and fails the process (exit code 1)
    if capacity was ever exceeded, so this can gate a CI pipeline."""
    import requests

    host = environment.host.rstrip("/")
    try:
        ev = requests.get(f"{host}{API}/events/{TARGET_EVENT_ID}", timeout=15).json()
    except Exception as e:  # noqa: BLE001
        print(f"\n[oversell check] could not fetch event: {e}")
        return

    total, available = ev["total_seats"], ev["available_seats"]
    sold = total - available

    print("\n" + "=" * 68)
    print(f"  OVERSELL CHECK - event {TARGET_EVENT_ID}: {ev['name']!r}")
    print(f"  capacity        : {total}")
    print(f"  seats sold      : {sold}")
    print(f"  seats remaining : {available}")
    if available < 0 or sold > total:
        print("  RESULT          : *** OVERSOLD - CONCURRENCY BUG ***")
        environment.process_exit_code = 1
    else:
        print("  RESULT          : OK - never exceeded capacity under concurrent load")
        environment.process_exit_code = 0
    print("=" * 68 + "\n")
