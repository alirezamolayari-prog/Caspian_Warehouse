"""MCP server exposing warehouse data to external AI clients (read-only by default).

    caspian-mcp                      stdio: for a client on this PC (Claude Desktop, Claude Code…)
    caspian-mcp --http [--port N]    Streamable HTTP on 127.0.0.1, for agents that connect over HTTP
    caspian-mcp --http --lan         ... reachable from the LAN (bind 0.0.0.0)

Example stdio client configuration:

    {"mcpServers": {"caspian-warehouse": {"command": "caspian-mcp"}}}

The HTTP mode always requires `Authorization: Bearer <token>`; the token is generated in the
app (Settings → AI → MCP) and kept in this PC's Windows Credential Manager.

Read tools connect with the read-only database user when one has been created (Settings → AI),
otherwise with the app user forced into READ ONLY mode. Only when an administrator enables it,
two draft-only tools are added (`create_draft_document`); posting is never possible from MCP.
Every call is appended to Logs/mcp-audit.log on the PC running the server.
"""

import argparse
import asyncio
import datetime as dt
import functools
import hmac
import json
import logging
import re
import sys
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from logging.handlers import RotatingFileHandler
from typing import Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy import text

from caspian.core.permissions import Perm
from caspian.core.secrets import get_secret
from caspian.core.settings import Settings, log_dir
from caspian.db.database import Database, DbConfig
from caspian.db.models import DocType
from caspian.db.readonly import open_read_only
from caspian.services import documents, items, master, mcp_settings
from caspian.services.actor import Actor
from caspian.services.ai.tools import ToolContext, run_tool
from caspian.services.errors import ServiceError
from caspian.services.scheduler import REPORT_NAMES, build_report

log = logging.getLogger(__name__)

MCP_ACTOR = Actor(None, "mcp", "MCP", "mcp", frozenset(p.value for p in (
    Perm.ITEMS_VIEW, Perm.STOCK_VIEW, Perm.DOCUMENTS_VIEW, Perm.REPORTS_VIEW)), is_ai=True)
# Drafts only: no documents.post, and is_ai makes every protected action refuse it.
MCP_DRAFT_ACTOR = Actor(None, "mcp", "MCP", "mcp", frozenset(p.value for p in (
    Perm.ITEMS_VIEW, Perm.DOCUMENTS_VIEW, Perm.DOCUMENTS_EDIT)), is_ai=True)
SQL_ROW_LIMIT = 200
DEFAULT_HTTP_PORT = 8765
DRAFT_TYPES = {"RECEIPT": DocType.RECEIPT, "ISSUE": DocType.ISSUE}
_FORBIDDEN_SQL = re.compile(r"\b(into\s+outfile|into\s+dumpfile|load_file|sleep|benchmark|"
                            r"lock|for\s+update|information_schema|mysql\.)\b", re.IGNORECASE)

# Who is calling, for the audit log (set per HTTP request by the bearer-token middleware).
_client: ContextVar[str] = ContextVar("mcp_client", default="stdio")
_audit_log: logging.Logger | None = None


def audit_logger() -> logging.Logger:
    """Logs/mcp-audit.log on this PC: one line per tool call or rejected request."""
    global _audit_log
    if _audit_log is None:
        logger = logging.getLogger("caspian.mcp.audit")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        log_dir().mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir() / "mcp-audit.log", maxBytes=2_000_000, backupCount=5,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        _audit_log = logger
    return _audit_log


def _audit(event: str, **fields) -> None:
    audit_logger().info("%s %s", event, json.dumps(fields, ensure_ascii=False, default=str)[:600])


def _json(value) -> str:
    def default(v):
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, dt.date | dt.datetime):
            return v.isoformat()
        return str(v)

    return json.dumps(value, ensure_ascii=False, default=default)


