"""«سوابق گفتگو»: past assistant conversations (QA round 2, feature B)."""

from PySide6.QtWidgets import QHBoxLayout, QPushButton

from caspian.core import jalali
from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.services import users
from caspian.services.ai import history
from caspian.services.errors import ServiceError, ValidationError
from caspian.ui.app_context import AppContext
from caspian.ui.dialogs import FormDialog
from caspian.ui.messages import confirm, show_error
from caspian.ui.tasks import spawn
from caspian.ui.widgets import DataTable, SearchableCombo, SearchBox

PAGE = 50
COLUMNS = ("زمان", "کاربر", "دستور", "پاسخ (خلاصه)", "اسناد")


class HistoryDialog(FormDialog):
    """Newest first, searchable, loads more on demand; «استفاده دوباره» puts a command back."""

    confirm_discard = False

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__("سوابق گفتگو با دستیار",
                         "گفتگوهای گذشته شما (مدیر: همه کاربران). سوابق پس از مدت تعیین‌شده در تنظیمات "
                         "خودکار پاک می‌شوند؛ صدا هرگز ذخیره نمی‌شود.",
                         submit_text="استفاده دوباره", cancel_text="بستن", parent=parent)
        self.setMinimumSize(900, 560)
        self._ctx = ctx
        self.chosen_command = ""
        self._loaded: list[history.HistoryRow] = []
        self._commands: dict[int, str] = {}
        top = QHBoxLayout()
        self.search = SearchBox("جستجو در سوابق…")
        self.search.search.connect(lambda _t: spawn(self.reload()))
        top.addWidget(self.search, 1)
        self.user = SearchableCombo("کاربر…")
        self.user.setVisible(ctx.actor.can(Perm.USERS_MANAGE))
        self.user.activated.connect(lambda _i: spawn(self.reload()))
        top.addWidget(self.user)
        self.body.addLayout(top)
        self.table = DataTable(COLUMNS)
        self.table.set_empty_text("سابقه‌ای نیست.")
        self.table.doubleClicked.connect(lambda _i: self.submit_button.click())
        self.body.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.more_button = QPushButton("بیشتر…")
        self.more_button.clicked.connect(lambda: spawn(self.load_more()))
        self.delete_button = QPushButton("حذف سوابق من")
        self.delete_button.setProperty("variant", "danger")
        self.delete_button.clicked.connect(lambda: spawn(self.on_delete()))
        row.addWidget(self.more_button)
        row.addStretch(1)
        row.addWidget(self.delete_button)
        self.body.addLayout(row)

    async def start(self) -> None:
        if self._ctx.actor.can(Perm.USERS_MANAGE):
            self.user.set_items(((u.full_name or u.username, u.id)
                                 for u in await users.list_users(self._ctx.db, self._ctx.actor)),
                                none_text="همه کاربران")
        await self.reload()

    async def reload(self) -> None:
        self._loaded: list[history.HistoryRow] = []
        await self.load_more()

    async def load_more(self) -> None:
        """Next page of older lines (lazy loading keeps the dialog fast with long histories)."""
        oldest = self._loaded[-1].id if self._loaded else None
        try:
            rows = await history.list_history(self._ctx.db, self._ctx.actor, self.user.currentData(),
                                              self.search.text(), oldest, PAGE)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        self._loaded += rows
        self.more_button.setEnabled(len(rows) == PAGE)
        self.table.set_rows(self._pairs())

    def _pairs(self) -> list[tuple[int, tuple]]:
        """One table row per command: the user's line and the assistant line that followed it."""
        pairs, pending = [], {}
        for r in sorted(self._loaded, key=lambda r: r.id):
            key = (r.conversation, r.user_id)
            if r.role == "user":
                if key in pending:
                    pairs.append((pending[key], None))
                pending[key] = r
            elif key in pending:
                pairs.append((pending.pop(key), r))
        pairs += [(u, None) for u in pending.values()]
        pairs.sort(key=lambda p: p[0].id, reverse=True)
        self._commands = {u.id: u.text for u, _a in pairs}
        out = []
        for u, a in pairs:
            when = f"{jalali.format_date(u.created_at)} {to_persian_digits(u.created_at.strftime('%H:%M'))}"
            reply = (a.text if a else "")[:120].replace("\n", " ")
            docs = "، ".join(to_persian_digits(d) for d in (a.document_ids if a else [])) or "—"
            out.append((u.id, (when, u.user, u.text[:200], reply, docs)))
        return out

    async def on_delete(self) -> None:
        if not await confirm(self, "همه سوابق گفتگوی شما حذف شود؟", "حذف", danger=True):
            return
        try:
            await history.delete_own(self._ctx.db, self._ctx.actor)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        await self.reload()

    async def submit(self) -> None:
        if (row_id := self.table.selected_id()) is None:
            raise ValidationError("دستوری را در جدول انتخاب کنید.")
        self.chosen_command = self._commands.get(row_id, "")
