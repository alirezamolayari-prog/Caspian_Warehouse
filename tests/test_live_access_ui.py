"""Test report issues 1 + 2 in the window: a deactivated or changed session goes back to the login
screen with a Persian message (at once on a refused action, within a minute when idle)."""

import contextlib
import datetime as dt
from decimal import Decimal

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocType
from caspian.services import auth, documents, master, users
from caspian.services.errors import ServiceError
from caspian.ui import main_window
from caspian.ui.app_context import AppContext
from caspian.ui.main_window import MainWindow
from helpers import wait_until

PW = "Str0ngPass1"


async def _keeper_window(qtbot, themes, db, admin, monkeypatch, next_actor=None):
    await users.create_user(db, admin, "keeper1", "انباردار", PW, "storekeeper")
    keeper = (await auth.login(db, "keeper1", PW)).actor
    asked = []

    async def fake_login(_db, *args, message="", **kwargs):
        asked.append(message)
        return next_actor

    monkeypatch.setattr(main_window, "run_login", fake_login)
    window = MainWindow(AppContext(db, DbConfig(), Settings(), themes, keeper))
    qtbot.addWidget(window)
    window.show()
    return window, keeper, asked


async def test_idle_deactivated_session_returns_to_login(qtbot, themes, db, admin, monkeypatch):
    window, keeper, asked = await _keeper_window(qtbot, themes, db, admin, monkeypatch)
    await window.check_session()
    assert window.isVisible() and not asked  # still valid: nothing happens
    await users.set_active(db, admin, keeper.user_id, False)
    await window.check_session()
    assert asked and "غیرفعال" in asked[0]
    assert not window.isVisible()  # the login was cancelled: the app closes


async def test_refused_action_returns_to_login_at_once(qtbot, themes, db, admin, monkeypatch):
    window, keeper, asked = await _keeper_window(qtbot, themes, db, admin, monkeypatch, next_actor=admin)
    await users.set_active(db, admin, keeper.user_id, False)
    wh = (await master.list_warehouses(db))[0].id
    unit = (await master.list_units(db))[0].id
    with contextlib.suppress(ServiceError):  # refused: that is the point
        await documents.create_document(db, keeper, documents.DocumentInput(
            DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(1, unit, Decimal(1))]))
    assert await wait_until(lambda: bool(asked) and window.isVisible() and window.ctx.actor == admin)
    assert "غیرفعال" in asked[0]
