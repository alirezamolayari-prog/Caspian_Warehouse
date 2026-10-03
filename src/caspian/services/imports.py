"""Draft-first import pipeline.

Every source (Excel/CSV/Word, barcode scanning, AI-parsed text) produces an
ImportBatch of ImportLines. Each line is matched against existing items and gets a
status (NEW / EXISTING_MATCH / CONFLICT / ERROR / IGNORED). A human reviews and
resolves the lines; only `apply_batch` writes items or a *draft* document, in one
transaction. AI actors may create batches but can never apply them.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from rapidfuzz import fuzz, process
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.core.permissions import Perm
from caspian.core.text import normalize, to_ascii_digits
from caspian.db.database import Database
from caspian.db.models import (
    BatchStatus,
    Category,
    DocType,
    ImportBatch,
    ImportKind,
    ImportLine,
    ImportSource,
    Item,
    ItemBarcode,
    LineStatus,
    Person,
    Resolution,
    Unit,
    Warehouse,
)
from caspian.services import audit, documents, items
from caspian.services.actor import Actor
from caspian.services.errors import NotFound, PermissionDenied, ValidationError
from caspian.services.import_files import RawRow
from caspian.services.items import ItemInput
from caspian.services.protected import Approval, ProtectedAction, consume

AUTO_MATCH = 90  # fuzzy score accepted without asking (stock lines only)
SUGGEST = 70  # below this a name is treated as a new item
AMBIGUOUS_GAP = 2  # top two candidates this close: ask, don't pick
DEFAULT_UNIT = "عدد"

STATUS_NAMES = {
    LineStatus.NEW: "جدید",
    LineStatus.EXISTING_MATCH: "منطبق",
    LineStatus.CONFLICT: "تعارض",
    LineStatus.ERROR: "خطا",
    LineStatus.IGNORED: "نادیده",
}
RESOLUTION_NAMES = {
    Resolution.CREATE: "ایجاد کالای جدید",
    Resolution.MATCH: "استفاده از کالای موجود",
    Resolution.OVERWRITE: "بازنویسی کالای موجود",
    Resolution.IGNORE: "نادیده گرفتن",
}
KIND_NAMES = {ImportKind.ITEMS: "تعریف کالا", ImportKind.STOCK: "اقلام سند"}
SOURCE_NAMES = {
    ImportSource.EXCEL: "اکسل", ImportSource.CSV: "CSV", ImportSource.WORD: "Word",
    ImportSource.SCAN: "اسکن بارکد", ImportSource.TEXT: "متن (دستیار)",
    ImportSource.MANUAL: "ورود دستی",
}


# ----- matching -----


@dataclass
class ItemIndex:
    by_code: dict[str, int]
    by_barcode: dict[str, tuple[int, int | None]]
    names: dict[int, str]  # normalized names for fuzzy search
    display: dict[int, str]
    units_of: dict[int, set[int]]  # item -> unit ids it may be counted in
    unit_by_name: dict[str, int]  # normalized unit name -> id (active units)


async def build_index(s: AsyncSession) -> ItemIndex:
    rows = (await s.scalars(select(Item).where(Item.is_active))).all()
    barcodes = (await s.execute(
        select(ItemBarcode.barcode, ItemBarcode.item_id, ItemBarcode.unit_id)
        .join(Item, Item.id == ItemBarcode.item_id).where(Item.is_active)
    )).all()
    units = (await s.scalars(select(Unit).where(Unit.is_active))).all()
    return ItemIndex(
        by_code={i.code: i.id for i in rows},
        by_barcode={b: (i, u) for b, i, u in barcodes},
        names={i.id: i.name_normalized for i in rows},
        display={i.id: i.name for i in rows},
        units_of={i.id: {i.base_unit_id, *(u.unit_id for u in i.units)} for i in rows},
        unit_by_name={normalize(u.name): u.id for u in units},
    )


def fuzzy_candidates(index: ItemIndex, name: str, limit: int = 3) -> list[tuple[int, int]]:
    query = normalize(name)
    if not query or not index.names:
        return []
    hits = process.extract(query, index.names, scorer=fuzz.WRatio, limit=limit,
                           score_cutoff=SUGGEST)
    return [(item_id, round(score)) for _name, score, item_id in hits]


@dataclass
class Evaluation:
    status: LineStatus
    message: str = ""
    match_item_id: int | None = None
    match_score: int | None = None
    candidates: list | None = None


def evaluate(index: ItemIndex, kind: ImportKind, line: ImportLine,
             seen: dict[str, int]) -> Evaluation:
    """Classify one line. `seen` tracks codes/barcodes already used earlier in the file."""
    if line.parse_error:
        return Evaluation(LineStatus.ERROR, line.parse_error)
    if not (line.code or line.name or line.barcode):
        return Evaluation(LineStatus.IGNORED, "ردیف خالی")

    if kind == ImportKind.STOCK and (line.qty is None or line.qty <= 0):
        return Evaluation(LineStatus.ERROR, "مقدار باید بزرگ‌تر از صفر باشد.")
    unit_id = None
    if line.unit_name:
        unit_id = index.unit_by_name.get(normalize(line.unit_name))
        if unit_id is None:
            return Evaluation(LineStatus.ERROR, f"واحد «{line.unit_name}» تعریف نشده است.")

    if kind == ImportKind.ITEMS:
        for key, label in ((line.code, "کد"), (line.barcode, "بارکد")):
            if key:
                if key in seen:
                    return Evaluation(LineStatus.CONFLICT,
                                      f"{label} «{key}» در ردیف {seen[key]} همین فایل تکرار شده است.")
                seen[key] = line.row_no

    by_code = index.by_code.get(line.code) if line.code else None
    by_barcode = index.by_barcode.get(line.barcode, (None, None))[0] if line.barcode else None
    if by_code and by_barcode and by_code != by_barcode:
        return Evaluation(LineStatus.CONFLICT, "کد و بارکد به دو کالای متفاوت اشاره دارند.",
                          candidates=[[by_code, 100], [by_barcode, 100]])
    exact = by_code or by_barcode
    if exact:
        if kind == ImportKind.ITEMS and line.name:
            score = round(fuzz.WRatio(normalize(line.name), index.names[exact]))
            if score < 60:
                return Evaluation(
                    LineStatus.CONFLICT,
                    f"کد/بارکد موجود است اما نام متفاوت است («{index.display[exact]}»).",
                    exact, score, [[exact, score]])
        if kind == ImportKind.STOCK and unit_id and unit_id not in index.units_of[exact]:
            return Evaluation(LineStatus.ERROR,
                              f"واحد «{line.unit_name}» برای «{index.display[exact]}» تعریف نشده.",
                              exact)
        return Evaluation(LineStatus.EXISTING_MATCH, "", exact, 100)

    if line.name:
        found = fuzzy_candidates(index, line.name)
        if found:
            best_id, best = found[0]
            second = found[1][1] if len(found) > 1 else 0
            alternatives = [[i, sc] for i, sc in found]
            # Two items scoring (almost) the same — e.g. two active «جارو» — must be chosen by a person,
            # never guessed (QA round 1, #3).
            ambiguous = second >= best - AMBIGUOUS_GAP
            if best >= 99 and kind == ImportKind.STOCK and not ambiguous:
                return Evaluation(LineStatus.EXISTING_MATCH, "", best_id, best, alternatives)
            if kind == ImportKind.STOCK and best >= AUTO_MATCH and best - second >= 6:
                return Evaluation(LineStatus.EXISTING_MATCH,
                                  f"تطبیق تقریبی با «{index.display[best_id]}» — بررسی کنید.",
                                  best_id, best, alternatives)
            what = "احتمالاً همان" if kind == ImportKind.ITEMS else "شبیه"
            return Evaluation(LineStatus.CONFLICT,
                              f"{what} «{index.display[best_id]}» است؟ کالای موجود را انتخاب "
                              "کنید یا کالای جدید بسازید.",
                              best_id, best, [[i, sc] for i, sc in found])
        return Evaluation(LineStatus.NEW, "کالای جدید")
    return Evaluation(LineStatus.NEW, "کالای جدید — نام کالا را تکمیل کنید.")


def default_resolution(status: LineStatus) -> Resolution | None:
    return {
        LineStatus.NEW: Resolution.CREATE,
        LineStatus.EXISTING_MATCH: Resolution.MATCH,
        LineStatus.IGNORED: Resolution.IGNORE,
    }.get(status)


def allowed_resolutions(kind: ImportKind, line: ImportLine) -> list[Resolution]:
    options = []
    if line.status != LineStatus.ERROR and line.name:
        options.append(Resolution.CREATE)
    if line.status != LineStatus.ERROR and (line.match_item_id or line.candidates):
        options.append(Resolution.MATCH)
        if kind == ImportKind.ITEMS:
            options.append(Resolution.OVERWRITE)
    options.append(Resolution.IGNORE)
    return options


async def _evaluate_batch(s: AsyncSession, batch: ImportBatch) -> None:
    """(Re)classify all lines; keeps a user's decision if the status didn't change."""
    index = await build_index(s)
    seen: dict[str, int] = {}
    for line in batch.lines:
        old_status = line.status
        result = evaluate(index, batch.kind, line, seen)
        line.status = result.status
        line.message = result.message
        line.match_item_id = result.match_item_id
        line.match_score = result.match_score
        line.candidates = result.candidates
        if old_status != line.status or line.resolution is None:
            default = default_resolution(line.status)
            line.resolution = default if default in allowed_resolutions(batch.kind, line) else None


# ----- views -----


@dataclass(frozen=True)
class BatchRow:
    id: int
    kind: ImportKind
    source: ImportSource
    status: BatchStatus
    title: str
    created_at: dt.datetime
    counts: dict[LineStatus, int]
    unresolved: int

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True)
class LineRow:
    id: int
    row_no: int
    status: LineStatus
    code: str
    name: str
    barcode: str
    qty: Decimal | None
    unit_name: str
    unit_price: Decimal | None
    category_name: str
    reorder_point: Decimal | None
    message: str
    match_item_id: int | None
    match_name: str
    candidates: list[tuple[int, str, int]]  # (item_id, name, score)
    resolution: Resolution | None
    allowed: list[Resolution]
    match_code: str = ""  # code of the matched item (the file often has none)


@dataclass(frozen=True)
class BatchDetail:
    row: BatchRow
    doc_type: DocType | None
    warehouse_id: int | None
    person_id: int | None
    result_document_id: int | None
    lines: list[LineRow]


def _batch_row(b: ImportBatch) -> BatchRow:
    counts = {st: 0 for st in LineStatus}
    unresolved = 0
    for line in b.lines:
        counts[line.status] += 1
        unresolved += line.resolution is None
    return BatchRow(b.id, b.kind, b.source, b.status, b.title, b.created_at, counts, unresolved)


async def list_batches(db: Database, actor: Actor, status: BatchStatus | None = BatchStatus.OPEN,
                       limit: int = 100) -> list[BatchRow]:
    actor.require(Perm.IMPORT_RUN)
    stmt = select(ImportBatch).order_by(ImportBatch.id.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(ImportBatch.status == status)
    async with db.session() as s:
        return [_batch_row(b) for b in (await s.scalars(stmt)).all()]


async def open_batch_count(db: Database) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(ImportBatch)
                              .where(ImportBatch.status == BatchStatus.OPEN)) or 0


async def _load(s: AsyncSession, batch_id: int) -> ImportBatch:
    batch = await s.get(ImportBatch, batch_id)
    if batch is None:
        raise NotFound("پیش‌نویس ورود اطلاعات پیدا نشد.")
    return batch


def _require_open(batch: ImportBatch) -> None:
    if batch.status != BatchStatus.OPEN:
        raise ValidationError("این پیش‌نویس قبلاً اعمال یا حذف شده است.")


async def get_batch(db: Database, actor: Actor, batch_id: int) -> BatchDetail:
    actor.require(Perm.IMPORT_RUN)
    async with db.session() as s:
        batch = await _load(s, batch_id)
        ids = {ln.match_item_id for ln in batch.lines if ln.match_item_id}
        for ln in batch.lines:
            ids.update(c[0] for c in ln.candidates or [])
        found = (await s.execute(select(Item.id, Item.name, Item.code).where(Item.id.in_(ids)))).all() \
            if ids else []
        names = {i: n for i, n, _c in found}
        codes = {i: c for i, _n, c in found}
        lines = [
            LineRow(
                ln.id, ln.row_no, ln.status, ln.code, ln.name, ln.barcode, ln.qty, ln.unit_name,
                ln.unit_price, ln.category_name, ln.reorder_point, ln.message, ln.match_item_id,
                names.get(ln.match_item_id, "") if ln.match_item_id else "",
                [(c[0], names.get(c[0], "?"), c[1]) for c in ln.candidates or []],
                ln.resolution, allowed_resolutions(batch.kind, ln),
                codes.get(ln.match_item_id, "") if ln.match_item_id else "",
            )
            for ln in batch.lines
        ]
        return BatchDetail(_batch_row(batch), batch.doc_type, batch.warehouse_id,
                           batch.person_id, batch.result_document_id, lines)


# ----- commands -----


async def create_batch(
    db: Database, actor: Actor, kind: ImportKind, source: ImportSource, rows: list[RawRow],
    title: str = "", doc_type: DocType | None = None, warehouse_id: int | None = None,
    person_id: int | None = None,
) -> int:
    """Store rows as a draft batch and classify them. Allowed for the AI (it's a draft)."""
    actor.require(Perm.IMPORT_RUN)
    if not rows:
        raise ValidationError("هیچ ردیفی برای ورود وجود ندارد.")
    if kind == ImportKind.STOCK:
        if doc_type is None or warehouse_id is None:
            raise ValidationError("نوع سند و انبار را برای ورود اقلام مشخص کنید.")
        if doc_type in (DocType.TRANSFER, DocType.LOAN_RETURN):
            raise ValidationError("این نوع سند از طریق ورود اطلاعات پشتیبانی نمی‌شود.")
    async with db.session(actor.user_id) as s:
        if warehouse_id is not None and await s.get(Warehouse, warehouse_id) is None:
            raise ValidationError("انبار نامعتبر است.")
        if person_id is not None and await s.get(Person, person_id) is None:
            raise ValidationError("طرف حساب نامعتبر است.")
        batch = ImportBatch(kind=kind, source=source, title=title[:255], doc_type=doc_type,
                            warehouse_id=warehouse_id, person_id=person_id)
        for no, r in enumerate(rows, start=1):
            batch.lines.append(ImportLine(
                row_no=no, raw={k: str(v) for k, v in (r.raw or {}).items()},
                code=to_ascii_digits(r.code.strip())[:64], name=" ".join(r.name.split())[:255],
                barcode=to_ascii_digits(r.barcode.strip())[:64], unit_name=r.unit_name.strip(),
                category_name=r.category_name.strip(), qty=r.qty, unit_price=r.unit_price,
                reorder_point=r.reorder_point,
                status=LineStatus.NEW, parse_error=r.error,
            ))
        s.add(batch)
        await s.flush()
        await _evaluate_batch(s, batch)
        audit.record(s, actor, "import.batch_created", "import_batch", batch.id,
                     {"kind": kind.value, "source": source.value, "rows": len(rows)})
        return batch.id


EDITABLE = {"code", "name", "barcode", "qty", "unit_name", "unit_price", "category_name",
            "reorder_point"}


async def update_line(db: Database, actor: Actor, line_id: int, **values) -> None:
    """Correct a line's data; the batch is re-matched."""
    actor.require(Perm.IMPORT_RUN)
    if unknown := set(values) - EDITABLE:
        raise ValidationError(f"فیلد نامعتبر: {', '.join(unknown)}")
    async with db.session(actor.user_id) as s:
        line = await s.get(ImportLine, line_id)
        if line is None:
            raise NotFound("ردیف پیدا نشد.")
        batch = await _load(s, line.batch_id)
        _require_open(batch)
        for key, value in values.items():
            if isinstance(value, str):
                value = value.strip()
                if key in ("code", "barcode"):
                    value = to_ascii_digits(value)
            setattr(line, key, value)
        line.parse_error = ""  # the user corrected the data
        line.resolution = None
        await _evaluate_batch(s, batch)


async def set_resolution(db: Database, actor: Actor, line_id: int, resolution: Resolution,
                         item_id: int | None = None) -> None:
    actor.require(Perm.IMPORT_RUN)
    async with db.session(actor.user_id) as s:
        line = await s.get(ImportLine, line_id)
        if line is None:
            raise NotFound("ردیف پیدا نشد.")
        batch = await _load(s, line.batch_id)
        _require_open(batch)
        if item_id is not None:
            valid = {line.match_item_id, *(c[0] for c in line.candidates or [])}
            if item_id not in valid and await s.get(Item, item_id) is None:
                raise ValidationError("کالای انتخاب‌شده نامعتبر است.")
            line.match_item_id = item_id
        if resolution not in allowed_resolutions(batch.kind, line):
            raise ValidationError("این اقدام برای این ردیف مجاز نیست.")
        line.resolution = resolution


def variant_name(similar_name: str) -> str:
    """Starting text for «نسخه جدید از همین کالا»: the similar item's name, ready for the brand
    or size to be appended (e.g. «مایع ظرفشویی ۱ لیتری - ۲ لیتری برند X»)."""
    return f"{similar_name} - "


async def create_variant(db: Database, actor: Actor, line_id: int, name: str) -> None:
    """Resolve a row as a NEW item (a variant of a similar one) under the name the user edited."""
    actor.require(Perm.IMPORT_RUN)
    name = " ".join(name.split())[:255]
    if not name:
        raise ValidationError("نام کالای جدید را بنویسید.")
    async with db.session(actor.user_id) as s:
        line = await s.get(ImportLine, line_id)
        if line is None:
            raise NotFound("ردیف پیدا نشد.")
        batch = await _load(s, line.batch_id)
        _require_open(batch)
        line.name = name
        await _evaluate_batch(s, batch)
        if Resolution.CREATE not in allowed_resolutions(batch.kind, line):
            raise ValidationError(line.message or "برای این ردیف نمی‌توان کالای جدید ساخت.")
        line.resolution = Resolution.CREATE


async def discard_batch(db: Database, actor: Actor, batch_id: int) -> None:
    actor.require_human("حذف پیش‌نویس ورود اطلاعات")
    actor.require(Perm.IMPORT_RUN)
    async with db.session(actor.user_id) as s:
        batch = await _load(s, batch_id)
        _require_open(batch)
        batch.status = BatchStatus.DISCARDED
        audit.record(s, actor, "import.batch_discarded", "import_batch", batch.id)


async def reopen_for_deleted_document(s: AsyncSession, actor: Actor, document_id: int) -> None:
    """The draft a batch produced is being deleted: put the batch back in review.

    Lines are re-matched, so items created by the first apply are now EXISTING_MATCH and a
    second apply does not create them again.
    """
    batches = (await s.scalars(select(ImportBatch)
                               .where(ImportBatch.result_document_id == document_id))).all()
    for batch in batches:
        batch.result_document_id = None
        batch.status = BatchStatus.OPEN
        batch.applied_at = batch.applied_by_id = None
        await _evaluate_batch(s, batch)
        audit.record(s, actor, "import.batch_reopened", "import_batch", batch.id,
                     {"deleted_document_id": document_id})


def needs_overwrite_approval(detail: BatchDetail) -> bool:
    return any(ln.resolution == Resolution.OVERWRITE for ln in detail.lines)


async def _category_id(s: AsyncSession, name: str, cache: dict[str, int]) -> int | None:
    if not name:
        return None
    key = normalize(name)
    if key not in cache:
        existing = await s.scalars(select(Category))
        for cat in existing.all():
            cache.setdefault(normalize(cat.name), cat.id)
    if key not in cache:
        cat = Category(name=" ".join(name.split()))
        s.add(cat)
        await s.flush()
        cache[key] = cat.id
    return cache[key]


@dataclass(frozen=True)
class ApplyResult:
    created_items: int
    updated_items: int
    document_id: int | None


async def apply_batch(db: Database, actor: Actor, batch_id: int,
                      approval: Approval | None = None) -> ApplyResult:
    """Write the reviewed batch: new/updated items and (for stock) a DRAFT document."""
    if actor.is_ai:
        raise PermissionDenied("دستیار هوشمند فقط پیش‌نویس می‌سازد؛ اعمال آن با کاربر است.")
    actor.require(Perm.IMPORT_RUN)
    async with db.session(actor.user_id) as s:
        batch = await _load(s, batch_id)
        _require_open(batch)
        unresolved = [ln.row_no for ln in batch.lines if ln.resolution is None]
        if unresolved:
            rows = "، ".join(str(n) for n in unresolved[:10])
            raise ValidationError(f"{len(unresolved)} ردیف نیاز به تصمیم دارد (ردیف‌های {rows}).")
        for ln in batch.lines:
            if ln.resolution == Resolution.CREATE and not ln.name:
                raise ValidationError(f"ردیف {ln.row_no}: برای ایجاد کالا، نام لازم است.")
            if ln.resolution in (Resolution.MATCH, Resolution.OVERWRITE) and not ln.match_item_id:
                raise ValidationError(f"ردیف {ln.row_no}: کالای موجود انتخاب نشده است.")
        overwrites = [ln for ln in batch.lines if ln.resolution == Resolution.OVERWRITE]
        approver_id = consume(approval, ProtectedAction.IMPORT_OVERWRITE, actor) \
            if overwrites else None

        units = {normalize(u.name): u.id for u in (await s.scalars(select(Unit))).all()}
        categories: dict[str, int] = {}
        created = updated = 0
        item_for_line: dict[int, int] = {}
        for ln in batch.lines:
            if ln.resolution == Resolution.IGNORE:
                continue
            if ln.resolution == Resolution.CREATE:
                unit_id = units.get(normalize(ln.unit_name or DEFAULT_UNIT)) \
                    or units[normalize(DEFAULT_UNIT)]
                item = await items.create_item_in(s, actor, ItemInput(
                    code=ln.code or await items.next_code_in(s), name=ln.name,
                    base_unit_id=unit_id,
                    category_id=await _category_id(s, ln.category_name, categories),
                    reorder_point=ln.reorder_point,
                    barcodes=[(ln.barcode, None)] if ln.barcode else [],
                ))
                item_for_line[ln.id] = item.id
                created += 1
            elif ln.resolution == Resolution.OVERWRITE:
                actor.require(Perm.ITEMS_EDIT)
                item = await s.get(Item, ln.match_item_id)
                before = {"name": item.name, "reorder_point": str(item.reorder_point)}
                if ln.name:
                    item.name, item.name_normalized = ln.name, normalize(ln.name)
                if ln.category_name:
                    item.category_id = await _category_id(s, ln.category_name, categories)
                if ln.reorder_point is not None:
                    item.reorder_point = ln.reorder_point
                if ln.barcode and ln.barcode not in {b.barcode for b in item.barcodes}:
                    owner = await s.scalar(select(ItemBarcode.item_id)
                                           .where(ItemBarcode.barcode == ln.barcode))
                    if owner:
                        raise ValidationError(f"ردیف {ln.row_no}: بارکد متعلق به کالای دیگری است.")
                    item.barcodes.append(ItemBarcode(barcode=ln.barcode))
                audit.record(s, actor, "item.overwritten_by_import", "item", item.id,
                             {"before": before, "batch": batch.id}, approved_by_id=approver_id)
                item_for_line[ln.id] = item.id
                updated += 1
            else:
                item_for_line[ln.id] = ln.match_item_id

        document_id = None
        if batch.kind == ImportKind.STOCK:
            doc_lines = []
            for ln in batch.lines:
                if ln.id not in item_for_line:
                    continue
                item = await s.get(Item, item_for_line[ln.id])
                unit_id = units.get(normalize(ln.unit_name)) if ln.unit_name else None
                allowed = {item.base_unit_id, *(u.unit_id for u in item.units)}
                doc_lines.append(documents.LineInput(
                    item.id, unit_id if unit_id in allowed else item.base_unit_id, ln.qty,
                    ln.unit_price, f"ورود اطلاعات — ردیف {ln.row_no}"))
            if not doc_lines:
                raise ValidationError("همه ردیف‌ها نادیده گرفته شده‌اند؛ سندی ساخته نمی‌شود.")
            doc = await documents.create_document_in(s, actor, documents.DocumentInput(
                batch.doc_type, dt.date.today(), batch.warehouse_id, doc_lines,
                person_id=batch.person_id, description=f"از ورود اطلاعات: {batch.title}"[:500]))
            document_id = batch.result_document_id = doc.id

        batch.status = BatchStatus.APPLIED
        batch.applied_at = dt.datetime.now()
        batch.applied_by_id = actor.user_id
        audit.record(s, actor, "import.batch_applied", "import_batch", batch.id,
                     {"created": created, "updated": updated, "document_id": document_id},
                     approved_by_id=approver_id)
        return ApplyResult(created, updated, document_id)
