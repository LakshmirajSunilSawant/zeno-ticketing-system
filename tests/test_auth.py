# WHY test auth separately from booking: these are the two independent ways the system can lose
# money (oversell seats / let the wrong person act), so each gets its own focused suite.
from app.security import create_access_token, decode_access_token, hash_password, verify_password
from tests.conftest import auth_header


def test_password_is_hashed_not_stored_plaintext():
    h = hash_password("hunter2hunter2")
    assert h != "hunter2hunter2"
    assert h.startswith("$2b$")  # bcrypt identifier + cost factor
    assert verify_password("hunter2hunter2", h)
    assert not verify_password("wrong-password", h)


def test_same_password_hashes_differently():
    """Proves the hash is salted - identical passwords must not produce identical hashes,
    otherwise a leaked table instantly reveals which users share a password."""
    assert hash_password("samepassword") != hash_password("samepassword")


def test_verify_password_survives_corrupt_hash():
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_jwt_roundtrip_carries_identity_and_role():
    payload = decode_access_token(create_access_token(user_id=42, is_admin=True))
    assert payload["sub"] == "42"
    assert payload["is_admin"] is True
    assert "exp" in payload


def test_tampered_token_is_rejected():
    """A token signed with a different secret must not validate - this is the whole point of JWT."""
    token = create_access_token(user_id=1, is_admin=False)
    assert decode_access_token(token[:-4] + "AAAA") is None
    assert decode_access_token("garbage.token.here") is None


def test_register_and_login(client):
    r = client.post("/api/v1/auth/register", json={"email": "new@test.com", "password": "password123"})
    assert r.status_code == 201
    assert "hashed_password" not in r.json()  # response model must never leak the hash

    r = client.post("/api/v1/auth/login", json={"email": "new@test.com", "password": "password123"})
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"


def test_duplicate_email_rejected(client):
    body = {"email": "dupe@test.com", "password": "password123"}
    assert client.post("/api/v1/auth/register", json=body).status_code == 201
    r = client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"


def test_short_password_rejected_with_422(client):
    r = client.post("/api/v1/auth/register", json={"email": "a@b.com", "password": "short"})
    assert r.status_code == 422


def test_invalid_email_rejected_with_422(client):
    r = client.post("/api/v1/auth/register", json={"email": "not-an-email", "password": "password123"})
    assert r.status_code == 422


def test_wrong_password_gives_generic_message(client, user):
    """The message must not reveal whether the account exists (account-enumeration defence)."""
    r = client.post("/api/v1/auth/login", json={"email": "user@test.com", "password": "wrongpassword"})
    assert r.status_code == 401
    missing = client.post("/api/v1/auth/login", json={"email": "nobody@test.com", "password": "wrongpassword"})
    assert missing.json()["error"]["message"] == r.json()["error"]["message"]


def test_protected_route_requires_token(client):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_admin_only_route_blocks_normal_user(client, user, admin):
    payload = {"name": "E", "venue": "V", "starts_at": "2030-01-01T10:00:00Z", "total_seats": 5, "price": 10}
    r = client.post("/api/v1/events", json=payload, headers=auth_header(client, "user@test.com", "user12345"))
    assert r.status_code == 403
    r = client.post("/api/v1/events", json=payload, headers=auth_header(client, "admin@test.com", "admin12345"))
    assert r.status_code == 201


def test_security_headers_present(client):
    h = client.get("/health").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert "x-request-id" in h
