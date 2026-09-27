"""Application bootstrap: Qt app, asyncio loop (qasync), fonts, theme, database, main window."""

import asyncio
import contextlib
import logging
import sys

import qasync
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from caspian import APP_DISPLAY_NAME, APP_NAME
from caspian.core.logging_setup import setup_logging
from caspian.core.secrets import get_secret, set_secret
from caspian.core.settings import Settings
from caspian.db.bootstrap import open_mariadb
from caspian.db.database import Database, DbConfig, describe_error
from caspian.services.scheduler import SchedulerRunner
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.auth_dialogs import run_login
from caspian.ui.db_setup_dialog import DbSetupDialog
from caspian.ui.fonts import apply_app_font
from caspian.ui.main_window import MainWindow
from caspian.ui.theme import ThemeManager

log = logging.getLogger(__name__)


def create_app(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    app.setQuitOnLastWindowClosed(False)
    apply_app_font(app)
    return app


async def connect_database(settings: Settings) -> tuple[Database, DbConfig] | None:
    """Open the configured DB, or ask the user until it works. None = user gave up."""
    config = DbConfig.from_dict(settings.database)
    password, error = "", ""
    if config is not None:
        password = get_secret("db", config.secret_name) or ""
        try:
            return await open_mariadb(config, password), config
        except Exception as exc:
            log.warning("Stored DB connection failed: %s", exc)
            error = describe_error(exc)

    dialog = DbSetupDialog(config, password, error)
    if await exec_dialog(dialog) != QDialog.DialogCode.Accepted:
        return None
    settings.database = dialog.config.to_dict()
    settings.save()
    set_secret("db", dialog.config.secret_name, dialog.password)
    return dialog.database, dialog.config


async def _main(settings: Settings, themes: ThemeManager) -> None:
    app = QApplication.instance()
    connected = await connect_database(settings)
    if connected is None:
        app.quit()
        return
    db, config = connected
    try:
        actor = await run_login(db)
        if actor is None:
            return
        ctx = AppContext(db, config, settings, themes, actor)
        runner = SchedulerRunner(db, ctx.messenger)
        runner.start()
        window = MainWindow(ctx)
        closed = asyncio.Event()
        window.closed.connect(closed.set)
        window.show()
        await closed.wait()
        await runner.stop()
    finally:
        await db.dispose()
        app.quit()


def run() -> int:
    setup_logging()
    log.info("Starting %s", APP_NAME)
    app = create_app()
    settings = Settings.load()
    themes = ThemeManager(app, settings.theme_mode)
    with contextlib.suppress(asyncio.CancelledError):
        qasync.run(_main(settings, themes))
    return 0
