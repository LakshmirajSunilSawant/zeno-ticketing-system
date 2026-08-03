"""Live demonstration of what a JWT is, and what stops a client forging one.

WHY this script exists: "the token is signed" is easy to say and hard to believe. This proves it in
about ten seconds - it reads a real token's contents, then tries to escalate a normal user to admin
by editing the payload, and shows the server rejecting it.

WHY it uses only the standard library: it must run on any machine with Python, including an
interviewer's laptop, with no pip install first.

Run:  python demo_jwt.py                      (against the deployed instance)
      python demo_jwt.py http://127.0.0.1:8000  (against a local server)
"""

import base64
import json
import sys
import urllib.error
import urllib.request

API = (sys.argv[1] if len(sys.argv) > 1 else "http://65.2.107.97").rstrip("/")
EMAIL, PASSWORD = "demo@zeno.dev", "demo12345"


def _call(path: str, token: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(API + path, data=data)
    if body:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def decode_segment(seg: str) -> dict:
    # A JWT segment is base64url with the padding stripped; put it back before decoding.
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


def main() -> None:
    print(f"\nTarget: {API}")

    print("\n[1] Log in as a NORMAL (non-admin) user")
    status, tok = _call("/api/v1/auth/login", body={"email": EMAIL, "password": PASSWORD})
    if status != 200:
        sys.exit(f"    login failed ({status}) - is the server up and seeded?")
    token = tok["access_token"]
    print(f"    {token[:52]}...")

    header_b64, payload_b64, signature = token.split(".")

    print("\n[2] A JWT is SIGNED, not encrypted - anyone holding it can read it")
    print("    header :", json.dumps(decode_segment(header_b64)))
    print("    payload:", json.dumps(decode_segment(payload_b64)))
    print("    Note: never put anything secret in a JWT payload. This one carries")
    print("    only a user id, a role flag, and issued/expiry timestamps.")

    print("\n[3] The unmodified token works")
    print(f"    GET /auth/me -> {_call('/api/v1/auth/me', token)[0]}")

    print("\n[4] TAMPER: change the last 4 characters of the signature")
    status, body = _call("/api/v1/auth/me", token[:-4] + "AAAA")
    print(f"    GET /auth/me -> {status}  {body['error']['message']}")

    print("\n[5] FORGE: rewrite the payload to claim is_admin = true")
    payload = decode_segment(payload_b64)
    payload["is_admin"] = True
    forged_payload = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    forged = f"{header_b64}.{forged_payload}.{signature}"
    print("    forged payload:", json.dumps(payload))
    status, body = _call("/api/v1/auth/me", forged)
    print(f"    GET /auth/me            -> {status}  {body['error']['message']}")
    print(f"    GET /analytics/revenue  -> {_call('/api/v1/analytics/revenue', forged)[0]}  (admin-only)")

    print("\n    The signature is an HMAC over the header and payload, keyed with a secret")
    print("    only the server holds. Change one byte of the payload and it no longer matches.")
    print("\n    And even if the signature DID verify, this API still would not trust the")
    print("    is_admin claim - get_current_user re-loads the user from the database, so a")
    print("    demoted admin loses access immediately instead of at token expiry.\n")


if __name__ == "__main__":
    main()
