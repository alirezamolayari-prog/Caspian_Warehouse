"""End to end against a real Caspian Server (opt-in; run it on a COPY of the server's database).

    $env:CASPIAN_TEST_SERVER_URL = "http://127.0.0.1:8010"
    uv run pytest tests/test_server_e2e.py

Optional: CASPIAN_TEST_SERVER_WAREHOUSE / CASPIAN_TEST_SERVER_OWNER as "user:password"
(defaults: the server's seeded test users). The warehouse side uses the usual temporary SQLite DB.
"""

import os

import httpx
import pytest
from sqlalchemy import select

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocStatus, Document, GoodsRequest, RequestStatus
from caspian.services import documents
from caspian.services.server_link import connection, goods
from caspian.services.server_link.sync import SyncEngine, SyncState
from caspian.ui import document_print, messages
from caspian.ui.app_context import AppContext
from request_helpers import request_input, stock

URL = os.environ.get("CASPIAN_TEST_SERVER_URL")
WAREHOUSE = os.environ.get("CASPIAN_TEST_SERVER_WAREHOUSE", "warehouse:warehouse123").split(":", 1)
OWNER = os.environ.get("CASPIAN_TEST_SERVER_OWNER", "owner1:owner123").split(":", 1)

pytestmark = pytest.mark.skipif(not URL, reason="CASPIAN_TEST_SERVER_URL not set")


async def _owner(method: str, path: str, **kwargs):
    async with httpx.AsyncClient(base_url=URL, timeout=10) as http:
        token = (await http.post("/auth/login", data={"username": OWNER[0], "password": OWNER[1]})).json()
        response = await http.request(method, path, headers={
            "Authorization": f"Bearer {token['access_token']}"}, **kwargs)
        response.raise_for_status()
        return response.json()


async def test_request_to_printed_voucher(qtbot, themes, request_shop, admin, tmp_path, monkeypatch):
    db = request_shop["db"]
    await connection.save_connection(db, admin, URL, WAREHOUSE[0], WAREHOUSE[1])
    engine = SyncEngine(db)

    # 1. The manager registers a request; the sync sends it.
    request_id = await goods.create_request(db, admin, request_input(request_shop, qty=3))
    result = await engine.tick(admin)
    assert result.state == SyncState.CONNECTED, result.message
    async with db.session() as s:
        server_id = (await s.get(GoodsRequest, request_id)).server_id
    assert server_id is not None
    on_server = await _owner("GET", f"/requests/{server_id}")
    assert on_server["status"] == "pending" and on_server["items"][0]["item_code"] == "1024"
    assert on_server["requester_phone"] == "0912 345 6789"
    assert "decided_by_name" not in on_server

    # 2. An owner approves it on the phone; the next tick brings the decision and makes the draft.
    await _owner("POST", f"/requests/{server_id}/approve")
    result = await engine.tick(admin)
    assert [e.kind for e in result.events if e.request_id == request_id] == ["approved"]
    async with db.session() as s:
        doc_id = await s.scalar(select(Document.id).where(Document.server_request_id == server_id))
    assert doc_id is not None
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.DRAFT

    # 3. The manager prints it to PDF with the real print code: confirm → post → PDF.
    pdf = tmp_path / "voucher.pdf"

    async def yes(*_args, **_kwargs):
        return True

    async def save_as(*_args, **_kwargs):
        return str(pdf)

    monkeypatch.setattr(messages, "confirm", yes)
    monkeypatch.setattr(document_print, "ask_save_path", save_as)
    monkeypatch.setattr(document_print, "show_info", lambda *_a: None)
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    assert await document_print.pdf_document(None, ctx, doc_id)
    assert pdf.stat().st_size > 1000
    detail = await documents.get_document(db, admin, doc_id)
    assert detail.status == DocStatus.POSTED and await stock(db, request_shop["bags"]) == 47
    number = documents.number_text(detail.input.doc_type, detail.number)

    # 4. The next tick tells the server; the owners now see «issued» with the voucher number.
    await engine.tick(admin)
    on_server = await _owner("GET", f"/requests/{server_id}")
    assert on_server["status"] == "issued" and on_server["issue_number"] == number
    row = (await goods.get_request(db, admin, request_id)).row
    assert row.status == RequestStatus.ISSUED and not row.waiting
