"""Async engine/session management and connection configuration."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import URL, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from caspian.db.base import CreatedByMixin

log = logging.getLogger(__name__)

DEFAULT_PORT = 3306
DEFAULT_DB_NAME = "caspian"
CHARSET = "utf8mb4"
COLLATION = "utf8mb4_persian_ci"


@dataclass
class DbConfig:
    """Where the MariaDB server is. The password is kept in the OS credential store."""

    host: str = "localhost"
    port: int = DEFAULT_PORT
    name: str = DEFAULT_DB_NAME
    user: str = "caspian"

    def url(self, password: str, database: str | None = None) -> URL:
        return URL.create(
            "mariadb+aiomysql",
            username=self.user,
            password=password,
            host=self.host,
            port=self.port,
            database=self.name if database is None else (database or None),
            query={"charset": CHARSET},
        )

    @property
    def is_local(self) -> bool:
        return self.host in ("localhost", "127.0.0.1", "::1")

    @property
    def secret_name(self) -> str:
        """Key under which the password is stored in the credential store."""
        return f"{self.user}@{self.host}:{self.port}"

    @classmethod
    def from_dict(cls, data: dict) -> "DbConfig | None":
        if not data:
            return None
        return cls(
            host=str(data.get("host", "localhost")),
            port=int(data.get("port", DEFAULT_PORT)),
            name=str(data.get("name", DEFAULT_DB_NAME)),
            user=str(data.get("user", "caspian")),
        )

    def to_dict(self) -> dict:
        return {"host": self.host, "port": self.port, "name": self.name, "user": self.user}


class Database:
    def __init__(self, url: str | URL, **engine_kwargs) -> None:
        self.url = make_url(url)
        if self.url.get_backend_name() != "sqlite":
            engine_kwargs.setdefault("pool_pre_ping", True)
            engine_kwargs.setdefault("pool_recycle", 3600)
        self.engine: AsyncEngine = create_async_engine(self.url, **engine_kwargs)
        if self.is_sqlite:
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @property
    def is_sqlite(self) -> bool:
        return self.url.get_backend_name() == "sqlite"

    @asynccontextmanager
    async def session(self, actor_id: int | None = None) -> AsyncIterator[AsyncSession]:
        """Unit of work: commits on success, rolls back on any exception.

        `actor_id` is stamped as `created_by_id` on every new row that has one.
        """
        async with self.sessionmaker() as session:
            session.info["actor_id"] = actor_id
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def ping(self) -> None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()


_ERROR_MESSAGES = {
    2003: (
        "اتصال به سرور برقرار نشد. آدرس/پورت را بررسی کنید "
        "و مطمئن شوید سرویس MariaDB در حال اجراست."
    ),
    2005: "نام سرور نامعتبر است.",
    1045: "نام کاربری یا رمز عبور پایگاه داده نادرست است.",
    1044: "این کاربر به پایگاه داده دسترسی ندارد.",
    1049: "پایگاه داده وجود ندارد. گزینه «ایجاد پایگاه داده» را فعال کنید.",
}


def describe_error(exc: BaseException) -> str:
    """Persian, user-facing explanation of a connection failure."""
    orig = getattr(exc, "orig", None) or exc
    code = orig.args[0] if getattr(orig, "args", None) else None
    if isinstance(code, int) and code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[code]
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return _ERROR_MESSAGES[2003]
    return f"خطای پایگاه داده: {orig}"


@event.listens_for(Session, "before_flush")
def _stamp_created_by(session: Session, _flush_context, _instances) -> None:
    actor_id = session.info.get("actor_id")
    if actor_id is None:
        return
    for obj in session.new:
        if isinstance(obj, CreatedByMixin) and obj.created_by_id is None:
            obj.created_by_id = actor_id


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async def ensure_database(config: DbConfig, password: str) -> bool:
    """Create the configured database if missing. Returns True if it was created."""
    server = Database(config.url(password, database=""))
    try:
        async with server.engine.connect() as conn:
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :n"),
                    {"n": config.name},
                )
            ).first()
            if exists:
                return False
            if not config.name.replace("_", "").isalnum():
                raise ValueError(f"Invalid database name: {config.name!r}")
            await conn.execute(
                text(
                    f"CREATE DATABASE `{config.name}`"
                    f" CHARACTER SET {CHARSET} COLLATE {COLLATION}"
                )
            )
            await conn.commit()
            log.info("Created database %s", config.name)
            return True
    finally:
        await server.dispose()
