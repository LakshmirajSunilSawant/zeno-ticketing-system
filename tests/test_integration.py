"""End-to-end lifecycle over real HTTP: register -> create event -> book -> modify -> cancel,
asserting the seat count after every single step.

WHY this exists alongside the unit tests: the unit tests prove the service logic is correct; this
proves the whole stack is wired correctly (routing, auth, serialisation, status codes, envelope).
It's the same journey the Postman/Newman collection walks, kept in Python so CI runs it for free.
"""

from tests.conftest import auth_header

EVENT = {
    "name": "Integration Test Show",
    "venue": "CI Arena",
    "starts_at": "2030-06-01T19:30:00Z",
    "total_seats": 20,
    "price": 250.0,
}


def seats_left(client, event_id: int) -> int:
    return client.get(f"/api/v1/events/{event_id}").json()["available_seats"]


def test_full_booking_lifecycle(client, admin, user):
    admin_h = auth_header(client, "admin@test.com", "admin12345")
    user_h = auth_header(client, "user@test.com", "user12345")

    # 1. admin creates the event
    r = client.post("/api/v1/events", json=EVENT, headers=admin_h)
    assert r.status_code == 201
    event_id = r.json()["id"]
    assert seats_left(client, event_id) == 20

    # 2. event listing is public - no auth header at all
    assert any(e["id"] == event_id for e in client.get("/api/v1/events").json())

    # 3. book 4 seats
    r = client.post("/api/v1/bookings", json={"event_id": event_id, "seats": 4}, headers=user_h)
    assert r.status_code == 201
    booking = r.json()
    assert booking["status"] == "CONFIRMED"
    assert booking["total_price"] == 1000.0  # 4 x 250
    assert seats_left(client, event_id) == 16

    # 4. modify up to 7 -> three more seats leave inventory
    r = client.patch(f"/api/v1/bookings/{booking['id']}", json={"seats": 7}, headers=user_h)
    assert r.status_code == 200 and r.json()["status"] == "MODIFIED"
    assert seats_left(client, event_id) == 13

    # 5. modify back down to 2 -> five seats returned
    assert client.patch(f"/api/v1/bookings/{booking['id']}", json={"seats": 2}, headers=user_h).status_code == 200
    assert seats_left(client, event_id) == 18

    # 6. it shows up in the user's booking list
    mine = client.get(f"/api/v1/users/{user.id}/bookings", headers=user_h).json()
    assert len(mine) == 1 and mine[0]["id"] == booking["id"]

    # 7. cancel -> all seats back to the original count
    r = client.delete(f"/api/v1/bookings/{booking['id']}", headers=user_h)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert seats_left(client, event_id) == 20

    # 8. cancelling again is a 409, and inventory is NOT credited twice
    assert client.delete(f"/api/v1/bookings/{booking['id']}", headers=user_h).status_code == 409
    assert seats_left(client, event_id) == 20


def test_idempotent_retry_over_http(client, admin, user):
    admin_h = auth_header(client, "admin@test.com", "admin12345")
    user_h = auth_header(client, "user@test.com", "user12345")
    event_id = client.post("/api/v1/events", json=EVENT, headers=admin_h).json()["id"]

    headers = {**user_h, "Idempotency-Key": "checkout-session-xyz"}
    first = client.post("/api/v1/bookings", json={"event_id": event_id, "seats": 3}, headers=headers)
    second = client.post("/api/v1/bookings", json={"event_id": event_id, "seats": 3}, headers=headers)

    assert first.status_code == 201
    # 200 (not 201) plus the replay header is how the client knows nothing new was created.
    assert second.status_code == 200
    assert second.headers.get("idempotent-replay") == "true"
    assert first.json()["id"] == second.json()["id"]
    assert seats_left(client, event_id) == 17  # charged once


def test_sellout_returns_409_and_never_goes_negative(client, admin, user):
    admin_h = auth_header(client, "admin@test.com", "admin12345")
    user_h = auth_header(client, "user@test.com", "user12345")
    tiny = {**EVENT, "total_seats": 3}
    event_id = client.post("/api/v1/events", json=tiny, headers=admin_h).json()["id"]

    assert client.post("/api/v1/bookings", json={"event_id": event_id, "seats": 3}, headers=user_h).status_code == 201
    assert seats_left(client, event_id) == 0

    r = client.post("/api/v1/bookings", json={"event_id": event_id, "seats": 1}, headers=user_h)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "insufficient_seats"
    assert seats_left(client, event_id) == 0  # not -1


def test_cannot_read_or_cancel_someone_elses_booking(client, admin, user, other_user):
    admin_h = auth_header(client, "admin@test.com", "admin12345")
    user_h = auth_header(client, "user@test.com", "user12345")
    other_h = auth_header(client, "other@test.com", "other12345")
    event_id = client.post("/api/v1/events", json=EVENT, headers=admin_h).json()["id"]
    bid = client.post("/api/v1/bookings", json={"event_id": event_id, "seats": 1}, headers=user_h).json()["id"]

    assert client.get(f"/api/v1/bookings/{bid}", headers=other_h).status_code == 403
    assert client.delete(f"/api/v1/bookings/{bid}", headers=other_h).status_code == 403
    assert client.get(f"/api/v1/users/{user.id}/bookings", headers=other_h).status_code == 403
    # ...but an admin legitimately can, for customer support.
    assert client.get(f"/api/v1/bookings/{bid}", headers=admin_h).status_code == 200


def test_analytics_reflect_bookings(client, admin, user):
    admin_h = auth_header(client, "admin@test.com", "admin12345")
    user_h = auth_header(client, "user@test.com", "user12345")
    event_id = client.post("/api/v1/events", json=EVENT, headers=admin_h).json()["id"]
    bid = client.post("/api/v1/bookings", json={"event_id": event_id, "seats": 4}, headers=user_h).json()["id"]

    rev = {r["event_id"]: r for r in client.get("/api/v1/analytics/revenue", headers=admin_h).json()}
    assert rev[event_id]["revenue"] == 1000.0

    occ = {r["event_id"]: r for r in client.get("/api/v1/analytics/occupancy", headers=admin_h).json()}
    assert occ[event_id]["seats_sold"] == 4 and occ[event_id]["occupancy_pct"] == 20.0

    # Cancelled bookings must drop straight out of both reports.
    client.delete(f"/api/v1/bookings/{bid}", headers=user_h)
    rev = {r["event_id"]: r for r in client.get("/api/v1/analytics/revenue", headers=admin_h).json()}
    assert rev[event_id]["revenue"] == 0.0

    trend = client.get("/api/v1/analytics/bookings-over-time?days=7", headers=admin_h)
    assert trend.status_code == 200


def test_analytics_requires_admin(client, user):
    user_h = auth_header(client, "user@test.com", "user12345")
    assert client.get("/api/v1/analytics/revenue", headers=user_h).status_code == 403


def test_openapi_docs_are_served(client):
    """The auto-generated docs are part of the deliverable, so they're worth asserting on."""
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/api/v1/bookings" in spec["paths"]
