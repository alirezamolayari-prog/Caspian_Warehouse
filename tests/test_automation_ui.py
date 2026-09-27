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
