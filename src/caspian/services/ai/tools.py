"""Tools the AI assistant may call.

Every tool runs as `actor.as_ai()`: the signed-in user's permissions, flagged as AI so
protected actions and applying drafts are refused by the services themselves. Besides the
read-only tools the assistant can work like a user: create warehouse documents
(`create_document`) and, when the admin allows it, post them (`post_document`) — through the
same services, so stock, dates, units, stocktake freeze and permissions all apply. There is
deliberately no tool to cancel, delete, deactivate, merge, restore or change roles/settings,
and the services refuse those for AI actors anyway (`Actor.require_human`).
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
from caspian.core.text import normalize, to_ascii_digits
from caspian.db.database import Database
from caspian.db.models import DocType, ImportKind, ImportSource
from caspian.services import documents, imports, items, master, reports
from caspian.services.actor import Actor
from caspian.services.errors import ServiceError
from caspian.services.import_files import RawRow

log = logging.getLogger(__name__)

MAX_ROWS = 50


@dataclass
class ToolContext:
    db: Database
    actor: Actor  # already restricted with as_ai()
    created_batches: list[int]
    messenger: object | None = None  # caspian.services.messaging.Messenger
    allow_post: bool = False  # «اجازه ثبت نهایی سند توسط دستیار»
    created_documents: list[dict] = field(default_factory=list)
    signatures: dict[str, dict] = field(default_factory=dict)  # one create per request (#4)


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
    if found and len(exact) == 0:
        best = found[0][1]
        second = found[1][1] if len(found) > 1 else 0
        if best >= imports.AUTO_MATCH and best - second > imports.AMBIGUOUS_GAP + 4:
            return found[0][0], None
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
            qty = Decimal(str(line.get("qty")))
        except (InvalidOperation, ValueError):
            return {"error": f"مقدار «{detail.input.name}» عدد نیست."}
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
        description=str(args.get("description", "") or "ثبت توسط دستیار هوشمند")[:500]))
    detail = await documents.get_document(ctx.db, ctx.actor, doc_id)
    result = {"document_id": doc_id, "number": documents.number_text(doc_type, detail.number),
              "type": documents.DOC_TYPE_NAMES[doc_type], "status": "DRAFT",
              "lines": [{"code": ln.item_code, "name": ln.item_name, "qty": _num(ln.qty),
                         "unit": ln.unit_name} for ln in detail.lines]}
    ctx.created_documents.append(result)
    ctx.signatures[signature] = result
    if args.get("post"):
        result.update(await _post(ctx, doc_id))
    return result


async def _post(ctx: ToolContext, doc_id: int) -> dict:
    if not ctx.allow_post:
        return {"posted": False, "note": "ثبت نهایی توسط دستیار در تنظیمات غیرفعال است؛ پیش‌نویس ماند."}
    try:
        await documents.post_document(ctx.db, ctx.actor, doc_id)
    except ServiceError as exc:
        return {"posted": False, "post_error": exc.message, "note": "سند به‌صورت پیش‌نویس ماند."}
    for created in ctx.created_documents:
        if created["document_id"] == doc_id:
            created["status"] = "POSTED"
    return {"posted": True, "status": "POSTED"}


async def _post_document(ctx: ToolContext, args: dict) -> dict:
    ref = str(args.get("document", "") or args.get("document_id", "")).strip()
    doc_id = int(ref) if ref.isdigit() else None
    if doc_id is None:
        rows = await documents.list_documents(ctx.db, ctx.actor, query=ref, limit=5)
        drafts = [r for r in rows if r.number_text == ref or str(r.number) in ref]
        if len(drafts) != 1:
            return {"error": "سند پیش‌نویس با این شماره پیدا نشد."}
        doc_id = drafts[0].id
    result = await _post(ctx, doc_id)
    detail = await documents.get_document(ctx.db, ctx.actor, doc_id)
    already = any(c["document_id"] == doc_id for c in ctx.created_documents)
    if not already and result.get("posted"):
        ctx.created_documents.append({"document_id": doc_id, "status": "POSTED",
                                      "number": documents.number_text(detail.input.doc_type, detail.number),
                                      "type": documents.DOC_TYPE_NAMES[detail.input.doc_type]})
    return {"document_id": doc_id, **result}


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
    Tool("open_loans", "کالاهای امانی که هنوز برنگشته‌اند.", _obj({}), _open_loans,
         Perm.DOCUMENTS_VIEW),
    Tool("suppliers", "فهرست تأمین‌کنندگان (برای نوشتن پیام درخواست خرید).",
         _obj({"query": {"type": "string"}}), _suppliers, Perm.ITEMS_VIEW),
    Tool("create_document",
         "ساخت سند انبار مثل کاربر: رسید (RECEIPT)، حواله خروج (ISSUE)، انتقال (TRANSFER)، خروج امانی "
         "(LOAN_OUT) یا برگشت امانی (LOAN_RETURN). person = نام تحویل‌گیرنده/تأمین‌کننده (برای حواله و "
         "امانی الزامی). هر ردیف: code یا name یا barcode، qty، unit اختیاری. post=true یعنی بلافاصله "
         "ثبت نهایی شود. اگر پاسخ needs_choice داشت، از کاربر بپرس و دوباره بساز.",
         _obj({"doc_type": {"type": "string",
                            "enum": ["RECEIPT", "ISSUE", "TRANSFER", "LOAN_OUT", "LOAN_RETURN"]},
               "person": {"type": "string"}, "warehouse": {"type": "string"},
               "dest_warehouse": {"type": "string"}, "description": {"type": "string"},
               "post": {"type": "boolean"},
               "lines": {"type": "array", "items": _obj({
                   "name": {"type": "string"}, "code": {"type": "string"},
                   "barcode": {"type": "string"}, "qty": {"type": "number"},
                   "unit": {"type": "string"}, "notes": {"type": "string"}})}},
              ("doc_type", "lines")),
         _create_document, Perm.DOCUMENTS_EDIT),
    Tool("post_document", "ثبت نهایی یک سند پیش‌نویس با شناسه (document_id) یا شماره (مثل ح-۱۲). "
         "همه کنترل‌های موجودی و تاریخ اعمال می‌شود.",
         _obj({"document": {"type": "string"}}, ("document",)), _post_document, Perm.DOCUMENTS_POST),
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


def available_tools(actor: Actor, allow_post: bool = False) -> list[Tool]:
    return [t for t in TOOLS if (t.permission is None or actor.can(t.permission))
            and (allow_post or t.name != "post_document")]


async def run_tool(ctx: ToolContext, name: str, arguments: str | dict) -> str:
    """Execute a tool call and return a JSON string for the model. Never raises."""
    tool = _BY_NAME.get(name)
    if tool is None or (tool.permission and not ctx.actor.can(tool.permission)) or (
            name == "post_document" and not ctx.allow_post):
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