def validate_select(sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", sql, re.IGNORECASE):
        raise ValueError("Only SELECT queries are allowed.")
    if ";" in sql:
        raise ValueError("Only a single statement is allowed.")
    if _FORBIDDEN_SQL.search(sql):
        raise ValueError("This construct is not allowed.")
    return sql


def _audited(fn):
    """Record every call (tool, arguments, caller, outcome) before returning its result."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            _audit("call", tool=fn.__name__, client=_client.get(), ok=False, args=kwargs,
                   error=str(exc)[:200])
            raise
        failed = isinstance(result, str) and result.startswith('{"error"')
        _audit("call", tool=fn.__name__, client=_client.get(), ok=not failed, args=kwargs)
        return result

    return wrapper


def build_server(db: Database, allow_sql: bool, write_db: Database | None = None) -> MCPServer:
    """`write_db`: only when an admin enabled draft creation; it adds the draft-only tools."""
    server = MCPServer(
        name="caspian-warehouse",
        instructions="Caspian Warehouse inventory (Persian data). Read-only, except that draft "
                     "documents may be created when the administrator allowed it; drafts are "
                     "always finalized by a person in the app. Quantities are in each item's base "
                     "unit.")
    ctx = ToolContext(db, MCP_ACTOR, [])

    def tool(description: str):
        return lambda fn: server.tool(description=description)(_audited(fn))

    @tool("Search items by name, code or barcode (Persian text welcome).")
    async def search_items(query: str, limit: int = 20) -> str:
        return await run_tool(ctx, "search_items", {"query": query, "limit": limit})

    @tool("Stock of one item per warehouse, by item code.")
    async def item_stock(code: str) -> str:
        return await run_tool(ctx, "item_stock", {"code": code})

    @tool("Burn-rate analysis and reorder suggestions. Omit code for all items needing an order.")
    async def reorder_analysis(code: str = "", lookback_days: int = 90, lead_time_days: int = 14,
                               cover_days: int = 60) -> str:
        args = {"lookback_days": lookback_days, "lead_time_days": lead_time_days,
                "cover_days": cover_days, "only_needing_order": not code}
        if code:
            args["code"] = code
        return await run_tool(ctx, "reorder_analysis", args)

    @tool("Recent warehouse documents; doc_type one of RECEIPT, ISSUE, TRANSFER, "
          "ADJUSTMENT, LOAN_OUT, LOAN_RETURN, OPENING.")
    async def recent_documents(doc_type: str = "", limit: int = 20) -> str:
        return await run_tool(ctx, "recent_documents", {"doc_type": doc_type or None, "limit": limit})

    @tool("Returnable items that are still out on loan.")
    async def open_loans() -> str:
        return await run_tool(ctx, "open_loans", {})

    @tool(f"A full report as JSON rows. name: {', '.join(REPORT_NAMES)}.")
    async def report(name: str) -> str:
        if name not in REPORT_NAMES or name == "activity":  # activity needs admin rights
            return _json({"error": "Unknown report. Use one of: stock_balance, reorder, loans"})
        table = await build_report(db, name)
        return _json({"title": table.title, "meta": table.meta,
                      "columns": [c.title for c in table.columns], "rows": table.rows})

    if allow_sql:
        @tool(f"Run one read-only SELECT query (max {SQL_ROW_LIMIT} rows). "
              "The users table is not accessible.")
        async def query_sql(sql: str) -> str:
            try:
                sql = validate_select(sql)
            except ValueError as exc:
                return _json({"error": str(exc)})
            async with db.engine.connect() as conn:
                try:
                    result = await conn.execute(
                        text(f"SELECT * FROM ({sql}) AS q LIMIT {SQL_ROW_LIMIT}"))
                except Exception as exc:  # report DB errors (e.g. no privilege) to the client
                    return _json({"error": str(getattr(exc, "orig", exc))[:300]})
                return _json({"columns": list(result.keys()),
                              "rows": [list(r) for r in result.fetchall()]})

    if write_db is not None:
        @tool("Create a DRAFT warehouse document (never posted: a person reviews and posts it in the "
              "app). doc_type: RECEIPT or ISSUE. lines: [{\"code\": item code, \"qty\": number, "
              "\"unit\": optional unit name}]. warehouse_code: e.g. \"01\". person: supplier / "
              "recipient name (required for ISSUE).")
        async def create_draft_document(doc_type: str, warehouse_code: str, lines: list[dict[str, Any]],
                                        person: str = "", description: str = "") -> str:
            try:
                return _json(await _create_draft(write_db, doc_type, warehouse_code, lines, person,
                                                 description))
            except ServiceError as exc:
                return _json({"error": exc.message})

    return server


async def _create_draft(db: Database, doc_type: str, warehouse_code: str, lines: list[dict],
                        person: str, description: str) -> dict:
    kind = DRAFT_TYPES.get(str(doc_type).upper())
    if kind is None:
        raise ServiceError("doc_type must be RECEIPT or ISSUE.")
    warehouse = next((w for w in await master.list_warehouses(db) if w.code == warehouse_code.strip()), None)
    if warehouse is None:
        raise ServiceError(f"Unknown warehouse code «{warehouse_code}».")
    person_id = None
    if person.strip():
        matches = await master.search_persons(db, person.strip())
        if not matches:
            raise ServiceError(f"Unknown person «{person}».")
        person_id = matches[0].id
    units = {u.name: u.id for u in await master.list_units(db)}
    doc_lines = []
    for n, line in enumerate(lines or [], start=1):
        code = str(line.get("code", "")).strip()
        rows = [r for r in await items.search_items(db, MCP_DRAFT_ACTOR, code, limit=5) if r.code == code]
        if not rows:
            raise ServiceError(f"Line {n}: unknown item code «{code}».")
        detail = await items.get_item(db, MCP_DRAFT_ACTOR, rows[0].id)
        unit_id = units.get(str(line.get("unit", "")).strip()) or detail.input.base_unit_id
        try:
            qty = Decimal(str(line.get("qty")))
        except (InvalidOperation, ValueError):
            raise ServiceError(f"Line {n}: qty must be a number.") from None
        doc_lines.append(documents.LineInput(rows[0].id, unit_id, qty, notes="MCP"))
    doc_id = await documents.create_document(db, MCP_DRAFT_ACTOR, documents.DocumentInput(
        kind, dt.date.today(), warehouse.id, doc_lines, person_id=person_id,
        description=f"ساخته‌شده از MCP — {description}".strip(" —")[:500]))
    detail = await documents.get_document(db, MCP_DRAFT_ACTOR, doc_id)
    return {"document_id": doc_id, "number": documents.number_text(kind, detail.number),
            "status": "DRAFT", "note": "Draft only: a person must review and post it in the app."}


# ----- HTTP -----


class BearerTokenMiddleware:
    """Rejects any HTTP request without `Authorization: Bearer <token>` (constant-time compare)."""

    def __init__(self, app, token: str) -> None:
        if not token:
            raise ValueError("An MCP HTTP token is required.")
        self.app, self._token = app, token.encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        given = headers.get(b"authorization", b"")
        client = f"http {(scope.get('client') or ('?',))[0]}"
        if not (given[:7].lower() == b"bearer " and hmac.compare_digest(given[7:].strip(), self._token)):
            _audit("rejected", client=client, path=scope.get("path"))
            body = b'{"error": "missing or invalid bearer token"}'
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"application/json"), (b"www-authenticate", b"Bearer"),
                (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        reset = _client.set(client)
        try:
            await self.app(scope, receive, send)
        finally:
            _client.reset(reset)


def bind_host(lan: bool) -> str:
    return "0.0.0.0" if lan else "127.0.0.1"


def http_app(server: MCPServer, token: str, lan: bool = False):
    """Streamable HTTP app at /mcp, token-protected. Stateless: each request is independent,
    which also keeps the caller known to the audit log for the whole call."""
    app = server.streamable_http_app(stateless_http=True, json_response=True, host=bind_host(lan))
    return BearerTokenMiddleware(app, token)  # lifespan events pass through to the app


# ----- entry point -----


async def _drafts_enabled(db: Database) -> bool:
    try:
        return await mcp_settings.allow_drafts(db)
    finally:
        await db.dispose()  # the server runs its own event loop later


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout is the MCP channel
    parser = argparse.ArgumentParser(prog="caspian-mcp", description="Caspian Warehouse MCP server")
    parser.add_argument("--http", action="store_true", help="Streamable HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--lan", action="store_true", help="listen on all interfaces (LAN)")
    args = parser.parse_args(argv)
    settings = Settings.load()
    config = DbConfig.from_dict(settings.database)
    if config is None:
        sys.exit("Database is not configured. Start the Caspian Warehouse app once first.")
    app_password = get_secret("db", config.secret_name) or ""
    db, dedicated = open_read_only(config, app_password)
    write_db = Database(config.url(app_password)) if asyncio.run(_drafts_enabled(db)) else None
    server = build_server(db, allow_sql=dedicated, write_db=write_db)
    if not args.http:
        server.run("stdio")
        return
    token = mcp_settings.http_token()
    if not token:
        sys.exit("No MCP HTTP token on this PC. Create one in the app: Settings → AI → MCP.")
    import uvicorn

    _audit("started", transport="http", host=bind_host(args.lan), port=args.port,
           drafts=write_db is not None)
    uvicorn.run(http_app(server, token, args.lan), host=bind_host(args.lan), port=args.port,
                log_level="warning")


if __name__ == "__main__":
    main()
