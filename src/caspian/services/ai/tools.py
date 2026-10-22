"""Tools the AI assistant may call.

Every tool runs as `actor.as_ai()`: the signed-in user's permissions, flagged as AI so
protected actions and applying drafts are refused by the services themselves. Besides the
read-only tools the assistant can prepare warehouse documents (`create_document`) and goods requests
for the owners (`create_goods_request`) — always as DRAFTS; a request is sent only after the person's
«بله، بفرست», and approving, printing or posting is never the assistant's. Anything that changes
stock is confirmed by the user on a card in the chat («بله، ثبت کن») and then posted by the person,
never by the assistant: `documents.post_document_in` refuses AI actors. There is deliberately no
tool to post, cancel, delete, deactivate, merge, restore or change roles/settings, and the services
refuse those for AI actors anyway (`Actor.require_human`).
"""

import datetime as dt
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from rapidfuzz import fuzz

from caspian.core import jalali
from caspian.core.permissions import Perm
from caspian.core.text import normalize, normalize_location, to_ascii_digits
from caspian.db.database import Database
from caspian.db.models import DocType, ImportKind, ImportSource, RequestStatus
from caspian.services import documents, imports, items, locations, master, reports
from caspian.services.actor import Actor
from caspian.services.errors import ServiceError
from caspian.services.import_files import RawRow
from caspian.services.server_link import goods

log = logging.getLogger(__name__)

MAX_ROWS = 50


@dataclass
class ToolContext:
    db: Database
    actor: Actor  # already restricted with as_ai()
    created_batches: list[int]
    messenger: object | None = None  # caspian.services.messaging.Messenger
    created_documents: list[dict] = field(default_factory=list)
    signatures: dict[str, dict] = field(default_factory=dict)  # one create per request (#4)
    created_requests: list[dict] = field(default_factory=list)  # goods requests (drafts) to confirm


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], Awaitable[Any]]
    permission: Perm | None = None

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def _num(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required)}


# ----- resolving names the user said -----

PERSON_MATCH = 85  # fuzzy score accepted for a person's name
CLEAR_GAP = 8  # the best match must beat the next one by this much


async def _resolve_person(ctx: ToolContext, name: str) -> tuple[int | None, dict | None]:
    """(person_id, None) for one clear match, else (None, a «needs_choice» answer for the model)."""
    name = " ".join(str(name or "").split())
    if not name:
        return None, None
    persons = await master.search_persons(ctx.db)
    key = _strip_titles(normalize(name))
    scored = sorted(((round(fuzz.WRatio(key, _strip_titles(normalize(p.name)))), p) for p in persons),
                    key=lambda t: -t[0])
    exact = [p for sc, p in scored if _strip_titles(normalize(p.name)) == key]
    if len(exact) == 1:
        return exact[0].id, None
    good = [(sc, p) for sc, p in scored if sc >= PERSON_MATCH]
    if len(good) == 1 or (len(good) > 1 and good[0][0] - good[1][0] >= CLEAR_GAP and not exact):
        return good[0][1].id, None
    candidates = [{"name": p.name, "kind": p.kind_name, "score": sc} for sc, p in (good or scored)[:5]]
    return None, {"needs_choice": "person", "asked": name, "candidates": candidates,
                  "note": "شخص به‌طور قطعی پیدا نشد؛ از کاربر بپرس کدام است (یا شخص جدید باید تعریف شود)."}


_TITLES = ("آقای ", "خانم ", "جناب ", "سرکار ")


def _strip_titles(text: str) -> str:
    for title in _TITLES:
        text = text.replace(normalize(title), "")
    return text.strip()


async def _item_candidates(ctx: ToolContext, ids: list[tuple[int, int]]) -> list[dict]:
    out = []
    for item_id, score in ids:
        detail = await items.get_item(ctx.db, ctx.actor, item_id)
        stock = None
        if ctx.actor.can(Perm.STOCK_VIEW):
            stock = _num(sum((q for _w, q in await documents.stock_by_warehouse(ctx.db, item_id)),
                             Decimal(0)))
        out.append({"code": detail.input.code, "name": detail.input.name, "stock": stock, "score": score})
    return out


