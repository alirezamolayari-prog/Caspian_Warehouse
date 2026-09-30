import datetime as dt

from PySide6.QtCore import QByteArray, QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian import APP_DISPLAY_NAME, __version__
from caspian.core import jalali
from caspian.core.permissions import DEFAULT_ROLES
from caspian.db.models import DocStatus
from caspian.services import auth
from caspian.services.actor import Actor
from caspian.ui.about import AboutDialog
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.assistant_page import AssistantPage
from caspian.ui.auth_dialogs import ChangePasswordDialog, SetPinDialog, run_login
from caspian.ui.dialogs import FormDialog
from caspian.ui.documents_page import DocumentsPage
from caspian.ui.icons import icon
from caspian.ui.imports_page import ImportsPage
from caspian.ui.items_page import ItemsPage
from caspian.ui.master_page import MasterDataPage
from caspian.ui.messages import show_info
from caspian.ui.pages import PAGES, DashboardPage, PageSpec, PlaceholderPage
from caspian.ui.reports_page import ReportsPage
from caspian.ui.settings_page import SettingsPage
from caspian.ui.stocktake_page import StocktakePage
from caspian.ui.theme import Theme
from caspian.ui.users_page import UsersPage


class ProfileDialog(FormDialog):
    """«پروفایل من»: who is signed in, with a shortcut to change the password."""

    confirm_discard = False  # nothing to lose on closing

    def __init__(self, ctx, parent=None) -> None:
        actor = ctx.actor
        super().__init__("پروفایل من", submit_text="تغییر رمز عبور", cancel_text="بستن", parent=parent)
        role = DEFAULT_ROLES.get(actor.role_code, (actor.role_code,))[0]
        for label, value in (("نام کاربری:", actor.username), ("نام:", actor.full_name or "—"),
                             ("نقش:", role)):
            self.form.addRow(label, QLabel(value))
        self.wants_password_change = False

    async def submit(self) -> None:
        self.wants_password_change = True


