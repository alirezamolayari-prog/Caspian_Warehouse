import asyncio

import httpx
import pytest

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import TaskStatus
from caspian.services import messaging, scheduler
from caspian.services.ai.gateway import Gateway
from caspian.ui.app_context import AppContext
from caspian.ui.automation_settings import InstructionsDialog, MessagingTab, TasksTab
from helpers import settle


@pytest.fixture
def ctx(themes, db, admin, monkeypatch):
    store = {}
    monkeypatch.setattr(messaging, "get_secret", lambda k, n: store.get((k, n)))
    monkeypatch.setattr(messaging, "set_secret", lambda k, n, v: store.__setitem__((k, n), v))
    c = AppContext(db, DbConfig(), Settings(), themes, admin)
    c.ai = Gateway(db, httpx.MockTransport(lambda r: httpx.Response(500)), online_check=lambda: False)
    c.secrets = store
    return c


async def test_instructions_to_approved_task(qtbot, ctx):
    dlg = InstructionsDialog(ctx)
    qtbot.addWidget(dlg)
    dlg.text.setPlainText("- هر روز ساعت ۸ شب پشتیبان بگیر\n- هر شنبه ساعت ۹ گزارش موجودی را به تلگرام بفرست")
    dlg.submit_button.click()
    await settle(dlg)
    assert len(dlg.created) == 2
    tab = TasksTab(ctx)
    qtbot.addWidget(tab)
    await tab.refresh()
    assert tab.table.rowCount() == 2
    tab.table.selectRow(0)
    assert tab.approve_button.isEnabled()
    await tab.on_approve()
    rows = await scheduler.list_tasks(ctx.db, ctx.actor)
    assert rows[0].status is TaskStatus.ACTIVE and rows[1].status is TaskStatus.PROPOSED


async def test_messaging_tab_saves_config_and_token(qtbot, ctx):
    tab = MessagingTab(ctx)
    qtbot.addWidget(tab)
    await tab.load()
    tab.tg_enabled.setChecked(True)
    tab.tg_token.setText("123:ABC")
    tab.tg_chats.setText("111، 222")
    await tab.on_save()
    config = await messaging.load_config(ctx.db)
    assert config.telegram.chat_ids == ["111", "222"] and config.channels == ["telegram"]
    assert ctx.secrets[("msg", "telegram_token")] == "123:ABC"
    assert tab.tg_token.text() == ""  # secrets aren't left in the form


# ----- MCP (#17) -----


@pytest.fixture
def mcp_keys(monkeypatch):
    from caspian.services import mcp_settings

    store = {}
    monkeypatch.setattr(mcp_settings, "get_secret", lambda k, n: store.get((k, n)))
    monkeypatch.setattr(mcp_settings, "set_secret", lambda k, n, v: store.__setitem__((k, n), v))
    return store


def test_claude_code_command_and_address():
    from caspian.ui import mcp_runner

    exe = r"C:\Program Files\Caspian Warehouse\caspian-mcp.exe"
    assert mcp_runner.claude_code_command([exe]) == f'claude mcp add caspian-warehouse -- "{exe}"'
    assert mcp_runner.http_address(8765, False) == "http://127.0.0.1:8765/mcp"
    assert "IP" in mcp_runner.http_address(8765, True)


async def test_mcp_card_copy_token_and_drafts_switch(qtbot, ctx, mcp_keys, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from caspian.services import mcp_settings
    from caspian.ui import mcp_runner
    from caspian.ui.automation_settings import McpSection
    from helpers import wait_until

    exe = r"C:\Program Files\Caspian Warehouse\caspian-mcp.exe"
    monkeypatch.setattr(mcp_runner, "mcp_command", lambda: [exe])

    async def not_running(port, transport=None):
        return False

    monkeypatch.setattr(mcp_runner, "http_running", not_running)
    card = McpSection(ctx)
    qtbot.addWidget(card)
    await card.refresh_http()
    assert await wait_until(lambda: card.http_state.text() == "متوقف")
    assert card.token.text() == "" and card.allow_drafts.isVisibleTo(card)

    card.on_copy()
    copied = QApplication.clipboard().text()
    assert '"mcpServers"' in copied
    assert f'claude mcp add caspian-warehouse -- "{exe}"' in copied

    card.on_new_token()
    assert len(card.token.text()) >= 32 and mcp_keys[("mcp", "http_token")] == card.token.text()
    assert card.token.echoMode() == card.token.EchoMode.Password  # hidden unless «نمایش»

    card.allow_drafts.setChecked(True)  # admin switch, stored in app_settings and audited
    for _ in range(200):
        if await mcp_settings.allow_drafts(ctx.db):
            break
        await asyncio.sleep(0.02)
    assert await mcp_settings.allow_drafts(ctx.db)


async def test_mcp_test_button_reports_failures(qtbot, ctx, mcp_keys, monkeypatch):
    from caspian.ui import automation_settings, mcp_runner
    from caspian.ui.automation_settings import McpSection
    from helpers import wait_until

    async def broken(command, timeout=45.0):
        raise RuntimeError("Database is not configured")

    shown = []
    monkeypatch.setattr(mcp_runner, "probe_stdio", broken)
    monkeypatch.setattr(automation_settings, "show_error", lambda parent, text: shown.append(text))
    card = McpSection(ctx)
    qtbot.addWidget(card)
    await card.on_test()
    assert await wait_until(lambda: shown)
    assert "پاسخ نداد" in shown[0] and "not configured" in shown[0]


async def test_probe_stdio_talks_to_a_real_server(tmp_path):
    """The «تست MCP» client against an actual MCP server process (list_tools over stdio)."""
    import sys

    from caspian.ui import mcp_runner

    script = tmp_path / "srv.py"
    script.write_text(
        "from mcp.server.mcpserver import MCPServer\n"
        "s = MCPServer(name='t')\n"
        "@s.tool()\n"
        "def ping() -> str:\n"
        "    return 'pong'\n"
        "s.run('stdio')\n", encoding="utf-8")
    assert await mcp_runner.probe_stdio([sys.executable, str(script)], timeout=60) == ["ping"]
