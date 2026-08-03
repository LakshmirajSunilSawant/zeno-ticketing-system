# Zeno — Event Ticketing API

A BookMyShow-style ticketing backend. Users browse events, book seats, modify and cancel bookings.
The whole design turns on one hard requirement: **an event must never sell more seats than it has**,
even when hundreds of people click "Book" on the last seat at the same instant.

**Live:** `http://<ec2-public-ip>` · **Interactive docs:** `/docs` · **Health:** `/health`

```bash
curl http://<ec2-public-ip>/api/v1/events
```

---

## Contents
1. [Architecture](#architecture)
2. [Quick start](#quick-start)
3. [API](#api)
4. [How overselling is prevented](#how-overselling-is-prevented) ← the important part
5. [Technology choices](#technology-choices)
6. [Production-readiness](#production-readiness)
7. [Analytics](#analytics)
8. [Testing](#testing)
9. [Deployment](#deployment)
10. [What I'd add with more time](#what-id-add-with-more-time)
11. [Known limitations](#known-limitations)

---

## Architecture

```
                         ┌───────────────────────────────┐
   any client, anywhere  │  curl · Postman · browser      │
   (CORS is open)        │  /docs Swagger UI · locust     │
                         └───────────────┬───────────────┘
                                         │  HTTP :80
                         ┌───────────────▼───────────────┐
                         │  EC2 t3.micro · SG: 80 open    │
                         └───────────────┬───────────────┘
                                         │
   ┌─────────────────────────────────────▼─────────────────────────────────────┐
   │                      FastAPI (uvicorn, 1 worker)                          │
   │                                                                           │
   │   MIDDLEWARE (outermost → innermost)                                      │
   │   ① RequestContext ─ request-id, latency, one JSON access log per request │
   │   ② CORS ────────── allow any origin (demo); expose X-Request-ID          │
   │   ③ SecurityHeaders  nosniff · frame-deny · CSP · HSTS on TLS             │
   │   ④ SlowAPI ─────── 100 req/min, keyed by user-id (falls back to IP)      │
   │                                                                           │
   │   ROUTES  /api/v1/…            EXCEPTION HANDLERS                         │
   │   ├── auth      register/login/me      AppError  → 4xx + envelope         │
   │   ├── events    CRUD (write=admin)     Validation→ 422 + field details    │
   │   ├── bookings  book/modify/cancel     Exception → 500, no stack trace    │
   │   └── analytics revenue/occupancy/trend                                   │
   │                          │                                                │
   │            ┌─────────────▼─────────────┐                                  │
   │            │  services/booking.py      │  ← the ONLY code that changes    │
   │            │  transaction + row lock   │    Event.available_seats         │
   │            └─────────────┬─────────────┘                                  │
   └──────────────────────────┼────────────────────────────────────────────────┘
                              │ SQLAlchemy 2.0 (sync)
                  ┌───────────▼────────────┐
                  │ PostgreSQL (container) │   users ─┐
                  │  SQLite locally        │   events ├─< bookings
                  │  CHECK available >= 0  │          ┘
                  └────────────────────────┘
                              │
                  ┌───────────▼────────────┐
                  │ notifications.py (stub)│  logs instead of sending email.
                  │ → real: publish to a   │  Deliberately outside the booking
                  │   queue, worker sends  │  transaction. See "Known limits".
                  └────────────────────────┘
```

**Data model**

```
users                 events                          bookings
─────                 ──────                          ────────
id                    id                              id
email (unique)        name, venue, starts_at          user_id   ──> users.id
hashed_password       total_seats                     event_id  ──> events.id
is_admin              available_seats  ← the counter  seats
created_at            price                           status  CONFIRMED|MODIFIED|CANCELLED
                      CHECK 0 <= available <= total   unit_price  ← price snapshot at purchase
                                                      idempotency_key
                                                      UNIQUE (user_id, idempotency_key)
```

---

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements-dev.txt                  # runtime deps + pytest/locust
python seed.py                                       # creates admin + demo user + 4 events
uvicorn app.main:app --reload
```

> `requirements.txt` holds runtime dependencies only — that's what the Docker image installs.
> `requirements-dev.txt` adds pytest, httpx and locust. Production images shouldn't ship test tools.

Open <http://127.0.0.1:8000/docs>. Seeded logins: `admin@zeno.dev / admin12345`, `demo@zeno.dev / demo12345`.

No configuration is required — it defaults to SQLite. Set `DATABASE_URL` to a Postgres URL and the
same code runs unchanged.

---

## API

All routes are under `/api/v1`. Full interactive reference at **`/docs`**, generated from the
Pydantic schemas — so the documentation cannot drift from the implementation.

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/auth/register` | — | 422 on bad email / password < 8 chars |
| POST | `/auth/login` | — | returns JWT bearer token |
| POST | `/auth/token` | — | same, OAuth2 form body (powers the Authorize button in `/docs`) |
| GET | `/auth/me` | user | |
| POST | `/events` | **admin** | |
| GET | `/events` | — | public, paginated (`limit`, `offset`) |
| GET | `/events/{id}` | — | public |
| POST | `/bookings` | user | accepts `Idempotency-Key` header |
| PATCH | `/bookings/{id}` | owner | change seat count, re-validates availability |
| DELETE | `/bookings/{id}` | owner | soft cancel, releases seats |
| GET | `/bookings/{id}` | owner/admin | |
| GET | `/users/{id}/bookings` | self/admin | |
| GET | `/analytics/revenue` | **admin** | |
| GET | `/analytics/occupancy` | **admin** | |
| GET | `/analytics/bookings-over-time` | **admin** | `?days=30` |
| GET | `/health` | — | liveness probe, exempt from rate limiting |

**Error envelope** — every failure, without exception:

```json
{ "error": { "code": "insufficient_seats",
             "message": "Only 2 seat(s) left for 'Arijit Singh Live', requested 5",
             "request_id": "8f3c1a…" } }
```

`request_id` is echoed in the `X-Request-ID` response header and appears on every log line for that
request, so a user's bug report maps straight to a server-side trace.

---

## How overselling is prevented

> **One sentence:** every booking runs inside a single database transaction that takes a row-level
> lock on the event row (`SELECT … FOR UPDATE`) before it checks and decrements `available_seats`,
> so two simultaneous requests for the last seat are serialised by the database and the second one
> loses.

The naive version has a classic read-modify-write race:

```
Request A                    Request B
──────────                   ──────────
read available_seats  → 1
                             read available_seats  → 1     ← both see a seat
write available_seats = 0
                             write available_seats = 0     ← two tickets, one seat
```

The fix is in [`app/services/booking.py`](app/services/booking.py):

```python
stmt = select(Event).where(Event.id == event_id).with_for_update()   # ← the lock
event = db.execute(stmt).scalar_one_or_none()

if event.available_seats < seats:
    raise Conflict("…")            # only reachable while holding the lock, so the read is current

event.available_seats -= seats
db.add(Booking(...))
db.commit()                        # lock released atomically with the write
```

`FOR UPDATE` locks **one row**. Request B blocks on `SELECT` until A commits, then re-reads the
*post-commit* value and correctly sees 0. Bookings for *different* events never contend at all.

**Why pessimistic locking and not optimistic (a `version` column + retry)?**
Contention on a hot event is high by definition — that's what a hot event *is*. Optimistic locking
would mean most requests do their work, fail the version check, and retry, burning CPU and adding
latency exactly when load is highest. The critical section here is one indexed read plus one write
(microseconds), so blocking is cheaper than retrying. Optimistic locking would be the better choice
if writes were rare and conflicts unlikely, or if transactions were long enough that holding a lock
was itself a risk.

**Three independent layers, so no single bug can oversell:**

| Layer | Mechanism | Catches |
|---|---|---|
| Application | `SELECT … FOR UPDATE` inside a transaction | the read-modify-write race |
| Schema | `CHECK (available_seats >= 0)` | any future code path that bypasses the service |
| Schema | `UNIQUE (user_id, idempotency_key)` | double-booking on client retry |

**SQLite parity.** SQLite has no `FOR UPDATE`, so locally the engine issues `BEGIN IMMEDIATE`, which
takes the write lock at the *start* of the transaction rather than on first write — the same mutual
exclusion, at coarser granularity (whole database, not one row). That's an acceptable trade for a
dev/test engine and it's what makes the local load test honest. See [`app/database.py`](app/database.py).

**Proof it works** — [`locustfile.py`](locustfile.py) against the seeded 5-seat event:

```
locust -f locustfile.py --host http://127.0.0.1:8000 --headless -u 40 -r 40 -t 20s
```
```
  Name                          # reqs   # fails
  POST /bookings [contended]      1939   0 (0.00%)
  GET  /events                     382   0 (0.00%)
  Aggregated                      2401   0 (0.00%)

  ====================================================================
    OVERSELL CHECK - event 4: 'SOLD-OUT-DEMO: Last 5 Seats'
    capacity        : 5
    seats sold      : 5
    seats remaining : 0
    RESULT          : OK - never exceeded capacity under concurrent load
  ====================================================================
```

1,939 concurrent booking attempts, exactly 5 seats sold. A `409 insufficient_seats` is counted as a
success in that run — it's the system correctly refusing to oversell. The listener exits non-zero if
capacity is ever exceeded, so it can gate CI.

The same guarantee is asserted in `pytest` without HTTP: `test_concurrent_bookings_never_oversell`
races 20 threads (each with its own connection) for 10 seats and asserts exactly 10 winners.

---

## Technology choices

**FastAPI over Flask/Django**
- **Validation is the type signature.** Pydantic models *are* the request schema; malformed input
  is rejected with a precise 422 before any handler code runs. Flask needs marshmallow wired in by
  hand; DRF serializers are more ceremony for the same result.
- **OpenAPI docs for free** at `/docs`, generated from those same models — the docs cannot go stale.
- **Async-capable** when I need it (fan-out to payment/notification services), without forcing it
  where I don't. Django would have brought an ORM, admin, templates and auth I'd have to fight
  around for a JSON API this small.

**PostgreSQL over MongoDB** — this is a *transactional* problem, not a document problem.
- Seat counts need **atomic read-modify-write with row-level locking**. Postgres gives me
  `SELECT … FOR UPDATE` in one line. Mongo has transactions now, but they're a bolt-on to a design
  whose default unit of atomicity is a single document.
- The data is inherently relational: bookings reference users and events, and **foreign keys +
  CHECK constraints let the database itself refuse invalid state**. A booking can't point at a
  deleted event; `available_seats` can't go negative — regardless of what the application does.
- Analytics are `GROUP BY` queries. That's SQL's home turf.
- Mongo would be the right call for a product catalogue with heterogeneous per-event attributes.
  It's the wrong call for money and inventory.

**SQLAlchemy sync, not async** — `SELECT … FOR UPDATE` and explicit transaction boundaries are
trivial to express and to reason about in the sync API. FastAPI runs sync endpoints in a threadpool,
so requests still execute concurrently on separate connections; the concurrency test proves it.
Async SQLAlchemy would add `await`-everywhere ceremony for no throughput gain at this scale, since
this workload is database-bound, not network-fan-out-bound.

**bcrypt directly, not passlib** — `passlib` 1.7.4 is unmaintained and crashes reading `bcrypt`
4.1+'s version metadata. The `bcrypt` package's own API is three lines. (Argon2id would be the
current best-practice choice; bcrypt is the well-understood, universally-supported default.)

**JWT** — stateless auth, so any instance can validate a token without a shared session store.
The honest trade-off: **tokens can't be revoked before they expire.** That's why the TTL is bounded,
and why a production system pairs a short-lived access token with a refresh token and a deny-list.

---

## Production-readiness

| Concern | Implementation | Where |
|---|---|---|
| **Auth** | JWT bearer, bcrypt (cost 12, salted) password hashing | `security.py`, `deps.py` |
| **Authorization** | admin flag for writes; per-object ownership checks to stop IDOR | `deps.py`, `services/booking.py` |
| **Rate limiting** | slowapi, 100/min, keyed by user-id → falls back to `X-Forwarded-For` | `ratelimit.py` |
| **Input validation** | Pydantic schemas with bounds (`seats` 1–10, price ≥ 0) → 422 + field errors | `schemas.py` |
| **Structured logging** | structlog; JSON in prod, one line per request with id/latency/status | `logging_config.py`, `middleware.py` |
| **Error handling** | one envelope for every failure; stack traces logged, never returned | `main.py`, `errors.py` |
| **Security headers** | `nosniff`, `X-Frame-Options`, CSP, `Referrer-Policy`, HSTS on TLS | `middleware.py` |
| **CORS** | open (`*`) so the API is reachable from any machine; credentials disabled | `main.py` |
| **API versioning** | every route under `/api/v1` | `main.py` |
| **OpenAPI docs** | `/docs` (Swagger UI), `/redoc`, `/openapi.json` | free from FastAPI |
| **Idempotency** | `Idempotency-Key` header, enforced by a UNIQUE index — not a read-then-write check | `services/booking.py` |
| **Health check** | `/health`, doesn't touch the DB, exempt from rate limiting | `main.py` |
| **Config** | 12-factor; all secrets from env vars, nothing in git | `config.py`, `.env.example` |

**Idempotency, specifically.** A client whose connection times out mid-booking must be able to
retry without being charged twice. It sends `Idempotency-Key: <uuid>`; a replay returns the original
booking with **`200`** (not `201`) and an `Idempotent-Replay: true` header. Correctness does *not*
depend on "check if it exists, then insert" — that's itself a race. The `UNIQUE (user_id,
idempotency_key)` index means the *database* rejects the duplicate, and the loser of the race
returns the winner's booking. `test_concurrent_idempotent_retries_book_once` fires ten simultaneous
replays of one key and asserts exactly one booking row results.

**Rate limiting, honestly.** The counter is in-memory, so it's **per process** — with N workers the
effective limit is N × 100/min. That's why the deploy pins one worker. The real fix is a shared
counter in Redis (a one-line `storage_uri` change) or rate limiting at the API gateway.

---

## Analytics

Three `GROUP BY` endpoints straight over the OLTP tables (`app/routers/analytics.py`):

- `GET /analytics/revenue` — gross revenue and tickets sold per event
- `GET /analytics/occupancy` — seats sold vs capacity, with occupancy %
- `GET /analytics/bookings-over-time?days=30` — daily counts for a trend line

Two deliberate decisions:

- **Revenue uses `unit_price` snapshotted on the booking**, not the event's current price. Re-pricing
  an event must never rewrite historical revenue.
- **Occupancy is derived from the booking rows**, not from `Event.available_seats`. They should
  always agree — so if they ever disagree, that's a free consistency check telling me the
  denormalised counter has drifted.

**How this evolves at scale.** These queries scan `bookings` and compete with customer-facing writes
for the same connections and buffer cache. The path is: (1) point them at a **read replica** so
reporting can't slow down checkout; (2) **CDC** the tables into a warehouse (Snowflake/BigQuery) with
Debezium; (3) model them as **dbt** tables on a schedule, with tests on the metrics; (4) serve BI
(Metabase/Looker) from the warehouse. The endpoint contracts wouldn't change — only what's behind them.

---

## Testing

```bash
pytest                      # 34 tests, ~11s
```

**Unit tests** (`tests/test_booking_logic.py`) — service layer, no HTTP, so failures are unambiguous:
seat decrement, oversell refusal, cancel restores seats, **double-cancel doesn't credit seats twice**,
modify up and down, price snapshotting, IDOR guard, idempotency (including two users legitimately
reusing the same key), and the two concurrency tests.

**Auth tests** (`tests/test_auth.py`) — hashes are salted and never stored in plaintext, tampered
tokens rejected, wrong-password and unknown-email return the *same* message (no account enumeration),
admin-only routes reject normal users, security headers present.

**Integration tests** (`tests/test_integration.py`) — the full lifecycle over real HTTP with the seat
count asserted after *every* step: create event → book → modify up → modify down → list → cancel →
double-cancel 409 → verify inventory. Plus idempotent replay, sell-out, cross-user access denial,
analytics correctness, and that `/docs` is served.

**Load test** (`locustfile.py`) — see [above](#how-overselling-is-prevented).

**API regression** (`postman_collection.json`) — 21 Newman-runnable requests walking the same
lifecycle from *outside* the process, so it can be pointed at the deployed service:

```bash
newman run postman_collection.json --env-var baseUrl=http://<ec2-public-ip>
```

Tests use a temporary **file-based** SQLite database, not `:memory:` — an in-memory database lives
inside a single connection, which would make the concurrency tests pass for the wrong reason. It's
still created and destroyed per session, and the schema is dropped and recreated between tests.

---

## Deployment

Deployed on a single **AWS EC2** instance running the API and PostgreSQL as two containers via
Docker Compose. The instance is built entirely from [`deploy/ec2-user-data.sh`](deploy/ec2-user-data.sh),
so it is reproducible: if the box dies, relaunching from that script yields an identical one.

```
                    Internet
                       │  :80
        ┌──────────────▼──────────────────────────┐
        │ EC2 t3.micro · Amazon Linux 2023        │
        │ Security group: 80 open, 22 from my IP  │
        │                                         │
        │  ┌────────────┐  docker network         │
        │  │ api        │───────┐                 │
        │  │ :8000      │       │                 │
        │  └────────────┘       ▼                 │
        │                 ┌──────────┐            │
        │                 │ db :5432 │            │
        │                 │ NOT      │            │
        │                 │ published│            │
        │                 └────┬─────┘            │
        │                      │ pgdata volume    │
        └──────────────────────┴──────────────────┘
```

**Postgres has no `ports:` mapping.** It is reachable only on the internal Docker network as
hostname `db`, so nothing on the internet can open a connection to 5432 — the database is not part
of the attack surface at all. Only port 80 is exposed.

**Steps**

1. EC2 → **Launch instance**. Amazon Linux 2023, **t3.micro**, create/select a key pair.
2. Security group: allow **HTTP (80) from `0.0.0.0/0`** and **SSH (22) from *My IP*** only.
3. **Advanced details → User data**: paste [`deploy/ec2-user-data.sh`](deploy/ec2-user-data.sh).
4. Launch, wait ~3 min for the bootstrap, then hit `http://<public-ip>/docs`.
5. Read the generated credentials: `ssh ec2-user@<ip>` then `sudo cat /opt/zeno/.env`.

Redeploy a new commit with `sudo bash /opt/zeno/deploy/update.sh` — the `pgdata` volume is
untouched, so existing bookings survive.

| Variable | Source | Purpose |
|---|---|---|
| `DATABASE_URL` | composed in `docker-compose.yml` | points at the `db` service |
| `POSTGRES_PASSWORD` | generated on the instance | never in git or on my laptop |
| `JWT_SECRET` | generated on the instance | token signing |
| `ENV` | `production` | switches logs to JSON |
| `CORS_ORIGINS` | `*` | callable from anywhere |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | generated on the instance | seeded admin account |

`seed.py` runs on every container start and is idempotent, so the demo always has an admin and
sample events without a manual step.

**Trade-offs I'd raise before being asked**

- **Co-locating Postgres means no managed backups, no point-in-time recovery, no failover.** The
  production answer is RDS; the only change required is pointing `DATABASE_URL` at the RDS endpoint
  and deleting the `db` service. I chose the single box because it's genuinely $0 on the free tier.
- **Plain HTTP, no TLS**, because there's no domain attached. Real fix: an ALB with an ACM
  certificate, or CloudFront in front. HSTS is already emitted conditionally once TLS exists.
- **One instance = a single point of failure**, and a redeploy is a few seconds of downtime. An ASG
  behind an ALB with a rolling deploy fixes both.

The image is also plain Docker with no EC2-specific assumptions, so the same artifact runs on ECS
Fargate, App Runner or Kubernetes; a `Procfile` is included for Heroku-style platforms.

---

## What I'd add with more time

- **Redis cache** for event listings — the highest-traffic, least-volatile read. Cache `GET /events`
  with a short TTL, invalidated on booking. Would cut database load substantially at almost no cost
  in correctness, since a slightly stale *listing* is fine; the *booking* path stays uncached.
- **Message queue** (SQS/Kafka) for notifications. `notifications.py` is already the seam: publish
  `booking.confirmed` after commit, let a worker deliver it with retries and a dead-letter queue.
  Today a third-party outage cannot break booking, because the stub only logs.
- **Read replica** for analytics, so reporting queries can't contend with checkout.
- **Data warehouse + dbt + BI** as described [above](#analytics).
- **Alembic migrations** instead of `create_all`, so schema changes are versioned and reversible.
- **CI/CD** — GitHub Actions running `pytest` + `newman` on every PR, blocking merge on failure,
  auto-deploying `main`.
- **Observability** — Prometheus metrics (booking latency, 409 rate, seat-sellout rate) with Grafana
  dashboards and alerts; OpenTelemetry traces. The 409 rate is the interesting business metric: a
  spike means real demand being turned away.
- **Seat-level inventory** (specific seats, holds with TTL) rather than a plain count — how a real
  ticketing product works, and what makes seat maps and 10-minute checkout timers possible.
- **Payments** with a proper saga: hold seats → charge → confirm, with compensating release on
  failure or timeout.
- **Refresh tokens + revocation**, and per-endpoint rate limits (login much stricter than reads).

---

## Known limitations

Being explicit about what this *doesn't* do, since a 2-hour build has to draw lines somewhere:

- **A single hot event serialises on one row.** That's the cost of the lock. At scale you shard the
  counter into N rows per event and lock a random shard, or move allocation into Redis with a
  durable write-behind.
- **No payment step.** Bookings are confirmed immediately; there's no hold-then-charge saga.
- **Notifications are stubbed** (they log). Intentional — see above.
- **`create_all` instead of migrations.** Fine for one schema version, not for a second.
- **In-memory rate limiting** is per-process; the deploy pins one worker to compensate.
- **No check that an event hasn't already started.** Deliberately skipped: SQLite returns naive
  datetimes while Postgres returns timezone-aware ones, and getting that comparison subtly wrong is
  a worse bug than not having the check. It belongs behind a normalised UTC helper.
- **Money is `Numeric(10,2)` in the database but serialised as a JSON float.** Exact where it
  matters; a stricter API would send integer minor units (paise).
