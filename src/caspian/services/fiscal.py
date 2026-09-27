"""Fiscal year-end closing and read-only archives.

Closing year N (protected: admin PIN):
1. Pre-checks: no draft documents or open stocktakes in N.
2. Mandatory encrypted backup (caller supplies it).
3. Archive: that backup is restored into a separate database `<name>_N` that the app user
   can only read (MariaDB) or a copied file (SQLite dev).
4. Active database: opening balances for N+1 (per warehouse, OPENING documents), N's
   documents / ledger / finished stocktakes / applied imports removed (open loans are
   carried), optional purge of persons, items and stale items, balances recomputed from the
   ledger, audit log up to the end of N exported to Excel and wiped.
5. N is marked closed: documents dated in N or earlier can no longer be created or changed.
The active database keeps its name, so no PC needs reconfiguring.
"""

import asyncio
import datetime as dt
import logging
import shutil
from collections import defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func, select, text, update

from caspian.core import jalali
from caspian.core.permissions import Perm
from caspian.db.database import Database, DbConfig
from caspian.db.models import (
    AuditLog,
    BatchStatus,
    DocStatus,
    DocType,
    Document,
    DocumentLine,
    ImportBatch,
    ImportLine,
    Item,
    Person,
    StockBalance,
    StockLedger,
    Stocktake,
    StocktakeStatus,
)
from caspian.db.readonly import wrap_read_only
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.backup import MariaDbDumper, create_backup, iter_plaintext
from caspian.services.documents import _loan_rows
from caspian.services.errors import NotFound, ValidationError
from caspian.services.excel_export import write_xlsx
from caspian.services.fiscal_state import ArchiveInfo, load_state, read_state, write_state
from caspian.services.protected import Approval, ProtectedAction, consume
from caspian.services.reports import Column, ReportTable

log = logging.getLogger(__name__)
STALE_MONTHS = 12


@dataclass
class CloseOptions:
    carry_persons: bool = True
    carry_items: bool = True
    carry_stock: bool = True
    purge_stale_items: bool = False

    @classmethod
    def fresh_start(cls) -> "CloseOptions":
        return cls(carry_persons=False, carry_items=False, carry_stock=False)


@dataclass(frozen=True)
class PreCheck:
    year: int
    drafts: int
    open_stocktakes: int
    documents: int
    open_loans: int
    already_closed: bool
    not_ended: bool = False

    @property
    def blocking(self) -> list[str]:
        problems = []
        if self.already_closed:
            problems.append(f"سال {self.year} قبلاً بسته شده است.")
        if self.not_ended:
            problems.append(f"سال مالی {self.year} هنوز به پایان نرسیده است.")
        if self.drafts:
            problems.append(f"{self.drafts} سند پیش‌نویس در این سال وجود دارد؛ ثبت نهایی یا حذف کنید.")
        if self.open_stocktakes:
            problems.append("انبارگردانی باز وجود دارد؛ آن را تأیید یا لغو کنید.")
        return problems


@dataclass(frozen=True)
class CloseResult:
    year: int
    archive: str
    opening_documents: int
    removed_documents: int
    carried_loans: int
    removed_items: int
    deactivated_items: int
    removed_persons: int
    audit_rows_exported: int
    audit_file: str


def archive_name(active_name: str, year: int) -> str:
    return f"{active_name}_{year}"


# ----- checks -----


async def pre_check(db: Database, actor: Actor, year: int) -> PreCheck:
    actor.require(Perm.YEAR_CLOSE)
    async with db.session() as s:
        drafts = await s.scalar(select(func.count()).select_from(Document).where(
            Document.fiscal_year <= year, Document.status == DocStatus.DRAFT)) or 0
        docs = await s.scalar(select(func.count()).select_from(Document).where(
            Document.fiscal_year == year)) or 0
        stocktakes = await s.scalar(select(func.count()).select_from(Stocktake).where(
            Stocktake.status.in_([StocktakeStatus.OPEN, StocktakeStatus.COUNTED]))) or 0
        loans = len([r for r in await _loan_rows(s) if r.outstanding > 0])
        closed = (await read_state(s)).closed_through
    return PreCheck(year, drafts, stocktakes, docs, loans, closed is not None and year <= closed,
                    year >= jalali.fiscal_year_of(dt.date.today()))


