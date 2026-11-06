"""Registration, token login and the current-user dependency.

Passwords are hashed with scrypt (standard library). Login issues an opaque random bearer token;
only its HMAC-SHA256 under SECRET_KEY is stored, with an expiry.
"""

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import AuthToken, User, as_utc, utcnow
from app.deps import client_ip, db_session, rate_limiter, settings_dep, token_key
from app.ratelimit import RateLimiter
from app.settings import Settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    encoded = [base64.b64encode(part).decode() for part in (salt, digest)]
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${encoded[0]}${encoded[1]}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p)
        )
    except ValueError:
        log.error("password_hash_unreadable")
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the username does not exist, so timing does not reveal which
# usernames are registered.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def token_digest(token: str, key: bytes) -> str:
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def upsert_user(session: Session, username: str, password: str) -> None:
    """Create a user, or reset its password. Used to seed demo accounts at startup."""
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        session.add(User(username=username, password_hash=hash_password(password)))
    elif not verify_password(password, user.password_hash):
        user.password_hash = hash_password(password)


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=128)


class UserOut(BaseModel):
    id: int
    username: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105  # OAuth2 token type name, not a secret
    expires_in: int


DbSession = Annotated[Session, Depends(db_session)]
Limiter = Annotated[RateLimiter, Depends(rate_limiter)]
AppSettings = Annotated[Settings, Depends(settings_dep)]
TokenKey = Annotated[bytes, Depends(token_key)]


def current_user(
    session: DbSession, key: TokenKey, token: Annotated[str | None, Depends(oauth2_scheme)]
) -> User:
    unauthorized = HTTPException(
        status_code=401, detail="not_authenticated", headers={"WWW-Authenticate": "Bearer"}
    )
    if not token:
        raise unauthorized
    record = session.scalar(
        select(AuthToken).where(AuthToken.token_digest == token_digest(token, key))
    )
    if record is None or as_utc(record.expires_at) <= utcnow():
        raise unauthorized
    user = session.get(User, record.user_id)
    if user is None:
        raise unauthorized
    return user


CurrentUser = Annotated[User, Depends(current_user)]


@router.post("/register", status_code=201)
def register(
    body: Credentials, request: Request, session: DbSession, limiter: Limiter, settings: AppSettings
) -> UserOut:
    limiter.hit(f"register:{client_ip(request)}", settings.register_limit_per_hour, 3600)
    user = User(username=body.username.lower(), password_hash=hash_password(body.password))
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="username_taken") from None
    log.info("user_registered", extra={"user_id": user.id})
    return UserOut(id=user.id, username=user.username)


@router.post("/token")
def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    request: Request,
    session: DbSession,
    limiter: Limiter,
    settings: AppSettings,
    key: TokenKey,
) -> TokenOut:
    """Exchange a username and password for a bearer token (OAuth2 password flow)."""
    username = form.username.lower()
    limiter.hit(f"login-ip:{client_ip(request)}", settings.login_limit_per_minute, 60)
    limiter.hit(f"login-user:{username}", settings.login_limit_per_minute, 60)
    user = session.scalar(select(User).where(User.username == username))
    if not verify_password(form.password, user.password_hash if user else _DUMMY_HASH) or not user:
        log.info("login_failed")
        raise HTTPException(
            status_code=401, detail="invalid_credentials", headers={"WWW-Authenticate": "Bearer"}
        )
    token = secrets.token_urlsafe(32)
    ttl = timedelta(hours=settings.token_ttl_hours)
    session.execute(delete(AuthToken).where(AuthToken.expires_at <= utcnow()))
    session.add(
        AuthToken(user_id=user.id, token_digest=token_digest(token, key), expires_at=utcnow() + ttl)
    )
    session.commit()
    log.info("login_succeeded", extra={"user_id": user.id})
    return TokenOut(access_token=token, expires_in=int(ttl.total_seconds()))


@router.post("/logout", status_code=204)
def logout(
    user: CurrentUser,
    session: DbSession,
    key: TokenKey,
    token: Annotated[str | None, Depends(oauth2_scheme)],
) -> Response:
    """Revoke the bearer token used for this request."""
    session.execute(
        delete(AuthToken).where(
            AuthToken.user_id == user.id, AuthToken.token_digest == token_digest(token or "", key)
        )
    )
    session.commit()
    return Response(status_code=204)


@router.get("/me")
def me(user: CurrentUser) -> UserOut:
    return UserOut(id=user.id, username=user.username)