async def _resolve_item(ctx: ToolContext, line: dict) -> tuple[int | None, dict | None]:
    """Same matching rules as imports (exact code/barcode first, then names; ties are asked)."""
    code = to_ascii_digits(str(line.get("code", "") or "").strip())
    barcode = to_ascii_digits(str(line.get("barcode", "") or "").strip())
    name = str(line.get("name", "") or "").strip()
    if barcode and (hit := await items.lookup_barcode(ctx.db, barcode)):
        return hit[0], None
    async with ctx.db.session() as s:
        index = await imports.build_index(s)
    if code and code in index.by_code:
        return index.by_code[code], None
    if not name:
        return None, {"needs_choice": "item", "asked": code or barcode, "candidates": [],
                      "note": "کالایی با این کد/بارکد پیدا نشد."}
    found = imports.fuzzy_candidates(index, name, limit=5)
    exact = [i for i, n in index.names.items() if n == normalize(name)]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) == 0 and len(found) == 1 and found[0][1] >= imports.AUTO_MATCH:
        return found[0][0], None  # the only candidate there is; the user still confirms it on the card
    # Several similar items: never pick one — the user says which (QA: «یه شمع» must not guess).
    pool = [(i, 100) for i in exact] or found
    return None, {"needs_choice": "item", "asked": name, "candidates": await _item_candidates(ctx, pool),
                  "note": f"چند کالا با «{name}» مطابقت دارد یا کالا پیدا نشد؛ از کاربر بپرس کدام "
                          "(کد و موجودی را نشان بده)."}


def _signature(doc_type: str, person: str, warehouse: str, lines: list[dict]) -> str:
    rows = sorted(f"{normalize(str(ln.get('code') or ln.get('name') or ln.get('barcode') or ''))}"
                  f"|{ln.get('qty')}|{normalize(str(ln.get('unit') or ''))}" for ln in lines)
    return "\n".join([doc_type, normalize(person or ""), normalize(warehouse or ""), *rows])


# ----- handlers -----


async def _search_items(ctx: ToolContext, args: dict) -> list[dict]:
    rows = await items.search_items(ctx.db, ctx.actor, str(args.get("query", "")),
                                    limit=min(int(args.get("limit", 15)), MAX_ROWS))
    return [{"code": r.code, "name": r.name, "category": r.category, "unit": r.base_unit,
             "on_hand": _num(r.on_hand), "reorder_point": _num(r.reorder_point)} for r in rows]


async def _item_stock(ctx: ToolContext, args: dict) -> dict:
    ctx.actor.require(Perm.STOCK_VIEW)
    rows = await items.search_items(ctx.db, ctx.actor, str(args["code"]), limit=5)
    item = next((r for r in rows if r.code == str(args["code"])), rows[0] if rows else None)
    if item is None:
        return {"error": "کالا پیدا نشد"}
    per_wh = await documents.stock_by_warehouse(ctx.db, item.id)
    return {"code": item.code, "name": item.name, "unit": item.base_unit,
            "total": _num(item.on_hand),
            "by_warehouse": [{"warehouse": w, "qty": _num(q)} for w, q in per_wh]}


async def _reorder_analysis(ctx: ToolContext, args: dict) -> list[dict]:
    params = reports.ReorderParams(
        lookback_days=int(args.get("lookback_days", 90)),
        lead_time_days=int(args.get("lead_time_days", 14)),
        cover_days=int(args.get("cover_days", 60)), safety_days=int(args.get("safety_days", 7)))
    rates = await reports.burn_rates(ctx.db, ctx.actor, params,
                                     only_needing_order=bool(args.get("only_needing_order", True)))
    code = args.get("code")
    if code:
        rates = [r for r in rates if r.code == str(code)] or [
            r for r in await reports.burn_rates(ctx.db, ctx.actor, params) if r.code == str(code)]
    return [{"code": r.code, "name": r.name, "unit": r.unit, "on_hand": _num(r.on_hand),
             "daily_usage": round(float(r.daily), 3), "coverage_days": r.coverage_days,
             "reorder_point": _num(r.reorder_point),
             "suggested_reorder_point": _num(r.suggested_reorder_point),
             "suggested_order_qty": _num(r.suggested_order_qty),
             "summary": r.suggestion_text} for r in rates[:MAX_ROWS]]


async def _find_location(ctx: ToolContext, args: dict) -> dict:
    """Consumption locations matching the user's words (plate, name or what staff call it)."""
    index = await locations.load_index(ctx.db)
    text = str(args.get("query", "") or "")
    try:
        exact = index.exact(text)
        found = [exact] if exact else index.search(text, limit=10)
    except locations.Phase1Plate as exc:
        return {"error": exc.message}
    return {"locations": [{"code": loc.code, "name": loc.label(), "floor": loc.floor.display if loc.floor
                           else "طبقه هنگام ثبت انتخاب می‌شود", "needs_floor": loc.needs_floor}
                          for loc in found],
            "floors": [{"code": f.code, "name": f.display} for f in index.floors if f.is_active]}