# ----- archive -----


async def create_archive_mariadb(config: DbConfig, backup_path: Path, backup_password: str,
                                 year: int, admin_user: str, admin_password: str) -> str:
    """Restore the year-end backup into `<name>_<year>` and grant SELECT only to the app users."""
    name = archive_name(config.name, year)
    admin_config = DbConfig(config.host, config.port, name, admin_user)
    dumper = MariaDbDumper(admin_config, admin_password)
    await asyncio.to_thread(dumper.load, iter_plaintext(backup_path, backup_password))
    admin = Database(admin_config.url(admin_password, database=""))
    try:
        async with admin.engine.connect() as conn:
            accounts = (await conn.execute(text(
                "SELECT User, Host FROM mysql.user WHERE User IN (:u, :ro)"),
                {"u": config.user, "ro": f"{config.user}_ro"})).all()
            for user, host in accounts:
                await conn.execute(text(f"GRANT SELECT ON `{name}`.* TO '{user}'@'{host}'"))
            await conn.commit()
    finally:
        await admin.dispose()
    return name


def create_archive_sqlite(active_path: Path, year: int) -> str:
    target = active_path.with_name(f"{active_path.stem}_{year}{active_path.suffix}")
    shutil.copyfile(active_path, target)
    return str(target)


def open_archive(db: Database, config: DbConfig, app_password: str, archive: ArchiveInfo) -> Database:
    """Read-only connection to an archive (SELECT-only grants + READ ONLY session)."""
    if db.is_sqlite:
        return wrap_read_only(f"sqlite+aiosqlite:///{archive.database}")
    archived = DbConfig(config.host, config.port, archive.database, config.user)
    return wrap_read_only(str(archived.url(app_password).render_as_string(hide_password=False)))


# ----- closing -----


def _audit_table(rows) -> ReportTable:
    return ReportTable(
        "بایگانی رویدادهای سیستم",
        [Column("زمان", "date"), Column("کاربر", "int"), Column("عملیات"), Column("موضوع"),
         Column("شناسه", "int"), Column("جزئیات"), Column("رایانه"), Column("تأییدکننده", "int")],
        [[r.at, r.user_id, r.action, r.entity_type or "", r.entity_id, str(r.details or ""),
          r.machine or "", r.approved_by_id] for r in rows])


async def close_year(db: Database, actor: Actor, year: int, options: CloseOptions,
                     approval: Approval | None, archive: str, backup_file: str,
                     export_dir: str | Path) -> CloseResult:
    """Steps 4-5 (the caller already took the backup and created the archive)."""
    if actor.is_ai:
        raise ValidationError("بستن سال مالی فقط با کاربر است.")
    actor.require(Perm.YEAR_CLOSE)
    check = await pre_check(db, actor, year)
    if check.blocking:
        raise ValidationError(" ".join(check.blocking))
    approver_id = consume(approval, ProtectedAction.CLOSE_FISCAL_YEAR, actor)
    return await _close(db, actor, year, options, approver_id, archive, backup_file, export_dir)


