"""Read-only MCP server exposing warehouse data to external AI clients.

Run with `caspian-mcp` (stdio transport). Example client configuration:

    {"mcpServers": {"caspian-warehouse": {"command": "caspian-mcp"}}}

It connects with the read-only database user when one has been created (Settings → AI),
otherwise with the app user forced into READ ONLY mode. There are no write tools.
"""

import datetime as dt
import json
import logging
import re
import sys
from decimal import Decimal

from mcp.server.mcpserver import MCPServer
from sqlalchemy import text

from caspian.core.permissions import Perm
from caspian.core.secrets import get_secret
from caspian.core.settings import Settings
from caspian.db.database import Database, DbConfig
from caspian.db.readonly import open_read_only
from caspian.services.actor import Actor
from caspian.services.ai.tools import ToolContext, run_tool
from caspian.services.scheduler import REPORT_NAMES, build_report

log = logging.getLogger(__name__)

MCP_ACTOR = Actor(None, "mcp", "MCP", "mcp", frozenset(p.value for p in (
    Perm.ITEMS_VIEW, Perm.STOCK_VIEW, Perm.DOCUMENTS_VIEW, Perm.REPORTS_VIEW)), is_ai=True)
SQL_ROW_LIMIT = 200
_FORBIDDEN_SQL = re.compile(r"\b(into\s+outfile|into\s+dumpfile|load_file|sleep|benchmark|"
                            r"lock|for\s+update|information_schema|mysql\.)\b", re.IGNORECASE)


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


def build_server(db: Database, allow_sql: bool) -> MCPServer:
    server = MCPServer(
        name="caspian-warehouse",
        instructions="Read-only access to the Caspian Warehouse inventory (Persian data). "
                     "Quantities are in each item's base unit.")
    ctx = ToolContext(db, MCP_ACTOR, [])

    @server.tool(description="Search items by name, code or barcode (Persian text welcome).")
    async def search_items(query: str, limit: int = 20) -> str:
        return await run_tool(ctx, "search_items", {"query": query, "limit": limit})

    @server.tool(description="Stock of one item per warehouse, by item code.")
    async def item_stock(code: str) -> str:
        return await run_tool(ctx, "item_stock", {"code": code})

    @server.tool(description="Burn-rate analysis and reorder suggestions. Omit code for all items "
                             "needing an order.")
    async def reorder_analysis(code: str = "", lookback_days: int = 90, lead_time_days: int = 14,
                               cover_days: int = 60) -> str:
        args = {"lookback_days": lookback_days, "lead_time_days": lead_time_days,
                "cover_days": cover_days, "only_needing_order": not code}
        if code:
            args["code"] = code
        return await run_tool(ctx, "reorder_analysis", args)

    @server.tool(description="Recent warehouse documents; doc_type one of RECEIPT, ISSUE, TRANSFER, "
                             "ADJUSTMENT, LOAN_OUT, LOAN_RETURN, OPENING.")
    async def recent_documents(doc_type: str = "", limit: int = 20) -> str:
        return await run_tool(ctx, "recent_documents",
                              {"doc_type": doc_type or None, "limit": limit})

    @server.tool(description="Returnable items that are still out on loan.")
    async def open_loans() -> str:
        return await run_tool(ctx, "open_loans", {})

    @server.tool(description=f"A full report as JSON rows. name: {', '.join(REPORT_NAMES)}.")
    async def report(name: str) -> str:
        if name not in REPORT_NAMES or name == "activity":  # activity needs admin rights
            return _json({"error": "Unknown report. Use one of: stock_balance, reorder, loans"})
        table = await build_report(db, name)
        return _json({"title": table.title, "meta": table.meta,
                      "columns": [c.title for c in table.columns], "rows": table.rows})

    if allow_sql:
        @server.tool(description=f"Run one read-only SELECT query (max {SQL_ROW_LIMIT} rows). "
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

    return server


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout is the MCP channel
    settings = Settings.load()
    config = DbConfig.from_dict(settings.database)
    if config is None:
        sys.exit("Database is not configured. Start the Caspian Warehouse app once first.")
    db, dedicated = open_read_only(config, get_secret("db", config.secret_name) or "")
    build_server(db, allow_sql=dedicated).run("stdio")


if __name__ == "__main__":
    main()
