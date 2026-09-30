"""Core schema. Quantities in the ledger are always in the item's base unit."""

import datetime as dt
import enum
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from caspian.db.base import (
    Base,
    BigIntPK,
    CreatedByMixin,
    IdMixin,
    TimestampMixin,
    VersionMixin,
)


def _enum(cls: type[enum.Enum]) -> Enum:
    return Enum(cls, native_enum=False, length=20, validate_strings=True)


# ----- users & security -----


class Role(IdMixin, Base):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)

    permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan", lazy="selectin"
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        BigIntPK, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission: Mapped[str] = mapped_column(String(64), primary_key=True)

    role: Mapped[Role] = relationship(back_populates="permissions")


class User(IdMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(150), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    pin_hash: Mapped[str | None] = mapped_column(String(255))
    role_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[dt.datetime | None]
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[dt.datetime | None]
    created_by_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("users.id"))

    role: Mapped[Role] = relationship(lazy="joined", foreign_keys=[role_id])


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index(None, "entity_type", "entity_id"),)

    at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), index=True)
    user_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[int | None] = mapped_column(BigIntPK)
    details: Mapped[dict | None] = mapped_column(JSON)
    # Admin who confirmed a protected action with their PIN.
    approved_by_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("users.id"))
    machine: Mapped[str | None] = mapped_column(String(128))