def _period_of(args: dict) -> tuple[dt.date | None, dt.date | None]:
    """«this_month» (default), «last_30_days», «this_year», or Jalali date_from/date_to."""
    today = dt.date.today()
    date_from = date_to = None
    for key in ("date_from", "date_to"):
        text = str(args.get(key, "") or "").strip()
        if text:
            value = jalali.parse_date(to_ascii_digits(text))
            if key == "date_from":
                date_from = value
            else:
                date_to = value
    if date_from or date_to:
        return date_from, date_to
    period = str(args.get("period", "this_month") or "this_month")
    if period == "last_30_days":
        return today - dt.timedelta(days=30), today
    if period == "this_year":
        return jalali.fiscal_year_bounds(jalali.fiscal_year_of(today))[0], today
    if period == "all":
        return None, None
    return reports.month_start(today), today


async def _consumption_report(ctx: ToolContext, args: dict) -> dict:
    """Consumption grouped by floor or by location for a period, optionally for one item/person."""
    group_by = "location" if str(args.get("group_by", "floor")) == "location" else "floor"
    try:
        date_from, date_to = _period_of(args)
    except ValueError:
        return {"error": "تاریخ نامعتبر است (نمونه: ۱۴۰۵/۰۷/۰۱)."}
    item_id = person_id = None
    if args.get("item"):
        item_id, question = await _resolve_item(ctx, {"name": str(args["item"]), "code": str(args["item"])})
        if question:
            return question
    if args.get("person"):
        person_id, question = await _resolve_person(ctx, str(args["person"]))
        if question:
            return question
    rows = await reports.consumption(ctx.db, ctx.actor, group_by, date_from, date_to, item_id, person_id)
    floor_code = str(args.get("floor", "") or "").strip()
    if floor_code:  # «مصرف همکف»: only that floor (by code or Persian name)
        index = await locations.load_index(ctx.db)
        key = normalize_location(floor_code)
        wanted = {f.code for f in index.floors if key in {normalize_location(f.code),
                                                          normalize_location(f.name_fa),
                                                          normalize_location(f.display)}}
        rows = [r for r in rows if r.floor_code in wanted]
    return {"period": reports._period(date_from, date_to), "group_by": group_by,
            "rows": [{"location_code": r.location_code, "location": r.location, "floor_code": r.floor_code,
                      "floor": r.floor, "vouchers": r.documents, "items": r.items,
                      "qty": _num(r.qty) if item_id else None, "value_rial": _num(r.value)}
                     for r in rows if r.documents or group_by == "floor"][:MAX_ROWS]}


async def _recent_documents(ctx: ToolContext, args: dict) -> list[dict]:
    doc_type = args.get("doc_type")
    rows = await documents.list_documents(ctx.db, ctx.actor,
                                          DocType(doc_type) if doc_type else None,
                                          limit=min(int(args.get("limit", 10)), MAX_ROWS))
    return [{"number": r.number, "type": r.type_name, "date": jalali.format_date(r.doc_date, False),
             "status": r.status_name, "warehouse": r.warehouse, "person": r.person,
             "lines": r.line_count} for r in rows]


async def _open_loans(ctx: ToolContext, args: dict) -> list[dict]:
    rows = await documents.outstanding_loans(ctx.db, ctx.actor)
    return [{"loan_number": r.number, "date": jalali.format_date(r.doc_date, False),
             "person": r.person, "item": r.item_name, "outstanding": _num(r.outstanding),
             "unit": r.base_unit, "days_out": r.days_out} for r in rows[:MAX_ROWS]]


async def _suppliers(ctx: ToolContext, args: dict) -> list[dict]:
    rows = await master.search_persons(ctx.db, str(args.get("query", "")),
                                       master.PersonKind.SUPPLIER, limit=20)
    return [{"name": p.name, "phone": p.phone} for p in rows]


