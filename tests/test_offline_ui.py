"""Go-live without Caspian Server, on screen: the requests page says plainly that requests are kept on
this PC, and the status bar shows no alarm while no server is set."""

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.services.server_link.sync import SyncEngine, SyncRunner, SyncState, TickResult
from caspian.ui.app_context import AppContext
from caspian.ui.main_window import MainWindow
from caspian.ui.requests_page import RequestsPage
from caspian.ui.server_status import ServerLink


def _ctx(themes, db, actor, linked=True):
    ctx = AppContext(db, DbConfig(), Settings(), themes, actor)
    if linked:
        ctx.server_link = ServerLink(SyncRunner(SyncEngine(db)))
    return ctx


async def test_requests_page_explains_offline_work(qtbot, themes, db, admin):
    ctx = _ctx(themes, db, admin)
    page = RequestsPage(ctx)
    qtbot.addWidget(page)
    page.show()
    for state in (SyncState.NOT_CONFIGURED, SyncState.DISCONNECTED, SyncState.ACCOUNT_PROBLEM):
        ctx.server_link._on_result(TickResult(state, "پیام"))
        assert page.offline_banner.isVisible()
        assert "روی همین رایانه ذخیره" in page.offline_banner.text()
    ctx.server_link._on_result(TickResult(SyncState.CONNECTED))
    assert not page.offline_banner.isVisible()


async def test_status_bar_stays_quiet_when_no_server_is_set(qtbot, themes, db, admin):
    ctx = _ctx(themes, db, admin)
    window = MainWindow(ctx)
    qtbot.addWidget(window)
    window.show()
    ctx.server_link._on_result(TickResult(SyncState.NOT_CONFIGURED))
    assert not window.server_status.isVisible()  # no red «قطع» on a PC that works without a server
    ctx.server_link._on_result(TickResult(SyncState.DISCONNECTED, "سرور در دسترس نیست"))
    assert window.server_status.isVisible() and window.server_status.text() == "سرور: قطع"