async def _close(db: Database, actor: Actor, year: int, options: CloseOptions, approver_id: int,
                 archive: str, backup_file: str, export_dir: str | Path) -> CloseResult:

    start_next, _ = jalali.fiscal_year_bounds(year + 1)
    _, end = jalali.fiscal_year_bounds(year)
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    audit_file = export_dir / f"audit-{year}.xlsx"

    async with db.session(actor.user_id) as s:
        # --- which documents survive: open loans (and their returns) are carried over
        open_loan_ids = {r.document_id for r in await _loan_rows(s) if r.outstanding > 0}
        old_doc_ids = set((await s.scalars(select(Document.id).where(Document.fiscal_year <= year))).all())
        carried = open_loan_ids | set((await s.scalars(select(Document.id).where(
            Document.related_document_id.in_(open_loan_ids)))).all()) if open_loan_ids else set()
        removed_docs = old_doc_ids - carried

        # --- opening balances at the end of the year (before touching the ledger)
        end_stock: dict[int, dict[int, Decimal]] = defaultdict(dict)
        rows = (await s.execute(
            select(StockLedger.warehouse_id, StockLedger.item_id, func.sum(StockLedger.qty_change))
            .where(StockLedger.doc_date <= end)
            .group_by(StockLedger.warehouse_id, StockLedger.item_id))).all()
        for wh, item, qty in rows:
            if qty:
                end_stock[wh][item] = Decimal(qty)
        opening = end_stock if options.carry_stock else {}
        had_stock = {item for lines in end_stock.values() for item in lines}

        # --- movement dates for the stale-item rule
        last_move = dict((await s.execute(select(StockLedger.item_id, func.max(StockLedger.doc_date))
                                          .group_by(StockLedger.item_id))).all())

        # --- remove the closed year's history
        await s.execute(delete(StockLedger).where(StockLedger.doc_date <= end))
        stale_stocktakes = select(Stocktake.id).where(Stocktake.status.in_(
            [StocktakeStatus.APPROVED, StocktakeStatus.CANCELLED]))
        await s.execute(delete(Stocktake).where(Stocktake.id.in_(stale_stocktakes)))
        finished = select(ImportBatch.id).where(ImportBatch.status != BatchStatus.OPEN)
        await s.execute(delete(ImportLine).where(ImportLine.batch_id.in_(finished)))
        await s.execute(delete(ImportBatch).where(ImportBatch.status != BatchStatus.OPEN))
        if removed_docs:
            await s.execute(update(Document).where(Document.related_document_id.in_(removed_docs))
                            .values(related_document_id=None))
            await s.execute(delete(DocumentLine).where(DocumentLine.document_id.in_(removed_docs)))
            await s.execute(delete(Document).where(Document.id.in_(removed_docs)))

        # --- opening documents for the new year
        opening_docs = 0
        for wh_id, lines in opening.items():
            number = (await s.scalar(select(func.max(Document.number)).where(
                Document.doc_type == DocType.OPENING, Document.fiscal_year == year + 1)) or 0) + 1
            doc = Document(doc_type=DocType.OPENING, fiscal_year=year + 1, number=number,
                           doc_date=start_next, status=DocStatus.POSTED, warehouse_id=wh_id,
                           description=f"مانده انتقالی از سال {year}", posted_at=dt.datetime.now(),
                           posted_by_id=actor.user_id)
            base_units = dict((await s.execute(select(Item.id, Item.base_unit_id)
                                               .where(Item.id.in_(list(lines))))).all())
            for no, (item_id, qty) in enumerate(sorted(lines.items()), start=1):
                doc.lines.append(DocumentLine(line_no=no, item_id=item_id, unit_id=base_units[item_id],
                                              qty=qty, factor=Decimal(1), base_qty=qty,
                                              notes="مانده انتقالی"))
            s.add(doc)
            await s.flush()
            for line in doc.lines:
                s.add(StockLedger(document_id=doc.id, line_id=line.id, item_id=line.item_id,
                                  warehouse_id=wh_id, doc_date=start_next, qty_change=line.base_qty))
            opening_docs += 1
        await s.flush()

        # --- balances = what the ledger now says
        await s.execute(delete(StockBalance))
        for item_id, wh_id, qty in (await s.execute(
                select(StockLedger.item_id, StockLedger.warehouse_id, func.sum(StockLedger.qty_change))
                .group_by(StockLedger.item_id, StockLedger.warehouse_id))).all():
            if qty:
                s.add(StockBalance(item_id=item_id, warehouse_id=wh_id, qty=qty))
        await s.flush()

        # --- items / persons
        matched = select(ImportLine.match_item_id).where(ImportLine.match_item_id.is_not(None))
        used_items = set((await s.scalars(select(DocumentLine.item_id))).all()) | set(
            (await s.scalars(select(StockBalance.item_id))).all()) | set((await s.scalars(matched)).all())
        stale_cutoff = end - dt.timedelta(days=30 * STALE_MONTHS)
        removed_items = deactivated = 0
        for item in (await s.scalars(select(Item))).all():
            # Stale: nothing on hand at year end, no movement in the last 12 months, and not a
            # recently defined item that simply hasn't been used yet.
            is_stale = (options.purge_stale_items and item.id not in had_stock
                        and item.created_at.date() < stale_cutoff
                        and (last_move.get(item.id) is None or last_move[item.id] < stale_cutoff))
            if not options.carry_items or is_stale:
                if item.id in used_items:
                    if is_stale and item.is_active:
                        item.is_active = False
                        deactivated += 1
                else:
                    await s.delete(item)
                    removed_items += 1
        removed_persons = 0
        if not options.carry_persons:
            used_persons = set((await s.scalars(select(Document.person_id).where(
                Document.person_id.is_not(None)))).all()) | set((await s.scalars(
                    select(ImportBatch.person_id).where(ImportBatch.person_id.is_not(None)))).all())
            for person in (await s.scalars(select(Person))).all():
                if person.id not in used_persons:
                    await s.delete(person)
                    removed_persons += 1

        # --- export and wipe the audit log up to the end of the closed year
        cutoff = dt.datetime.combine(end, dt.time.max)
        audit_rows = (await s.scalars(select(AuditLog).where(AuditLog.at <= cutoff)
                                      .order_by(AuditLog.id))).all()
        write_xlsx(_audit_table(audit_rows), audit_file)
        await s.execute(delete(AuditLog).where(AuditLog.at <= cutoff))

        # --- mark the year closed
        state = await read_state(s)
        state.closed_through = max(year, state.closed_through or year)
        state.archives = [a for a in state.archives if a["year"] != year] + [asdict(ArchiveInfo(
            year, archive, dt.datetime.now().isoformat(timespec="seconds"), actor.username,
            backup_file))]
        await write_state(s, state)
        audit.record(s, actor, "fiscal.year_closed", "fiscal_year", year, {
            "archive": archive, "backup": backup_file, "opening_documents": opening_docs,
            "removed_documents": len(removed_docs), "carried_loans": len(open_loan_ids),
            "options": asdict(options)}, approved_by_id=approver_id)

    return CloseResult(year, archive, opening_docs, len(removed_docs), len(open_loan_ids),
                       removed_items, deactivated, removed_persons, len(audit_rows), str(audit_file))