async def _create_stock_draft(ctx: ToolContext, args: dict) -> dict:
    doc_type = DocType(args.get("doc_type", "RECEIPT"))
    if doc_type not in (DocType.RECEIPT, DocType.ISSUE):
        return {"error": "فقط پیش‌نویس رسید یا حواله قابل ساخت است."}
    lines_in = list(args.get("lines", []))[:200]
    signature = "draft\n" + _signature(doc_type.value, str(args.get("person", "")),
                                         str(args.get("warehouse", "")), lines_in)
    if signature in ctx.signatures:
        return {**ctx.signatures[signature], "duplicate": True,
                "note": "این پیش‌نویس در همین درخواست قبلاً ساخته شده است؛ دوباره ساخته نشد."}
    person_id, question = await _resolve_person(ctx, str(args.get("person", "")))
    if question:
        return question
    warehouses = await master.list_warehouses(ctx.db)
    wh = next((w for w in warehouses if w.name == args.get("warehouse")), warehouses[0])
    rows = []
    for line in lines_in:
        qty = line.get("qty")
        rows.append(RawRow(
            code=str(line.get("code", "") or ""), name=str(line.get("name", "") or ""),
            barcode=str(line.get("barcode", "") or ""),
            qty=Decimal(str(qty)) if qty not in (None, "") else None,
            unit_name=str(line.get("unit", "") or ""),
            raw={k: str(v) for k, v in line.items()}))
    batch_id = await imports.create_batch(
        ctx.db, ctx.actor, ImportKind.STOCK, ImportSource.TEXT, rows,
        str(args.get("title", "پیش‌نویس دستیار"))[:200], doc_type, wh.id, person_id)
    ctx.created_batches.append(batch_id)
    detail = await imports.get_batch(ctx.db, ctx.actor, batch_id)
    result = {"draft_id": batch_id, "warehouse": wh.name,
              "status_counts": {k.value: v for k, v in detail.row.counts.items() if v},
              "needs_review": detail.row.unresolved,
              "note": "پیش‌نویس ساخته شد و باید توسط کاربر در «ورود اطلاعات» بررسی و اعمال شود."}
    ctx.signatures[signature] = result
    return result


async def _resolve_place(ctx: ToolContext, args: dict) -> tuple[dict, dict | None]:
    """Consumption place of an issue from the user's words: ({location_id, floor_id}, question)."""
    index = await locations.load_index(ctx.db)
    text = str(args.get("location", "") or "").strip()
    if not text:
        async with ctx.db.session() as s:
            required = await locations.required_on_issue_in(s)
        if not required:
            return {}, None
        return {}, {"needs_choice": "location", "candidates": [],
                    "note": "محل مصرف حواله لازم است؛ از کاربر بپرس کجا مصرف شده (شماره پلاک یا نام محل، "
                            "مثل «پلاک ۶۸» یا «آسانسور باری اسدنژاد»)."}
    try:
        loc = index.exact(text)
        found = [] if loc else index.search(text, limit=8)
    except locations.Phase1Plate as exc:
        return {}, {"error": exc.message}
    if loc is None and len(found) == 1:
        loc = found[0]
    if loc is None:
        return {}, {"needs_choice": "location", "candidates": [x.label() for x in found],
                    "note": "محل مصرف مشخص نیست؛ از کاربر بپرس کدام." if found
                    else "محلی با این نام پیدا نشد؛ از کاربر بپرس."}
    if not loc.needs_floor:
        return {"location_id": loc.id}, None
    key = normalize_location(str(args.get("floor", "") or ""))
    floor = next((f for f in index.floors if f.is_active and key and key in {
        normalize_location(f.code), normalize_location(f.name_fa), normalize_location(f.display)}), None)
    if floor is None:
        return {}, {"needs_choice": "floor", "candidates": [f.display for f in index.floors if f.is_active],
                    "note": f"برای «{loc.name_fa}» طبقه لازم است؛ از کاربر بپرس کدام طبقه."}
    return {"location_id": loc.id, "floor_id": floor.id}, None


