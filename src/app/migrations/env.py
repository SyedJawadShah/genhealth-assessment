"""Alembic environment. Migrations run at startup on the connection `run_migrations` passes in."""

from alembic import context
from sqlalchemy.engine import Connection

from app.db import Base

connection: Connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()
