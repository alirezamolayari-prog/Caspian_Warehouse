import asyncio
import shutil

import pytest
from argon2 import PasswordHasher
from sqlalchemy import event

from caspian.core import security
from caspian.db.bootstrap import prepare
from caspian.db.database import Database

FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    """Argon2 at production cost makes the suite very slow; strength isn't under test."""
    monkeypatch.setattr(security, "_hasher", FAST_HASHER)


@pytest.fixture(scope="session")
def template_db(tmp_path_factory):
    """Migrate + seed once; each test gets a copy (migrations take seconds)."""
    path = tmp_path_factory.mktemp("template") / "template.db"

    async def build():
        original = security._hasher
        security._hasher = FAST_HASHER
        try:
            database = Database(f"sqlite+aiosqlite:///{path}")
            await prepare(database)
            await database.dispose()
        finally:
            security._hasher = original

    asyncio.run(build())
    return path


@pytest.fixture
async def db(tmp_path, template_db):
    path = tmp_path / "test.db"
    shutil.copyfile(template_db, path)
    database = Database(f"sqlite+aiosqlite:///{path}")
    event.listen(database.engine.sync_engine, "connect", _no_fsync)
    yield database
    await database.dispose()


def _no_fsync(dbapi_conn, _record) -> None:
    """Tests don't need crash safety; fsync per commit dominates runtime on slow disks."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA synchronous=OFF")
    cursor.execute("PRAGMA journal_mode=MEMORY")
    cursor.close()


ADMIN_PW = "Str0ngPass"
ADMIN_PIN = "4826"


@pytest.fixture
async def admin(db):
    """The default admin after first-login setup (password changed, PIN set)."""
    from caspian.services import auth

    actor = (await auth.login(db, "admin", "admin")).actor
    await auth.change_password(db, actor, "admin", ADMIN_PW)
    await auth.set_pin(db, actor, ADMIN_PW, ADMIN_PIN)
    return actor


@pytest.fixture(scope="session")
def _session_themes(qapp):
    from caspian.ui.theme import ThemeManager

    return ThemeManager(qapp, "light")


@pytest.fixture
def themes(_session_themes):
    """One ThemeManager per session: re-applying the app stylesheet per test is slow."""
    _session_themes.set_mode("light")
    return _session_themes


@pytest.fixture(autouse=True)
def _delete_widgets(request):
    """Destroy windows left by a test; restyling hundreds of stale widgets slows the suite."""
    yield
    if "qapp" not in request.fixturenames and "qtbot" not in request.fixturenames:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    for widget in QApplication.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Tests must never overwrite the developer's real settings file."""
    monkeypatch.setattr("caspian.core.settings.settings_path", lambda: tmp_path / "settings.json")


@pytest.fixture(autouse=True)
def _isolated_logs(tmp_path, monkeypatch):
    """Tests must not write into the user's real log folder (e.g. the MCP audit log)."""
    from caspian import mcp_server

    logs = tmp_path / "Logs"
    monkeypatch.setattr("caspian.core.settings.log_dir", lambda: logs)
    monkeypatch.setattr(mcp_server, "log_dir", lambda: logs)
    if mcp_server._audit_log is not None:
        for handler in list(mcp_server._audit_log.handlers):
            mcp_server._audit_log.removeHandler(handler)
            handler.close()
    monkeypatch.setattr(mcp_server, "_audit_log", None)
    yield logs
    if mcp_server._audit_log is not None:
        for handler in list(mcp_server._audit_log.handlers):
            mcp_server._audit_log.removeHandler(handler)
            handler.close()