async def _create_document(ctx: ToolContext, args: dict) -> dict:
    """A real warehouse document (draft), optionally posted — like a user would in «اسناد انبار»."""
    try:
        doc_type = DocType(str(args.get("doc_type", "")).upper())
    except ValueError:
        return {"error": "نوع سند نامعتبر است."}
    if doc_type not in AI_DOC_TYPES:
        return {"error": "دستیار فقط رسید، حواله، انتقال، خروج امانی و برگشت امانی می‌سازد."}
    lines_in = [ln for ln in list(args.get("lines") or [])[:100] if isinstance(ln, dict)]
    if not lines_in:
        return {"error": "حداقل یک ردیف کالا لازم است."}
    signature = _signature(doc_type.value, str(args.get("person", "")), str(args.get("warehouse", "")),
                           lines_in)
    if signature in ctx.signatures:  # the same request twice in one turn: never create it again (#4)
        return {**ctx.signatures[signature], "duplicate": True,
                "note": "این سند در همین درخواست قبلاً ساخته شده است؛ دوباره ساخته نشد."}
    warehouses = await master.list_warehouses(ctx.db)
    if not warehouses:
        return {"error": "انباری تعریف نشده است."}

    def find_wh(text: str):
        key = normalize(str(text or ""))
        return next((w for w in warehouses if key and (normalize(w.name) == key or w.code == key)), None)

    wh = find_wh(args.get("warehouse")) or warehouses[0]
    dest = find_wh(args.get("dest_warehouse")) if doc_type == DocType.TRANSFER else None
    if doc_type == DocType.TRANSFER and dest is None:
        return {"needs_choice": "dest_warehouse", "candidates": [w.name for w in warehouses],
                "note": "انبار مقصد را از کاربر بپرس."}
    person_id, question = await _resolve_person(ctx, str(args.get("person", "")))
    if question:
        return question
    if doc_type in (DocType.ISSUE, DocType.LOAN_OUT, DocType.LOAN_RETURN) and person_id is None:
        return {"needs_choice": "person", "candidates": [],
                "note": "برای این سند تحویل‌گیرنده لازم است؛ نام شخص را از کاربر بپرس."}
    place: dict = {}
    if doc_type == DocType.ISSUE:
        place, question = await _resolve_place(ctx, args)
        if question:
            return question
        place["usage_note"] = str(args.get("usage_note", "") or "")[:500]
    units = {normalize(u.name): u.id for u in await master.list_units(ctx.db)}
    doc_lines = []
    for line in lines_in:
        item_id, question = await _resolve_item(ctx, line)
        if question:
            return question
        detail = await items.get_item(ctx.db, ctx.actor, item_id)
        allowed = {detail.input.base_unit_id, *(u for u, _f in detail.input.units)}
        unit_id = units.get(normalize(str(line.get("unit", "") or "")))
        try:
            qty = Decimal(to_ascii_digits(str(line.get("qty", "")).strip()))
        except (InvalidOperation, ValueError):
            qty = None
        if qty is None or not qty.is_finite() or qty <= 0:
            return {"needs_choice": "qty", "asked": detail.input.name,
                    "note": f"مقدار «{detail.input.name}» مشخص نیست؛ از کاربر بپرس چند تا (حدس نزن)."}
        doc_lines.append(documents.LineInput(
            item_id, unit_id if unit_id in allowed else detail.input.base_unit_id, qty,
            notes=str(line.get("notes", "") or "")[:200]))
    related = None
    if doc_type == DocType.LOAN_RETURN:
        loans = await documents.outstanding_loans(ctx.db, ctx.actor, person_id=person_id)
        wanted = {ln.item_id for ln in doc_lines}
        matching = sorted({r.document_id for r in loans if r.item_id in wanted})
        if len(matching) != 1:
            return {"error": "امانی باز مشخصی برای این شخص و کالا پیدا نشد؛ از کاربر بپرس."
                    if not matching else "چند امانی باز هست؛ از کاربر بپرس کدام برگشت داده شده."}
        related = matching[0]
    doc_id = await documents.create_document(ctx.db, ctx.actor, documents.DocumentInput(
        doc_type, dt.date.today(), wh.id, doc_lines, dest_warehouse_id=dest.id if dest else None,
        person_id=person_id, related_document_id=related,
        description=str(args.get("description", "") or "ثبت توسط دستیار هوشمند")[:500], **place))
    detail = await documents.get_document(ctx.db, ctx.actor, doc_id)
    summary = await documents.confirmation(ctx.db, ctx.actor, doc_id)
    result = {"document_id": doc_id, "number": documents.number_text(doc_type, detail.number),
              "type": documents.DOC_TYPE_NAMES[doc_type], "status": "DRAFT",
              "lines": [{"code": ln.item_code, "name": ln.item_name, "qty": _num(ln.qty),
                         "unit": ln.unit_name} for ln in detail.lines],
              "needs_confirmation": True, "summary": summary.text,
              "note": "پیش‌نویس ساخته شد و هنوز روی موجودی اثری ندارد. برنامه خلاصه را با دکمه‌های "
                      "«بله، ثبت کن» / «نه» به کاربر نشان می‌دهد؛ خودت ثبت نهایی نکن."}
    ctx.created_documents.append(result)
    ctx.signatures[signature] = result
    return result


