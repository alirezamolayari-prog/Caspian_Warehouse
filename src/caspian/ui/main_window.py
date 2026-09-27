import datetime as dt

from PySide6.QtCore import QByteArray, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from caspian import APP_DISPLAY_NAME, __version__
from caspian.core import jalali
from caspian.core.settings import Settings
from caspian.db.database import Database, DbConfig
from caspian.ui.icons import icon
from caspian.ui.pages import PAGES, DashboardPage, PageSpec, PlaceholderPage, SettingsPage
from caspian.ui.theme import Theme, ThemeManager


class MainWindow(QMainWindow):
    closed = Signal()

    def __init__(self, themes: ThemeManager, settings: Settings) -> None:
        super().__init__()
        self._themes = themes
        self._settings = settings
        self.db: Database | None = None
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

        themes.theme_changed.connect(self._on_theme_changed)
        self._on_theme_changed(themes.current)
        self.navigate("dashboard")

        if settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(settings.window_geometry.encode()))

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
            if spec.key == "settings":
                continue
            col.addWidget(self._make_nav_button(spec))
        col.addStretch(1)
        col.addWidget(self._make_nav_button(next(p for p in PAGES if p.key == "settings")))
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

        self._user_chip = QLabel("کاربر: —", objectName="UserChip")
        header.addWidget(self._user_chip)
        col.addLayout(header)

        self._stack = QStackedWidget()
        self._pages: dict[str, QWidget] = {}
        for spec in PAGES:
            if spec.key == "dashboard":
                page = DashboardPage()
            elif spec.key == "settings":
                page = SettingsPage(self._themes)
            else:
                page = PlaceholderPage(spec)
            self._pages[spec.key] = page
            self._stack.addWidget(page)
        col.addWidget(self._stack, 1)
        return content

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self._db_status = QLabel("پایگاه داده: پیکربندی نشده")
        bar.addWidget(self._db_status)
        bar.addPermanentWidget(QLabel(jalali.format_date(dt.date.today())))
        bar.addPermanentWidget(QLabel(f"نسخه {__version__}"))

    # ----- behavior -----

    def set_database(self, db: Database, config: DbConfig) -> None:
        self.db = db
        self._db_status.setText(f"پایگاه داده: {config.name} @ {config.host}")


    def navigate(self, key: str) -> None:
        self._stack.setCurrentWidget(self._pages[key])
        self._nav_buttons[key].setChecked(True)
        self._page_title.setText(next(p.title for p in PAGES if p.key == key))
        self._refresh_nav_icons()

    @property
    def current_page(self) -> str:
        current = self._stack.currentWidget()
        return next(k for k, w in self._pages.items() if w is current)

    def _on_theme_changed(self, theme: Theme) -> None:
        for button in self._nav_buttons.values():
            name = button.property("icon_name")
            color = theme.primary if button.isChecked() else theme.text_muted
            button.setIcon(icon(name, color))
        self._brand_icon.setPixmap(icon("boxes", theme.primary).pixmap(28, 28))
        self._assistant_button.setIcon(icon("sparkles", theme.text_muted))
        self._theme_button.setIcon(icon("sun" if theme.is_dark else "moon", theme.text_muted))
        self._settings.theme_mode = self._themes.mode

    def _refresh_nav_icons(self) -> None:
        self._on_theme_changed(self._themes.current)

    def closeEvent(self, event) -> None:
        self._settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self._settings.theme_mode = self._themes.mode
        self._settings.save()
        super().closeEvent(event)
        self.closed.emit()
