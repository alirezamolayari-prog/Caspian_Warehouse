"""Tools the AI assistant may call.

Every tool runs as `actor.as_ai()`: the signed-in user's permissions, flagged as AI so
protected actions and applying drafts are refused by the services themselves. There are
read-only tools plus exactly one write tool, `create_stock_draft`, which only creates an
import *draft* for a human to review. There is deliberately no tool to post, edit,
delete, merge, restore or change anything.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from caspian.core import jalali
from caspian.core.permissions import Perm
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
    warehouses = await master.list_warehouses(ctx.db)
    wh = next((w for w in warehouses if w.name == args.get("warehouse")), warehouses[0])
    rows = []
    for line in list(args.get("lines", []))[:200]:
        qty = line.get("qty")
        rows.append(RawRow(
            code=str(line.get("code", "") or ""), name=str(line.get("name", "") or ""),
            barcode=str(line.get("barcode", "") or ""),
            qty=Decimal(str(qty)) if qty not in (None, "") else None,
            unit_name=str(line.get("unit", "") or ""),
            raw={k: str(v) for k, v in line.items()}))
    batch_id = await imports.create_batch(
        ctx.db, ctx.actor, ImportKind.STOCK, ImportSource.TEXT, rows,
        str(args.get("title", "پیش‌نویس دستیار"))[:200], doc_type, wh.id)
    ctx.created_batches.append(batch_id)
    detail = await imports.get_batch(ctx.db, ctx.actor, batch_id)
    return {"draft_id": batch_id, "warehouse": wh.name,
            "status_counts": {k.value: v for k, v in detail.row.counts.items() if v},
            "needs_review": detail.row.unresolved,
            "note": "پیش‌نویس ساخته شد و باید توسط کاربر در «ورود اطلاعات» بررسی و اعمال شود."}


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
    Tool("create_stock_draft",
         "ساخت پیش‌نویس ورود/خروج کالا از متن کاربر. هر ردیف: name یا code یا barcode، qty و در صورت "
         "ذکر unit. این فقط پیش‌نویس است و کاربر باید آن را بررسی و اعمال کند.",
         _obj({"doc_type": {"type": "string", "enum": ["RECEIPT", "ISSUE"]},
               "warehouse": {"type": "string"}, "title": {"type": "string"},
               "lines": {"type": "array", "items": _obj({
                   "name": {"type": "string"}, "code": {"type": "string"},
                   "barcode": {"type": "string"}, "qty": {"type": "number"},
                   "unit": {"type": "string"}})}},
             ("doc_type", "lines")),
         _create_stock_draft, Perm.IMPORT_RUN),
]
_BY_NAME = {t.name: t for t in TOOLS}


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