async def _create_goods_request(ctx: ToolContext, args: dict) -> dict:
    """A goods request for the owners' approval, as a DRAFT the person sends with «بله، بفرست»."""
    lines_in = [ln for ln in list(args.get("lines") or [])[:100] if isinstance(ln, dict)]
    if not lines_in:
        return {"error": "حداقل یک کالا لازم است."}
    signature = "request\n" + _signature("REQUEST", str(args.get("person", "")),
                                         str(args.get("location", "")), lines_in)
    if signature in ctx.signatures:
        return {**ctx.signatures[signature], "duplicate": True,
                "note": "این درخواست در همین گفتگو قبلاً ساخته شده است؛ دوباره ساخته نشد."}
    person_id, question = await _resolve_person(ctx, str(args.get("person", "")))
    if question:
        return question
    if person_id is None:
        return {"needs_choice": "person", "candidates": [],
                "note": "درخواست‌کننده لازم است؛ نام او را از کاربر بپرس (باید در فهرست اشخاص باشد)."}
    if not str(args.get("location", "") or "").strip():
        return {"needs_choice": "location", "candidates": [],
                "note": "محل مصرف لازم است؛ از کاربر بپرس کجا مصرف می‌شود (شماره پلاک یا نام محل)."}
    place, question = await _resolve_place(ctx, args)
    if question:
        return question
    units = {normalize(u.name): u.id for u in await master.list_units(ctx.db)}
    lines = []
    for line in lines_in:
        item_id, question = await _resolve_item(ctx, line)
        if question:
            return question
        detail = await items.get_item(ctx.db, ctx.actor, item_id)
        allowed = {detail.input.base_unit_id, *(u for u, _f in detail.input.units)}
        unit_id = units.get(normalize(str(line.get("unit", "") or "")))
        try:
            qty = Decimal(to_ascii_digits(str(line.get("qty", "")).strip()))
        except (InvalidOperation, ValueError):
            qty = None
        if qty is None or not qty.is_finite() or qty <= 0:
            return {"needs_choice": "qty", "asked": detail.input.name,
                    "note": f"مقدار «{detail.input.name}» مشخص نیست؛ از کاربر بپرس چند تا (حدس نزن)."}
        unit_id = unit_id if unit_id in allowed else detail.input.base_unit_id
        lines.append(goods.RequestLineInput(item_id, unit_id, qty))
    request_id = await goods.create_request(ctx.db, ctx.actor, goods.RequestInput(
        person_id, place.get("location_id"), place.get("floor_id"), str(args.get("note", "") or "")[:500],
        lines))
    summary = await goods.confirmation(ctx.db, ctx.actor, request_id)
    result = {"request_id": request_id, "status": "DRAFT", "needs_confirmation": True,
              "summary": summary.text,
              "note": "پیش‌نویس درخواست ساخته شد و هنوز برای مالکان ارسال نشده است. برنامه خلاصه را با "
                      "دکمه‌های «بله، بفرست» / «نه» به کاربر نشان می‌دهد؛ تو نه ارسال می‌کنی، نه تأیید، "
                      "نه چاپ."}
    ctx.created_requests.append(result)
    ctx.signatures[signature] = result
    return result


async def _list_goods_requests(ctx: ToolContext, args: dict) -> list[dict]:
    status = str(args.get("status", "") or "").upper()
    rows = await goods.list_requests(ctx.db, ctx.actor, RequestStatus(status) if status in RequestStatus
                                     else None, limit=min(int(args.get("limit", 20)), MAX_ROWS))
    # Which owner decided is never known here (the server hides it), only the status.
    return [{"number": r.number_text, "status": r.status_name, "requester": r.requester,
             "location": r.location, "items": r.items_text, "rejection_reason": r.rejection_reason or None,
             "issue_number": r.issue_number or None, "waiting_to_send": r.waiting} for r in rows]


async def _send_report(ctx: ToolContext, args: dict) -> dict:
    from caspian.services.scheduler import REPORT_NAMES, send_report

    name = str(args.get("report", "stock_balance"))
    if name not in REPORT_NAMES or name == "activity":
        return {"error": "گزارش نامعتبر است (stock_balance، reorder یا loans)."}
    if ctx.messenger is None:
        return {"error": "ارسال پیام در این محیط در دسترس نیست."}
    channels = [c for c in args.get("channels", []) if c in ("telegram", "email")] or None
    # Only admin-configured recipients: the AI can't choose where data goes.
    return {"result": await send_report(ctx.db, ctx.messenger, name, channels)}


async def _propose_tasks(ctx: ToolContext, args: dict) -> dict:
    from caspian.services.scheduler import add_proposals, parse_instructions

    proposals = parse_instructions(str(args.get("instructions", "")))
    if not proposals:
        return {"error": "دستورالعمل قابل‌فهمی (پشتیبان‌گیری یا گزارش با زمان) پیدا نشد."}
    ids = await add_proposals(ctx.db, ctx.actor, proposals)
    return {"proposed_task_ids": ids, "tasks": [{"name": p.name, "cron": p.cron} for p in proposals],
            "note": "کارها پیشنهاد شدند و باید مدیر در تنظیمات ← کارهای زمان‌بندی‌شده تأیید کند."}