class AppSetting(Base):
    """Shared (all machines) business settings, e.g. company name, fiscal year."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | None] = mapped_column(JSON)


# ----- master data -----


class Warehouse(IdMixin, TimestampMixin, CreatedByMixin, Base):
    __tablename__ = "warehouses"

    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(150))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class Category(IdMixin, TimestampMixin, CreatedByMixin, Base):
    __tablename__ = "categories"

    name: Mapped[str] = mapped_column(String(150))
    parent_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("categories.id"))


class Unit(IdMixin, Base):
    __tablename__ = "units"

    name: Mapped[str] = mapped_column(String(50), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Item(IdMixin, TimestampMixin, CreatedByMixin, VersionMixin, Base):
    __tablename__ = "items"

    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    name_normalized: Mapped[str] = mapped_column(String(255), index=True)
    category_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("categories.id"))
    base_unit_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("units.id"))
    reorder_point: Mapped[Decimal | None]
    reorder_qty: Mapped[Decimal | None]
    is_returnable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    base_unit: Mapped[Unit] = relationship(lazy="joined")
    barcodes: Mapped[list["ItemBarcode"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", lazy="selectin"
    )
    units: Mapped[list["ItemUnit"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", lazy="selectin"
    )


class ItemUnit(IdMixin, Base):
    """Alternate unit for an item: 1 `unit` = `factor` base units (e.g. box = 24)."""

    __tablename__ = "item_units"
    __table_args__ = (
        UniqueConstraint("item_id", "unit_id"),
        CheckConstraint("factor > 0", name="factor_positive"),
    )

    item_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("items.id", ondelete="CASCADE"))
    unit_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("units.id"))
    factor: Mapped[Decimal]

    item: Mapped[Item] = relationship(back_populates="units")
    unit: Mapped[Unit] = relationship(lazy="joined")


class ItemBarcode(IdMixin, Base):
    __tablename__ = "item_barcodes"

    item_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("items.id", ondelete="CASCADE"))
    barcode: Mapped[str] = mapped_column(String(64), unique=True)
    # Unit the barcode represents (a box barcode vs. a piece barcode); NULL = base unit.
    unit_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("units.id"))

    item: Mapped[Item] = relationship(back_populates="barcodes")


class PersonKind(enum.StrEnum):
    SUPPLIER = "SUPPLIER"
    CUSTOMER = "CUSTOMER"
    EMPLOYEE = "EMPLOYEE"
    OTHER = "OTHER"


class Person(IdMixin, TimestampMixin, CreatedByMixin, VersionMixin, Base):
    __tablename__ = "persons"

    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    name_normalized: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[PersonKind] = mapped_column(_enum(PersonKind), default=PersonKind.OTHER)
    phone: Mapped[str] = mapped_column(String(50), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ----- documents & stock -----


class DocType(enum.StrEnum):
    OPENING = "OPENING"
    RECEIPT = "RECEIPT"
    ISSUE = "ISSUE"
    TRANSFER = "TRANSFER"
    ADJUSTMENT = "ADJUSTMENT"
    LOAN_OUT = "LOAN_OUT"
    LOAN_RETURN = "LOAN_RETURN"


class DocStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    CANCELLED = "CANCELLED"


class Document(IdMixin, TimestampMixin, CreatedByMixin, VersionMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("doc_type", "fiscal_year", "number"),
        Index(None, "doc_date"),
    )

    doc_type: Mapped[DocType] = mapped_column(_enum(DocType))
    fiscal_year: Mapped[int] = mapped_column(Integer)
    number: Mapped[int] = mapped_column(Integer)
    doc_date: Mapped[dt.date]
    status: Mapped[DocStatus] = mapped_column(_enum(DocStatus), default=DocStatus.DRAFT)
    warehouse_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("warehouses.id"))
    dest_warehouse_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("warehouses.id"))
    person_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("persons.id"))
    # Links a LOAN_RETURN to the LOAN_OUT it settles.
    related_document_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("documents.id"))
    description: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[dt.datetime | None]
    posted_by_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("users.id"))

    lines: Mapped[list["DocumentLine"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentLine.line_no",
        lazy="selectin",
    )


class DocumentLine(IdMixin, Base):
    __tablename__ = "document_lines"
    __table_args__ = (UniqueConstraint("document_id", "line_no"),)

    document_id: Mapped[int] = mapped_column(
        BigIntPK, ForeignKey("documents.id", ondelete="CASCADE")
    )
    line_no: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("items.id"), index=True)
    unit_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("units.id"))
    qty: Mapped[Decimal]
    factor: Mapped[Decimal] = mapped_column(default=Decimal(1))
    base_qty: Mapped[Decimal]
    unit_price: Mapped[Decimal | None]
    notes: Mapped[str] = mapped_column(Text, default="")

    document: Mapped[Document] = relationship(back_populates="lines")


class StockLedger(IdMixin, Base):
    """Immutable movement history; one row per warehouse effect of a posted line."""

    __tablename__ = "stock_ledger"
    __table_args__ = (Index(None, "item_id", "warehouse_id", "doc_date"),)

    document_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("documents.id"))
    line_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("document_lines.id"))
    item_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("items.id"))
    warehouse_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("warehouses.id"))
    doc_date: Mapped[dt.date]
    qty_change: Mapped[Decimal]
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())


class StockBalance(Base):
    """Running on-hand quantity per item and warehouse, maintained on posting."""

    __tablename__ = "stock_balances"

    item_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("items.id"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(
        BigIntPK, ForeignKey("warehouses.id"), primary_key=True
    )
    qty: Mapped[Decimal] = mapped_column(default=Decimal(0))


# ----- draft-first imports -----


class ImportKind(enum.StrEnum):
    ITEMS = "ITEMS"  # item master data (create / update items)
    STOCK = "STOCK"  # stock lines that become a draft document


class ImportSource(enum.StrEnum):
    EXCEL = "EXCEL"
    CSV = "CSV"
    WORD = "WORD"
    SCAN = "SCAN"
    TEXT = "TEXT"  # typed text parsed by the AI assistant


class BatchStatus(enum.StrEnum):
    OPEN = "OPEN"
    APPLIED = "APPLIED"
    DISCARDED = "DISCARDED"


class LineStatus(enum.StrEnum):
    NEW = "NEW"
    EXISTING_MATCH = "EXISTING_MATCH"
    CONFLICT = "CONFLICT"
    ERROR = "ERROR"
    IGNORED = "IGNORED"


class Resolution(enum.StrEnum):
    CREATE = "CREATE"  # create a new item
    MATCH = "MATCH"  # use `match_item_id` as-is
    OVERWRITE = "OVERWRITE"  # update `match_item_id` with the row's data (protected)
    IGNORE = "IGNORE"


class ImportBatch(IdMixin, TimestampMixin, CreatedByMixin, Base):
    """A draft import. Nothing reaches items/documents until a human applies it."""

    __tablename__ = "import_batches"

    kind: Mapped[ImportKind] = mapped_column(_enum(ImportKind))
    source: Mapped[ImportSource] = mapped_column(_enum(ImportSource))
    status: Mapped[BatchStatus] = mapped_column(_enum(BatchStatus), default=BatchStatus.OPEN,
                                                index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    doc_type: Mapped[DocType | None] = mapped_column(_enum(DocType))
    warehouse_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("warehouses.id"))
    person_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("persons.id"))
    applied_at: Mapped[dt.datetime | None]
    applied_by_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("users.id"))
    # Deleting that draft re-opens the batch (see documents.delete_draft).
    result_document_id: Mapped[int | None] = mapped_column(
        BigIntPK, ForeignKey("documents.id", ondelete="SET NULL"))

    lines: Mapped[list["ImportLine"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="ImportLine.row_no",
        lazy="selectin",
    )


class ImportLine(IdMixin, Base):
    __tablename__ = "import_lines"

    batch_id: Mapped[int] = mapped_column(
        BigIntPK, ForeignKey("import_batches.id", ondelete="CASCADE"), index=True
    )
    row_no: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict | None] = mapped_column(JSON)  # original cells, for the reviewer
    code: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    barcode: Mapped[str] = mapped_column(String(64), default="")
    unit_name: Mapped[str] = mapped_column(String(50), default="")
    category_name: Mapped[str] = mapped_column(String(150), default="")
    qty: Mapped[Decimal | None]
    unit_price: Mapped[Decimal | None]
    reorder_point: Mapped[Decimal | None]
    status: Mapped[LineStatus] = mapped_column(_enum(LineStatus))
    # Problem reading the source cell (e.g. text in a number column); cleared by editing.
    parse_error: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    match_item_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("items.id"))
    match_score: Mapped[int | None] = mapped_column(Integer)
    # Alternative items for CONFLICT rows: [[item_id, score], ...]
    candidates: Mapped[list | None] = mapped_column(JSON)
    resolution: Mapped[Resolution | None] = mapped_column(_enum(Resolution))

    batch: Mapped[ImportBatch] = relationship(back_populates="lines")


# ----- blind stocktake -----


class StocktakeStatus(enum.StrEnum):
    OPEN = "OPEN"  # counting in progress; system quantities hidden
    COUNTED = "COUNTED"  # counts submitted and locked; awaiting approval
    APPROVED = "APPROVED"  # adjustment posted
    CANCELLED = "CANCELLED"


class Stocktake(IdMixin, TimestampMixin, CreatedByMixin, Base):
    __tablename__ = "stocktakes"

    number: Mapped[int] = mapped_column(Integer, unique=True)
    warehouse_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("warehouses.id"))
    category_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("categories.id"))
    title: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[StocktakeStatus] = mapped_column(_enum(StocktakeStatus),
                                                    default=StocktakeStatus.OPEN, index=True)
    snapshot_at: Mapped[dt.datetime]
    submitted_at: Mapped[dt.datetime | None]
    submitted_by_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("users.id"))
    approved_at: Mapped[dt.datetime | None]
    approved_by_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("users.id"))
    adjustment_document_id: Mapped[int | None] = mapped_column(BigIntPK,
                                                               ForeignKey("documents.id"))

    lines: Mapped[list["StocktakeLine"]] = relationship(
        back_populates="stocktake", cascade="all, delete-orphan",
        order_by="StocktakeLine.line_no", lazy="selectin",
    )


class StocktakeLine(IdMixin, Base):
    __tablename__ = "stocktake_lines"
    __table_args__ = (UniqueConstraint("stocktake_id", "item_id"),)

    stocktake_id: Mapped[int] = mapped_column(
        BigIntPK, ForeignKey("stocktakes.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[int] = mapped_column(BigIntPK, ForeignKey("items.id"))
    # Base-unit quantity when the count started. Never shown to counters.
    system_qty: Mapped[Decimal]
    counted_qty: Mapped[Decimal | None]
    counted_by_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("users.id"))
    counted_at: Mapped[dt.datetime | None]
    note: Mapped[str] = mapped_column(Text, default="")

    stocktake: Mapped[Stocktake] = relationship(back_populates="lines")


# ----- AI providers -----


class ProviderKind(enum.StrEnum):
    OPENAI = "OPENAI"  # any OpenAI-compatible endpoint
    GROQ = "GROQ"
    HUGGINGFACE = "HUGGINGFACE"
    OLLAMA = "OLLAMA"  # local models (GGUF via Ollama / llama.cpp server)


class AIProvider(IdMixin, TimestampMixin, CreatedByMixin, Base):
    """Shared provider settings. API keys are NOT here: they live in each PC's
    Windows Credential Manager (see caspian.services.ai.config)."""

    __tablename__ = "ai_providers"

    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[ProviderKind] = mapped_column(_enum(ProviderKind))
    base_url: Mapped[str] = mapped_column(String(500))
    model: Mapped[str] = mapped_column(String(200))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    # Model used for speech-to-text on this provider (empty = not supported).
    stt_model: Mapped[str] = mapped_column(String(200), default="")


# ----- scheduled tasks -----


class TaskKind(enum.StrEnum):
    BACKUP = "BACKUP"
    REPORT = "REPORT"  # build a report and send it via Telegram / email


class TaskStatus(enum.StrEnum):
    PROPOSED = "PROPOSED"  # parsed from instructions (possibly by the AI); needs admin approval
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


class ScheduledTask(IdMixin, TimestampMixin, CreatedByMixin, Base):
    __tablename__ = "scheduled_tasks"

    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[TaskKind] = mapped_column(_enum(TaskKind))
    cron: Mapped[str] = mapped_column(String(100))  # "minute hour day-of-month month day-of-week"
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[TaskStatus] = mapped_column(_enum(TaskStatus), default=TaskStatus.PROPOSED)
    source_text: Mapped[str] = mapped_column(Text, default="")
    # Only one PC in the LAN runs a task: the one it was approved on.
    machine: Mapped[str] = mapped_column(String(128), default="")
    approved_by_id: Mapped[int | None] = mapped_column(BigIntPK, ForeignKey("users.id"))
    approved_at: Mapped[dt.datetime | None]
    last_run_at: Mapped[dt.datetime | None]
    next_run_at: Mapped[dt.datetime | None]
    last_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_result: Mapped[str] = mapped_column(Text, default="")
