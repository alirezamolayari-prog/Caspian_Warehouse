"""State shared by the whole UI: database, signed-in user, theme, preferences."""

import asyncio

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog

from caspian.core.settings import Settings
from caspian.db.database import Database, DbConfig
from caspian.services.actor import Actor
from caspian.ui.theme import ThemeManager


class AppContext(QObject):
    user_changed = Signal(object)  # Actor

    def __init__(self, db: Database, db_config: DbConfig, settings: Settings,
                 themes: ThemeManager, actor: Actor) -> None:
        super().__init__()
        self.db = db
        self.db_config = db_config
        self.settings = settings
        self.themes = themes
        self.actor = actor

    def set_actor(self, actor: Actor) -> None:
        self.actor = actor
        self.user_changed.emit(actor)


async def exec_dialog(dialog: QDialog) -> int:
    """Await a modal dialog without blocking the asyncio loop."""
    future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    dialog.finished.connect(lambda result: future.done() or future.set_result(result))
    dialog.open()
    return await future
