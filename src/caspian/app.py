"""Application bootstrap: Qt app, asyncio loop (qasync), fonts, theme, database, main window."""

import asyncio
import contextlib
import logging
import sys

import qasync
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from caspian import APP_DISPLAY_NAME, APP_NAME, __version__, setup_cli
from caspian.core.logging_setup import setup_logging
from caspian.core.secrets import get_secret, set_secret
from caspian.core.settings import Settings
from caspian.db.bootstrap import open_mariadb
from caspian.db.database import Database, DbConfig, describe_error
from caspian.db.migrate import SchemaTooNew
from caspian.db.models import TaskKind
from caspian.services.backup import make_scheduled_handler
from caspian.services.scheduler import HANDLERS, SchedulerRunner
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.auth_dialogs import run_login
from caspian.ui.backup_settings import first_run_backup_prompt
from caspian.ui.db_setup_dialog import DbSetupDialog
from caspian.ui.fonts import apply_app_font
from caspian.ui.icons import icon
from caspian.ui.main_window import MainWindow
from caspian.ui.messages import show_unexpected_error
from caspian.ui.tasks import spawn
from caspian.ui.theme import LIGHT, ThemeManager

log = logging.getLogger(__name__)


def create_app(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    app.setQuitOnLastWindowClosed(False)
    apply_app_font(app)
    app.setWindowIcon(icon("boxes", LIGHT.primary))
    return app


def install_exception_hook() -> None:
    """Log unexpected errors and tell the user, instead of silently dying."""

    def hook(exc_type, exc, tb) -> None:
        log.critical("Unhandled error", exc_info=(exc_type, exc, tb))
        if QApplication.instance() is not None:
            show_unexpected_error(exc_type, exc, tb)

    sys.excepthook = hook


async def _smoke_test(themes: ThemeManager) -> None:
    """Build every page against a throwaway SQLite database (used to verify packaged builds)."""
    import tempfile
    from pathlib import Path

    from caspian.db.bootstrap import prepare
    from caspian.services import auth

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(f"sqlite+aiosqlite:///{Path(tmp) / 'smoke.db'}")
        await prepare(db)
        actor = (await auth.login(db, "admin", "admin")).actor
        settings = Settings()
        settings.save = lambda *_a, **_k: None  # never overwrite the user's real settings
        window = MainWindow(AppContext(db, DbConfig(), settings, themes, actor))
        for key in list(window._pages):
            window.navigate(key)
            await asyncio.sleep(0.05)
        window.close()
        await db.dispose()
    print("smoke test OK")


async def connect_database(settings: Settings) -> tuple[Database, DbConfig, str] | None:
    """Open the configured DB, or ask the user until it works. None = user gave up."""
    config = DbConfig.from_dict(settings.database)
    password, error = "", ""
    if config is not None:
        password = get_secret("db", config.secret_name) or ""
        try:
            return await open_mariadb(config, password), config, password
        except SchemaTooNew as exc:
            # Not a connection problem: never offer to reconfigure, just explain and stop (#9).
            log.error("Database revision %s is newer than this app", exc.revision)
            box = QMessageBox(QMessageBox.Icon.Critical, "نسخه برنامه قدیمی است", exc.message,
                              QMessageBox.StandardButton.Ok)
            box.button(QMessageBox.StandardButton.Ok).setText("خروج")
            await exec_dialog(box)
            return None
        except Exception as exc:
            log.warning("Stored DB connection failed: %s", exc)
            error = describe_error(exc)

    dialog = DbSetupDialog(config, password, error)
    if await exec_dialog(dialog) != QDialog.DialogCode.Accepted:
        return None
    settings.database = dialog.config.to_dict()
    settings.save()
    set_secret("db", dialog.config.secret_name, dialog.password)
    return dialog.database, dialog.config, dialog.password


async def _main(settings: Settings, themes: ThemeManager) -> None:
    app = QApplication.instance()
    connected = await connect_database(settings)
    if connected is None:
        app.quit()
        return
    db, config, password = connected
    try:
        actor = await run_login(db)
        if actor is None:
            return
        ctx = AppContext(db, config, settings, themes, actor, password)
        HANDLERS[TaskKind.BACKUP] = make_scheduled_handler(db, config, password, settings)
        runner = SchedulerRunner(db, ctx.messenger)
        runner.start()
        window = MainWindow(ctx)
        closed = asyncio.Event()
        window.closed.connect(closed.set)
        window.show()
        spawn(first_run_backup_prompt(ctx, window))  # asked once per PC (#33)
        await closed.wait()
        await runner.stop()
    finally:
        await db.dispose()
        app.quit()


def run() -> int:
    setup_logging()
    log.info("Starting %s %s", APP_NAME, __version__)
    if (code := setup_cli.run(sys.argv)) is not None:  # installer helper modes, no GUI
        return code
    app = create_app()
    if "--smoke-test" in sys.argv:
        qasync.run(_smoke_test(ThemeManager(app, "light")))
        return 0
    install_exception_hook()
    settings = Settings.load()
    themes = ThemeManager(app, settings.theme_mode)
    with contextlib.suppress(asyncio.CancelledError):
        qasync.run(_main(settings, themes))
    return 0
