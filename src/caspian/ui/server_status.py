"""Qt side of the server sync: the runner's results as a signal the window and pages listen to."""

from PySide6.QtCore import QObject, Signal

from caspian.services.actor import Actor
from caspian.services.server_link.sync import SyncRunner, SyncState, TickResult

STATE_TEXT = {
    SyncState.CONNECTED: "سرور: متصل",
    SyncState.DISCONNECTED: "سرور: قطع (آفلاین)",
    SyncState.ACCOUNT_PROBLEM: "سرور: مشکل حساب",
    SyncState.NOT_CONFIGURED: "سرور: تنظیم نشده",
}


class ServerLink(QObject):
    result = Signal(object)  # TickResult, after every sync tick

    def __init__(self, runner: SyncRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.runner = runner
        self.last: TickResult | None = None
        runner.on_result = self._on_result

    def _on_result(self, result: TickResult) -> None:
        self.last = result
        self.result.emit(result)

    def wake(self) -> None:
        """Send/poll now instead of at the next tick."""
        self.runner.wake()

    def retry(self) -> None:
        """Also try an account the server refused before (password fixed, approved meanwhile); requests
        held back only by that account go back to the queue."""
        self.runner.engine.reset_rejection()
        self.runner.wake()

    def set_actor(self, actor: Actor) -> None:
        """Someone signed in: their saved login may now send what waited for them."""
        self.runner.engine.reset_rejection()
        self.runner.set_actor(actor)
