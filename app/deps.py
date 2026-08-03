# WHY FastAPI dependencies for auth: authentication becomes a type annotation on the handler.
# It's declarative, testable in isolation, and it shows up in the OpenAPI spec (padlock in /docs)
# automatically - so the docs can't drift from the enforcement.
from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import Forbidden, Unauthorized
from app.models import User
from app.security import decode_access_token

# auto_error=False so a missing token flows through OUR error envelope instead of FastAPI's
# default {"detail": ...} shape. tokenUrl makes the "Authorize" button in /docs actually work.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbSession,
    token: Annotated[str | None, Depends(oauth2_scheme)] = None,
) -> User:
    if not token:
        raise Unauthorized("Missing bearer token")
    payload = decode_access_token(token)
    if not payload:
        raise Unauthorized("Invalid or expired token")

    # WHY re-load the user from the DB instead of trusting the token's claims: the token could have
    # been issued before the account was deleted or demoted. The JWT proves identity; the DB is the
    # source of truth for authorisation. Cost is one indexed PK lookup per request.
    user = db.get(User, int(payload["sub"]))
    if not user:
        raise Unauthorized("User no longer exists")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        # 403 not 401: we know who you are, you just aren't allowed. Distinguishing them matters
        # because a client should retry after 401 (re-login) but never after 403.
        raise Forbidden("Admin privileges required")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
