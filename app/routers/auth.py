# WHY two login endpoints: /login takes JSON (what a real client sends), /token takes the OAuth2
# form body so the "Authorize" button inside FastAPI's /docs works during a live demo. Same code path.
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select

from app.deps import CurrentUser, DbSession
from app.errors import Conflict, Unauthorized
from app.models import User
from app.schemas import LoginRequest, Token, UserCreate, UserOut
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: DbSession):
    # WHY hash BEFORE the first query: bcrypt deliberately takes ~250ms. Doing it after the SELECT
    # would hold an open database transaction for that entire time, serialising every other writer
    # behind pure CPU work. Cheap ordering change, big difference under concurrent signups.
    hashed = hash_password(payload.password)

    existing = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if existing:
        raise Conflict("Email already registered", code="email_taken")
    user = User(email=payload.email, hashed_password=hashed)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _authenticate(db, email: str, password: str) -> User:
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    # WHY one generic message for both "no such user" and "wrong password": a distinct
    # "user not found" reply is a free account-enumeration oracle for an attacker.
    if not user or not verify_password(password, user.hashed_password):
        raise Unauthorized("Incorrect email or password")
    return user


@router.post("/login", response_model=Token)
def login(payload: LoginRequest, db: DbSession):
    user = _authenticate(db, payload.email, payload.password)
    return Token(access_token=create_access_token(user_id=user.id, is_admin=user.is_admin))


@router.post("/token", response_model=Token, include_in_schema=True)
def login_form(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: DbSession):
    # OAuth2 spec names the field `username`; we put the email in it.
    user = _authenticate(db, form.username, form.password)
    return Token(access_token=create_access_token(user_id=user.id, is_admin=user.is_admin))


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    """Trivial endpoint, but it's the fastest way to prove a token is valid during a demo."""
    return user