TOOLS: list[Tool] = [
    Tool("search_items", "جستجوی کالا با نام، کد یا بارکد. موجودی و نقطه سفارش را برمی‌گرداند.",
         _obj({"query": {"type": "string"}, "limit": {"type": "integer"}}, ("query",)),
         _search_items, Perm.ITEMS_VIEW),
    Tool("item_stock", "موجودی یک کالا به تفکیک انبار (با کد کالا).",
         _obj({"code": {"type": "string"}}, ("code",)), _item_stock, Perm.STOCK_VIEW),
    Tool("reorder_analysis",
         "تحلیل مصرف (Burn rate): مصرف روزانه، روزهای پوشش، نقطه سفارش و مقدار سفارش پیشنهادی. "
         "بدون code فهرست کالاهای نیازمند سفارش را می‌دهد.",
         _obj({"code": {"type": "string"}, "lookback_days": {"type": "integer"},
               "lead_time_days": {"type": "integer"}, "cover_days": {"type": "integer"},
               "safety_days": {"type": "integer"}, "only_needing_order": {"type": "boolean"}}),
         _reorder_analysis, Perm.REPORTS_VIEW),
    Tool("recent_documents", "آخرین اسناد انبار. doc_type یکی از RECEIPT, ISSUE, TRANSFER, "
         "ADJUSTMENT, LOAN_OUT, LOAN_RETURN, OPENING.",
         _obj({"doc_type": {"type": "string"}, "limit": {"type": "integer"}}),
         _recent_documents, Perm.DOCUMENTS_VIEW),
    Tool("find_location", "جستجوی محل مصرف (پلاک، نام محل یا اسمی که کارکنان می‌گویند) و فهرست طبقه‌ها.",
         _obj({"query": {"type": "string"}}, ("query",)), _find_location, Perm.DOCUMENTS_VIEW),
    Tool("consumption_report",
         "مصرف کالا (حواله‌های خروج ثبت‌شده) به تفکیک طبقه یا محل مصرف. group_by: floor یا location. "
         "period: this_month (پیش‌فرض)، last_30_days، this_year، all؛ یا date_from/date_to شمسی. "
         "floor (اختیاری) = فقط یک طبقه مثل «همکف». item و person اختیاری.",
         _obj({"group_by": {"type": "string", "enum": ["floor", "location"]},
               "period": {"type": "string", "enum": ["this_month", "last_30_days", "this_year", "all"]},
               "date_from": {"type": "string"}, "date_to": {"type": "string"},
               "floor": {"type": "string"}, "item": {"type": "string"}, "person": {"type": "string"}}),
         _consumption_report, Perm.REPORTS_VIEW),
    Tool("open_loans", "کالاهای امانی که هنوز برنگشته‌اند.", _obj({}), _open_loans,
         Perm.DOCUMENTS_VIEW),
    Tool("suppliers", "فهرست تأمین‌کنندگان (برای نوشتن پیام درخواست خرید).",
         _obj({"query": {"type": "string"}}), _suppliers, Perm.ITEMS_VIEW),
    Tool("create_document",
         "ساخت سند انبار مثل کاربر: رسید (RECEIPT)، حواله خروج (ISSUE)، انتقال (TRANSFER)، خروج امانی "
         "(LOAN_OUT) یا برگشت امانی (LOAN_RETURN). person = نام تحویل‌گیرنده/تأمین‌کننده (برای حواله و "
         "امانی الزامی). هر ردیف: code یا name یا barcode، qty، unit اختیاری. سند همیشه پیش‌نویس "
         "ساخته می‌شود و کاربر با دکمه تأیید آن را ثبت می‌کند. برای حواله خروج: location = محل مصرف "
         "(شماره پلاک یا نام محل)، floor = طبقه "
         "(فقط برای پله، آسانسور و مشاعات)، usage_note = شرح مصرف. اگر پاسخ needs_choice داشت، از "
         "کاربر بپرس و دوباره بساز.",
         _obj({"doc_type": {"type": "string",
                            "enum": ["RECEIPT", "ISSUE", "TRANSFER", "LOAN_OUT", "LOAN_RETURN"]},
               "person": {"type": "string"}, "warehouse": {"type": "string"},
               "dest_warehouse": {"type": "string"}, "description": {"type": "string"},
               "location": {"type": "string"}, "floor": {"type": "string"},
               "usage_note": {"type": "string"},
               "lines": {"type": "array", "items": _obj({
                   "name": {"type": "string"}, "code": {"type": "string"},
                   "barcode": {"type": "string"}, "qty": {"type": "number"},
                   "unit": {"type": "string"}, "notes": {"type": "string"}})}},
              ("doc_type", "lines")),
         _create_document, Perm.DOCUMENTS_EDIT),
    Tool("create_stock_draft",
         "ساخت پیش‌نویس «ورود اطلاعات» از فهرست طولانی کالا (برای بررسی کاربر). برای یک سند معمولی "
         "از create_document استفاده کن. هر ردیف: name یا code یا barcode، qty و unit.",
         _obj({"doc_type": {"type": "string", "enum": ["RECEIPT", "ISSUE"]},
               "person": {"type": "string"},
               "warehouse": {"type": "string"}, "title": {"type": "string"},
               "lines": {"type": "array", "items": _obj({
                   "name": {"type": "string"}, "code": {"type": "string"},
                   "barcode": {"type": "string"}, "qty": {"type": "number"},
                   "unit": {"type": "string"}})}},
             ("doc_type", "lines")),
         _create_stock_draft, Perm.IMPORT_RUN),
    Tool("create_goods_request",
         "ساخت «درخواست کالا» برای تأیید مالکان (از طریق سرور کاسپین). person = درخواست‌کننده (از فهرست "
         "اشخاص)، location = محل مصرف (شماره پلاک یا نام محل)، floor = طبقه فقط برای پله/آسانسور/مشاعات، "
         "note = توضیح. هر ردیف: code یا name، qty، unit اختیاری. فقط پیش‌نویس ساخته می‌شود و کاربر با "
         "دکمه «بله، بفرست» آن را ارسال می‌کند. اگر needs_choice برگشت، از کاربر بپرس و دوباره بساز.",
         _obj({"person": {"type": "string"}, "location": {"type": "string"}, "floor": {"type": "string"},
               "note": {"type": "string"},
               "lines": {"type": "array", "items": _obj({
                   "name": {"type": "string"}, "code": {"type": "string"}, "qty": {"type": "number"},
                   "unit": {"type": "string"}})}},
              ("person", "location", "lines")),
         _create_goods_request, Perm.REQUESTS_MANAGE),
    Tool("list_goods_requests",
         "فهرست درخواست‌های کالا و وضعیت آن‌ها (در صف ارسال، در انتظار تأیید مالک، تأیید شد، رد شد، حواله "
         "صادر شد). status اختیاری: QUEUED، PENDING، APPROVED، REJECTED، ISSUED.",
         _obj({"status": {"type": "string"}, "limit": {"type": "integer"}}),
         _list_goods_requests, Perm.REQUESTS_MANAGE),
    Tool("send_report", "ساخت گزارش اکسل و ارسال به گیرندگان از پیش تنظیم‌شده (تلگرام/ایمیل). "
         "report یکی از stock_balance, reorder, loans.",
         _obj({"report": {"type": "string"}, "channels": {"type": "array", "items": {"type": "string"}}},
              ("report",)), _send_report, Perm.REPORTS_VIEW),
    Tool("propose_tasks", "پیشنهاد کار زمان‌بندی‌شده از دستورالعمل متنی، مثل «هر روز ساعت ۸ شب پشتیبان بگیر». "
         "فقط پیشنهاد است و مدیر باید تأیید کند.",
         _obj({"instructions": {"type": "string"}}, ("instructions",)), _propose_tasks, Perm.AI_USE),
]
_BY_NAME = {t.name: t for t in TOOLS}


AI_DOC_TYPES = (DocType.RECEIPT, DocType.ISSUE, DocType.TRANSFER, DocType.LOAN_OUT, DocType.LOAN_RETURN)


def available_tools(actor: Actor) -> list[Tool]:
    return [t for t in TOOLS if t.permission is None or actor.can(t.permission)]


async def run_tool(ctx: ToolContext, name: str, arguments: str | dict) -> str:
    """Execute a tool call and return a JSON string for the model. Never raises."""
    tool = _BY_NAME.get(name)
    if tool is None or (tool.permission and not ctx.actor.can(tool.permission)):
        return json.dumps({"error": f"ابزار «{name}» در دسترس نیست."}, ensure_ascii=False)
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and arguments else (arguments or {})
        result = await tool.handler(ctx, args)
    except ServiceError as exc:
        result = {"error": exc.message}
    except (ValueError, KeyError, TypeError) as exc:
        result = {"error": f"ورودی نامعتبر: {exc}"}
    except Exception:
        log.exception("Tool %s failed", name)
        result = {"error": "خطای داخلی در اجرای ابزار"}
    return json.dumps(result, ensure_ascii=False, default=str)
