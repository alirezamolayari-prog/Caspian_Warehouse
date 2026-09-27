import json

import httpx
import pytest
from PySide6.QtCore import QUrl

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import ProviderKind
from caspian.services import imports, master
from caspian.services.ai import config
from caspian.services.ai.gateway import Gateway
from caspian.ui.app_context import AppContext
from caspian.ui.assistant_page import AssistantPage, _to_html
from caspian.ui.imports_page import TextImportDialog
from helpers import settle, wait_until


@pytest.fixture
def no_keys(monkeypatch):
    monkeypatch.setattr(config, "get_secret", lambda k, n: None)


def make_ctx(db, admin, themes, handler, online=True):
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    ctx.ai = Gateway(db, httpx.MockTransport(handler), online_check=lambda: online)
    return ctx


def test_markdown_subset():
    assert _to_html("**مهم**\n- یک") == "<b>مهم</b><br>• یک"
    assert "&lt;script&gt;" in _to_html("<script>")


async def test_chat_round_trip_and_draft_link(qtbot, themes, db, admin, no_keys):
    await config.save_provider(db, admin, "Local", ProviderKind.OLLAMA, "http://localhost:11434/v1", "q")
    replies = [
        {"content": None, "tool_calls": [{"id": "1", "type": "function", "function": {
            "name": "create_stock_draft",
            "arguments": json.dumps({"doc_type": "RECEIPT", "lines": [{"name": "دریل", "qty": 3}]})}}]},
        {"content": "پیش‌نویس رسید ساخته شد."},
    ]

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                                  **replies.pop(0)}}]})

    opened = []
    page = AssistantPage(make_ctx(db, admin, themes, handler), open_batch=opened.append)
    qtbot.addWidget(page)
    await page.send("۳ دریل رسید بزن")
    html_text = page.transcript.toHtml()
    assert "پیش‌نویس رسید ساخته شد" in html_text and "batch:" in html_text
    [batch] = await imports.list_batches(db, admin)
    page.on_link(QUrl(f"batch:{batch.id}"))
    assert opened == [batch.id]


async def test_unavailable_shows_friendly_message(qtbot, themes, db, admin, no_keys):
    page = AssistantPage(make_ctx(db, admin, themes, lambda r: httpx.Response(500), online=False))
    qtbot.addWidget(page)
    await page.update_status()
    assert "در دسترس نیست" in page.status.text()
    await page.send("سلام")
    assert "ورود اطلاعات" in page.transcript.toPlainText()
    assert page.send_button.isEnabled()


async def test_text_import_dialog_offline(qtbot, themes, db, admin, no_keys):
    ctx = make_ctx(db, admin, themes, lambda r: httpx.Response(500), online=False)
    dlg = TextImportDialog(ctx, await master.list_warehouses(db), [])
    qtbot.addWidget(dlg)
    dlg.text.setPlainText("۵ عدد دریل\nمیز ۲ تا")
    dlg.submit_button.click()
    await settle(dlg)
    assert await wait_until(lambda: dlg.batch_id is not None)
    assert not dlg.used_ai
    lines = (await imports.get_batch(db, admin, dlg.batch_id)).lines
    assert [ln.name for ln in lines] == ["دریل", "میز"]
