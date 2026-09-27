import datetime as dt
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, MetaData, Numeric, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# BIGINT autoincrement on MariaDB, INTEGER on SQLite (required for rowid autoincrement).
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")

# Quantities and prices: 18 digits, 4 decimals.
Qty = Numeric(18, 4)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict] = {Decimal: Qty, dt.datetime: DateTime(timezone=False)}


class IdMixin:
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class CreatedByMixin:
    """Audit column required on every document/transaction and master record.

    Nullable only for rows created by the system itself (seed data, migrations).
    """

    @declared_attr
    def created_by_id(cls) -> Mapped[int | None]:
        return mapped_column(BigIntPK, ForeignKey("users.id"), nullable=True)


class VersionMixin:
    """Optimistic locking so two LAN users can't silently overwrite each other."""

    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    @declared_attr.directive
    def __mapper_args__(cls):
        return {"version_id_col": cls.__table__.c.version_id}
