# WHY bcrypt directly instead of passlib: passlib 1.7.4 is unmaintained and crashes reading
# bcrypt>=4.1's version metadata. bcrypt's own API is 3 lines, so the dependency bought nothing.
# WHY bcrypt at all (not SHA-256): it is a deliberately slow, salted KDF, so a leaked DB can't be
# brute-forced at GPU speed. Argon2id would be the 2024 default; bcrypt is the safe boring choice.
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.config import settings


def hash_password(plain: str) -> str:
    # gensalt() defaults to cost factor 12 -> ~250ms per hash, which is the point.
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        # checkpw is constant-time, so it doesn't leak information via response timing.
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash in the DB must read as "wrong password", never as a 500.
        return False


def create_access_token(*, user_id: int, is_admin: bool) -> str:
    """WHY JWT: the API is stateless, so any Render instance can validate a token without a shared
    session store. Trade-off: tokens can't be revoked before expiry - that's why the TTL is short
    and why a real system pairs this with a short access token + refresh token + a deny-list."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),  # WHY str: the JWT spec requires `sub` to be a string.
        "is_admin": is_admin,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        # WHY pass algorithms explicitly: prevents the classic "alg: none" JWT confusion attack.
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None