class MainWindow(QMainWindow):
    closed = Signal()

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._themes = ctx.themes
        self._settings = ctx.settings
        self._closing_for_logout = False
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setMinimumSize(1100, 700)

        root = QWidget()
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        row.addWidget(self._build_content(), 1)
        self.setCentralWidget(root)
        self._build_status_bar()

        self._themes.theme_changed.connect(self._on_theme_changed)
        ctx.user_changed.connect(self._on_user_changed)
        self._install_shortcuts()
        self._on_user_changed(ctx.actor)

        if self._settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(self._settings.window_geometry.encode()))

    # ----- layout -----

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame(objectName="Sidebar")
        sidebar.setFixedWidth(232)
        col = QVBoxLayout(sidebar)
        col.setContentsMargins(14, 20, 14, 14)
        col.setSpacing(4)

        brand = QHBoxLayout()
        self._brand_icon = QLabel()
        brand.addWidget(self._brand_icon)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(QLabel(APP_DISPLAY_NAME, objectName="SidebarTitle"))
        titles.addWidget(QLabel("سامانه هوشمند مدیریت انبار", objectName="SidebarSubtitle"))
        brand.addLayout(titles, 1)
        col.addLayout(brand)
        col.addSpacing(20)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: dict[str, QPushButton] = {}
        for spec in PAGES:
            if not spec.bottom:
                col.addWidget(self._make_nav_button(spec))
        col.addStretch(1)
        for spec in PAGES:
            if spec.bottom:
                col.addWidget(self._make_nav_button(spec))
        return sidebar

    def _make_nav_button(self, spec: PageSpec) -> QPushButton:
        button = QPushButton(spec.title, objectName="NavButton")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIconSize(QSize(18, 18))
        button.setProperty("icon_name", spec.icon)
        button.clicked.connect(lambda _=False, key=spec.key: self.navigate(key))
        self._nav_group.addButton(button)
        self._nav_buttons[spec.key] = button
        return button

    def _build_content(self) -> QWidget:
        content = QWidget(objectName="ContentArea")
        col = QVBoxLayout(content)
        col.setContentsMargins(28, 20, 28, 20)
        col.setSpacing(18)

        header = QHBoxLayout()
        self._page_title = QLabel(objectName="PageTitle")
        header.addWidget(self._page_title)
        header.addStretch(1)

        self._assistant_button = QToolButton(objectName="IconButton")
        self._assistant_button.setToolTip("دستیار هوشمند")
        self._assistant_button.setIconSize(QSize(20, 20))
        self._assistant_button.clicked.connect(lambda: self.navigate("assistant"))
        header.addWidget(self._assistant_button)

        self._theme_button = QToolButton(objectName="IconButton")
        self._theme_button.setToolTip("تغییر پوسته روشن / تیره")
        self._theme_button.setIconSize(QSize(20, 20))
        self._theme_button.clicked.connect(self._themes.toggle)
        header.addWidget(self._theme_button)

        self._user_button = QToolButton(objectName="UserChip")
        self._user_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._user_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._user_button.setIconSize(QSize(16, 16))
        self._user_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._user_menu = QMenu(self)
        self._user_menu.addAction("پروفایل من", self.on_profile)
        self._user_menu.addAction("تغییر رمز عبور", self.on_change_password)
        self._pin_action = self._user_menu.addAction("تنظیم PIN مدیر", self.on_set_pin)
        self._users_action = self._user_menu.addAction("مدیریت کاربران", lambda: self.navigate("users"))
        self._new_user_action = self._user_menu.addAction("کاربر جدید", self.on_new_user)
        self._user_menu.addSeparator()
        self._user_menu.addAction("تغییر کاربر", self.on_switch_user)
        self._user_menu.addAction("خروج از حساب", self.on_logout)
        self._user_menu.addSeparator()
        self._user_menu.addAction("درباره برنامه", self.show_about)
        self._user_button.setMenu(self._user_menu)
        header.addWidget(self._user_button)
        col.addLayout(header)

        self._stack = QStackedWidget()
        self._pages: dict[str, QWidget] = {}
        for spec in PAGES:
            if spec.key == "dashboard":
                page = DashboardPage(self.ctx)
                page.open_page.connect(self.open_from_dashboard)
            elif spec.key == "items":
                page = ItemsPage(self.ctx)
            elif spec.key == "master":
                page = MasterDataPage(self.ctx)
            elif spec.key == "documents":
                page = DocumentsPage(self.ctx)
            elif spec.key == "imports":
                page = ImportsPage(self.ctx, open_document=self.open_document,
                                   new_document=self.new_document)
            elif spec.key == "stocktake":
                page = StocktakePage(self.ctx)
            elif spec.key == "reports":
                page = ReportsPage(self.ctx)
            elif spec.key == "assistant":
                page = AssistantPage(self.ctx, open_batch=self.open_import_batch)
            elif spec.key == "settings":
                page = SettingsPage(self.ctx)
            elif spec.key == "users":
                page = UsersPage(self.ctx)
            else:
                page = PlaceholderPage(spec)
            self._pages[spec.key] = page
            self._stack.addWidget(page)
        col.addWidget(self._stack, 1)
        return content

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        config = self.ctx.db_config
        bar.addWidget(QLabel(f"پایگاه داده: {config.name} @ {config.host}"))
        bar.addPermanentWidget(QLabel(jalali.format_date(dt.date.today())))
        bar.addPermanentWidget(QLabel(f"نسخه {__version__}"))

    # ----- navigation & permissions -----

    def is_allowed(self, key: str) -> bool:
        spec = next(p for p in PAGES if p.key == key)
        return spec.perm is None or self.ctx.actor.can(spec.perm)

    def navigate(self, key: str) -> None:
        if not self.is_allowed(key):
            key = "dashboard"
        self._stack.setCurrentWidget(self._pages[key])
        self._nav_buttons[key].setChecked(True)
        self._page_title.setText(next(p.title for p in PAGES if p.key == key))
        self._refresh_icons()

    def open_from_dashboard(self, key: str, option: str) -> None:
        self.navigate(key)
        if option == "low_stock":
            self._pages["items"].low_only.setChecked(True)
        elif option == "loans":
            documents = self._pages["documents"]
            documents.tabs.setCurrentWidget(documents.loans)
        elif option == "drafts":
            documents = self._pages["documents"]
            documents.tabs.setCurrentWidget(documents.documents)
            documents.documents.status_filter.setCurrentIndex(
                documents.documents.status_filter.findData(DocStatus.DRAFT))

    def _install_shortcuts(self) -> None:
        visible = [p.key for p in PAGES]
        for number, key in enumerate(visible[:9], start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{number}"), self)
            shortcut.activated.connect(lambda k=key: self.navigate(k))

    def show_about(self) -> None:
        AboutDialog(self).open()

    async def new_document(self, doc_type) -> None:
        self.navigate("documents")
        await self._pages["documents"].documents.open_editor(doc_type, None)

    async def open_document(self, doc_id: int) -> None:
        self.navigate("documents")
        await self._pages["documents"].documents.open_document(doc_id)

    @asyncSlot()
    async def open_import_batch(self, batch_id: int) -> None:
        """Jump from the assistant to the review screen of a draft it created."""
        self.navigate("imports")
        await self._pages["imports"].open_review(batch_id)

    @property
    def current_page(self) -> str:
        current = self._stack.currentWidget()
        return next(k for k, w in self._pages.items() if w is current)

    def _on_user_changed(self, actor: Actor) -> None:
        self._user_button.setText(actor.display_name)
        self._user_button.setToolTip(f"{actor.username} — {actor.role_code}")
        self._pin_action.setVisible(actor.is_admin)
        for action in (self._users_action, self._new_user_action):
            action.setVisible(self.is_allowed("users"))
        for key, button in self._nav_buttons.items():
            button.setVisible(self.is_allowed(key))
        self._assistant_button.setVisible(self.is_allowed("assistant"))
        current = self._stack.currentWidget()
        self.navigate(self.current_page if current is not None else "dashboard")

    # ----- user menu -----

    @asyncSlot()
    async def on_profile(self) -> None:
        dialog = ProfileDialog(self.ctx, self)
        if await exec_dialog(dialog) and dialog.wants_password_change:
            await self.on_change_password()

    @asyncSlot()
    async def on_new_user(self) -> None:
        self.navigate("users")
        await self._pages["users"].on_new()

    @asyncSlot()
    async def on_change_password(self) -> None:
        if await exec_dialog(ChangePasswordDialog(self.ctx.db, self.ctx.actor, parent=self)):
            show_info(self, "رمز عبور تغییر کرد.")

    @asyncSlot()
    async def on_set_pin(self) -> None:
        if await exec_dialog(SetPinDialog(self.ctx.db, self.ctx.actor, parent=self)):
            show_info(self, "کد PIN مدیر ذخیره شد.")

    @asyncSlot()
    async def on_switch_user(self) -> None:
        actor = await run_login(self.ctx.db, cancel_text="انصراف", parent=self)
        if actor is not None and actor != self.ctx.actor:
            await auth.logout(self.ctx.db, self.ctx.actor)
            self.ctx.set_actor(actor)

    @asyncSlot()
    async def on_logout(self) -> None:
        await auth.logout(self.ctx.db, self.ctx.actor)
        self.hide()
        actor = await run_login(self.ctx.db)
        if actor is None:
            self.close()
            return
        self.ctx.set_actor(actor)
        self.show()

    # ----- theme -----

    def _on_theme_changed(self, theme: Theme) -> None:
        self._refresh_icons()
        self._settings.theme_mode = self._themes.mode

    def _refresh_icons(self) -> None:
        theme = self._themes.current
        for button in self._nav_buttons.values():
            name = button.property("icon_name")
            color = theme.primary if button.isChecked() else theme.text_muted
            button.setIcon(icon(name, color))
        self._brand_icon.setPixmap(icon("boxes", theme.primary).pixmap(28, 28))
        self._assistant_button.setIcon(icon("sparkles", theme.text_muted))
        self._theme_button.setIcon(icon("sun" if theme.is_dark else "moon", theme.text_muted))
        self._user_button.setIcon(icon("user", theme.text_muted))

    def closeEvent(self, event) -> None:
        self._settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self._settings.theme_mode = self._themes.mode
        self._settings.save()
        super().closeEvent(event)
        self.closed.emit()
