"""Alembic environment.

At runtime the app passes an open connection via `config.attributes["connection"]`.
For development (`alembic revision --autogenerate`) a URL is taken from `-x url=...`
or defaults to a scratch SQLite file.
"""

from alembic import context
from sqlalchemy import create_engine

from caspian.db import models  # noqa: F401  (registers tables)
from caspian.db.base import Base

config = context.config
target_metadata = Base.metadata


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    _configure(connection)
else:
    url = context.get_x_argument(as_dictionary=True).get("url", "sqlite:///alembic_dev.db")
    engine = create_engine(url)
    with engine.begin() as conn:
        _configure(conn)