async def get_archive(db: Database, year: int) -> ArchiveInfo:
    for archive in (await load_state(db)).archive_list():
        if archive.year == year:
            return archive
    raise NotFound(f"بایگانی سال {year} پیدا نشد.")


async def run_year_end(db: Database, actor: Actor, year: int, options: CloseOptions,
                       approval: Approval | None, dumper, backup_password: str,
                       backup_dir: str | Path, config: DbConfig | None = None,
                       admin_user: str = "", admin_password: str = "",
                       progress=lambda _step: None) -> CloseResult:
    """The whole wizard: checks -> mandatory backup -> archive -> close."""
    if actor.is_ai:
        raise ValidationError("بستن سال مالی فقط با کاربر است.")
    actor.require(Perm.YEAR_CLOSE)
    check = await pre_check(db, actor, year)
    if check.blocking:
        raise ValidationError(" ".join(check.blocking))
    approver_id = consume(approval, ProtectedAction.CLOSE_FISCAL_YEAR, actor)  # before heavy work
    progress("backup")
    backup_info = await create_backup(db, actor, dumper, backup_password, backup_dir,
                                      label=f"year-end-{year}")
    progress("archive")
    if db.is_sqlite:
        archive = await asyncio.to_thread(create_archive_sqlite, Path(db.url.database), year)
    else:
        if not admin_user:
            raise ValidationError("برای ساخت بایگانی، حساب مدیر MariaDB لازم است.")
        archive = await create_archive_mariadb(config, backup_info.path, backup_password, year,
                                               admin_user, admin_password)
    progress("close")
    return await _close(db, actor, year, options, approver_id, archive, backup_info.name,
                        Path(backup_dir))
