# WHY slowapi with an in-memory store: zero infrastructure, and it's the right shape to swap for
# Redis later (one `storage_uri` change). The honest caveat I'd raise in an interview: in-memory
# limits are PER PROCESS, so with N workers the effective limit is N x the configured value.
# Anything real needs a shared counter in Redis, or rate limiting at the API gateway / edge.
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config import settings
from app.security import decode_access_token


def rate_limit_key(request: Request) -> str:
    """WHY key by user ID when we can: IP-based limiting punishes everyone behind one corporate NAT
    or mobile carrier. Falling back to IP covers unauthenticated traffic (login/register abuse)."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        payload = decode_access_token(auth.split(" ", 1)[1])
        if payload and payload.get("sub"):
            return f"user:{payload['sub']}"
    # WHY X-Forwarded-For first: on Render we sit behind a proxy, so request.client.host is the
    # proxy's IP and every user would share one bucket.
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return f"ip:{fwd.split(',')[0].strip()}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=[settings.RATE_LIMIT],  # 100/minute
    # storage_uri="redis://..."  <- the single change needed to make this multi-process correct
)
