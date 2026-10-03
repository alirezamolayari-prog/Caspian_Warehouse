"""QA round 2, feature B: «سوابق گفتگو» dialog and the retention setting."""

import asyncio

import httpx

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.services.ai import history
from caspian.services.ai.gateway import Gateway
from caspian.ui import assistant_page, chat_history
from caspian.ui.app_context import AppContext
from caspian.ui.assistant_page import AssistantPage
from caspian.ui.chat_history import HistoryDialog


def make_ctx(db, admin, themes):
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    ctx.ai = Gateway(db, httpx.MockTransport(lambda r: httpx.Response(500)), online_check=lambda: False)
    return ctx


async def _exchange(db, user_id, conv, command, reply, docs=None):
    await history.record(db, user_id, conv, "user", command)
    await history.record(db, user_id, conv, "assistant", reply, "Local", docs)


async def test_dialog_pairs_commands_and_loads_more(qtbot, themes, db, admin, monkeypatch):
    monkeypatch.setattr(chat_history, "PAGE", 4)
    for i in range(5):
        await _exchange(db, admin.user_id, "c", f"command {i}", f"reply {i}", [10 + i])
    dialog = HistoryDialog(make_ctx(db, admin, themes))
    qtbot.addWidget(dialog)
    await dialog.start()
    assert dialog.table.model().rowCount() == 2  # 4 newest lines = 2 commands
    assert dialog.more_button.isEnabled()
    await dialog.load_more()
    await dialog.load_more()
    assert dialog.table.model().rowCount() == 5 and not dialog.more_button.isEnabled()
    newest = dialog.table.model().index(0, 2).data()
    assert newest == "command 4" and dialog.table.model().index(0, 3).data() == "reply 4"
    assert dialog.table.model().index(0, 4).data() == "۱۴"
    dialog.search.setText("command 2")
    await dialog.reload()
    assert dialog.table.model().rowCount() == 1


async def test_reuse_puts_the_command_back_into_the_input(qtbot, themes, db, admin, monkeypatch):
    await _exchange(db, admin.user_id, "c", "how many drills", "10")

    async def fake_exec(dialog):
        dialog.table.selectRow(0)
        await dialog.submit()
        return True

    monkeypatch.setattr(assistant_page, "exec_dialog", fake_exec)
    ctx = make_ctx(db, admin, themes)
    page = AssistantPage(ctx)
    qtbot.addWidget(page)
    await page.on_history()
    assert page.input.text() == "how many drills"


async def test_retention_setting(qtbot, themes, db, admin):
    from caspian.ui.settings_page import AITab

    ctx = make_ctx(db, admin, themes)
    tab = AITab(ctx)
    qtbot.addWidget(tab)
    await tab.refresh()
    assert tab.history_days.value() == 30
    tab.history_days.setValue(45)
    tab.history_days.editingFinished.emit()
    for _ in range(100):
        if await history.retention_days(db) == 45:
            break
        await asyncio.sleep(0.02)
    assert await history.retention_days(db) == 45

