"""Database engine, ORM models and schema migrations."""

import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, Date, DateTime, ForeignKey, String, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """SQLite drops time zones; every stored timestamp is UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # HMAC-SHA256 of the bearer token; the token itself is never stored.
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16))
    # Only validated values are stored in these columns; anything else stays null.
    patient_first_name: Mapped[str | None] = mapped_column(String(100))
    patient_last_name: Mapped[str | None] = mapped_column(String(100))
    patient_date_of_birth: Mapped[date | None] = mapped_column(Date)
    provider_name: Mapped[str | None] = mapped_column(String(200))
    provider_npi: Mapped[str | None] = mapped_column(String(10))
    # Per-field status, reason and the extracted text, for review.
    field_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    document_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


def make_engine(url: str) -> Engine:
    if url.startswith(("postgres://", "postgresql://")):
        # Hosted Postgres URLs name no driver; SQLAlchemy would otherwise pick psycopg2.
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    if url.startswith("sqlite"):
        if url in ("sqlite://", "sqlite:///:memory:"):
            # One shared connection, so every session sees the same in-memory database.
            return create_engine(
                url, connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
        return create_engine(url, connect_args={"check_same_thread": False})
    # Serverless Postgres closes idle connections; check each one before use. libpq waits
    # forever by default, so bound the connect time.
    return create_engine(
        url, pool_pre_ping=True, pool_recycle=300, connect_args={"connect_timeout": 10}
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def run_migrations(engine: Engine) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
