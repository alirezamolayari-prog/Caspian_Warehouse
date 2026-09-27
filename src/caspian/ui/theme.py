"""Theme engine: color tokens, QPalette and stylesheet for light and dark modes.

Widgets never hard-code colors; they use object names / properties styled here,
or read tokens from `ThemeManager.current` for custom painting.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from caspian import APP_NAME
from caspian.resources import ICONS_DIR

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text_muted: str
    primary: str
    primary_hover: str
    primary_soft: str
    on_primary: str
    danger: str
    danger_hover: str
    danger_soft: str
    warning: str
    success: str
    shadow: str

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"


LIGHT = Theme(
    name="light",
    bg="#F4F6F8",
    surface="#FFFFFF",
    surface_alt="#EEF1F4",
    border="#E1E5EA",
    text="#1A1F26",
    text_muted="#5F6875",
    primary="#0F766E",
    primary_hover="#0B5E58",
    primary_soft="#DDF1EE",
    on_primary="#FFFFFF",
    danger="#DC2626",
    danger_hover="#B91C1C",
    danger_soft="#FEF2F2",
    warning="#B45309",
    success="#15803D",
    shadow="#1A1F2614",
)

DARK = Theme(
    name="dark",
    bg="#0E1217",
    surface="#161B21",
    surface_alt="#1D232B",
    border="#29313B",
    text="#E6E9ED",
    text_muted="#98A2AF",
    primary="#2DD4BF",
    primary_hover="#5EEAD4",
    primary_soft="#12332F",
    on_primary="#062522",
    danger="#F87171",
    danger_hover="#FCA5A5",
    danger_soft="#2A1517",
    warning="#FBBF24",
    success="#4ADE80",
    shadow="#00000066",
)


def build_palette(t: Theme) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t.bg,
        QPalette.ColorRole.WindowText: t.text,
        QPalette.ColorRole.Base: t.surface,
        QPalette.ColorRole.AlternateBase: t.surface_alt,
        QPalette.ColorRole.Text: t.text,
        QPalette.ColorRole.Button: t.surface,
        QPalette.ColorRole.ButtonText: t.text,
        QPalette.ColorRole.Highlight: t.primary,
        QPalette.ColorRole.HighlightedText: t.on_primary,
        QPalette.ColorRole.ToolTipBase: t.surface_alt,
        QPalette.ColorRole.ToolTipText: t.text,
        QPalette.ColorRole.PlaceholderText: t.text_muted,
        QPalette.ColorRole.Link: t.primary,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText,
                 QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(t.text_muted))
    return p


def themed_svg(name: str, color: str) -> str:
    """Path to a recolored copy of a bundled icon, for use in stylesheet url()s."""
    target = Path(user_cache_dir(APP_NAME, appauthor=False)) / "icons" / f"{name}-{color[1:]}.svg"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        source = (ICONS_DIR / f"{name}.svg").read_text(encoding="utf-8")
        target.write_text(source.replace("currentColor", color), encoding="utf-8")
    return target.as_posix()


def build_stylesheet(t: Theme) -> str:
    return f"""
* {{ outline: none; }}
QWidget {{ color: {t.text}; font-size: 10pt; }}
QMainWindow, #ContentArea {{ background: {t.bg}; }}

/* Sidebar */
#Sidebar {{ background: {t.surface}; border-left: 1px solid {t.border}; }}
#SidebarTitle {{ font-size: 13pt; font-weight: 700; color: {t.text}; }}
#SidebarSubtitle {{ color: {t.text_muted}; font-size: 8.5pt; }}
#NavButton {{
    text-align: right; padding: 9px 12px; border: none; border-radius: 8px;
    background: transparent; color: {t.text_muted}; font-weight: 500;
}}
#NavButton:hover {{ background: {t.surface_alt}; color: {t.text}; }}
#NavButton:checked {{ background: {t.primary_soft}; color: {t.primary}; font-weight: 700; }}

/* Header */
#Header {{ background: {t.bg}; }}
#PageTitle {{ font-size: 16pt; font-weight: 700; }}
QToolButton#UserChip {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 16px;
    padding: 5px 12px; color: {t.text}; font-weight: 500;
}}
QToolButton#UserChip:hover {{ background: {t.surface_alt}; }}
QToolButton#UserChip::menu-indicator {{ image: none; width: 0; }}

/* Cards */
#Card {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 12px; }}
#CardTitle {{ color: {t.text_muted}; font-size: 9pt; }}
#CardValue {{ font-size: 20pt; font-weight: 700; }}
#CardHint {{ color: {t.text_muted}; font-size: 8.5pt; }}
#EmptyTitle {{ font-size: 13pt; font-weight: 700; }}
#EmptyText, #Muted, #StatusText {{ color: {t.text_muted}; }}
#StatusText[error="true"] {{ color: {t.danger}; }}

/* Buttons */
QPushButton {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px;
    padding: 7px 16px; font-weight: 500;
}}
QPushButton:hover {{ background: {t.surface_alt}; }}
QPushButton:pressed {{ background: {t.border}; }}
QPushButton:disabled {{ color: {t.text_muted}; }}
QPushButton[variant="primary"] {{
    background: {t.primary}; border-color: {t.primary}; color: {t.on_primary};
}}
QPushButton[variant="primary"]:hover {{ background: {t.primary_hover}; }}
QPushButton[variant="danger"] {{ background: {t.danger}; border-color: {t.danger}; color: #FFFFFF; }}
QPushButton[variant="danger"]:hover {{ background: {t.danger_hover}; }}
#WarningBox {{ background: {t.danger_soft}; border: 1px solid {t.danger}; border-radius: 10px; }}
QToolButton#IconButton {{
    background: transparent; border: none; border-radius: 8px; padding: 6px;
}}
QToolButton#IconButton:hover {{ background: {t.surface_alt}; }}

/* Inputs */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit, QTextEdit {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px;
    padding: 6px 10px; selection-background-color: {t.primary};
    selection-color: {t.on_primary};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border: 1px solid {t.primary}; }}
QComboBox::drop-down {{ border: none; width: 26px; subcontrol-origin: padding; subcontrol-position: center left; }}
QComboBox::down-arrow {{ image: url("{themed_svg('chevron-down', t.text_muted)}"); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{
    background: {t.surface}; border: 1px solid {t.border}; selection-background-color: {t.primary_soft};
    selection-color: {t.text};
}}

/* Tables */
QTableView, QTreeView, QListView {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px;
    gridline-color: {t.border}; alternate-background-color: {t.surface_alt};
    selection-background-color: {t.primary_soft}; selection-color: {t.text};
}}
QHeaderView::section {{
    background: {t.surface_alt}; color: {t.text_muted}; border: none;
    border-bottom: 1px solid {t.border}; padding: 8px; font-weight: 600;
}}

/* Scrollbars */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {t.border}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {t.text_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QStatusBar {{ background: {t.surface}; border-top: 1px solid {t.border}; color: {t.text_muted}; }}
QStatusBar QLabel {{ color: {t.text_muted}; padding: 0 8px; }}
QToolTip {{ background: {t.surface_alt}; color: {t.text}; border: 1px solid {t.border}; padding: 4px 8px; }}
QMenu {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 16px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t.primary_soft}; color: {t.text}; }}
"""


class ThemeManager(QObject):
    """Resolves the theme mode (system/light/dark) and applies it app-wide."""

    theme_changed = Signal(object)

    def __init__(self, app: QApplication, mode: str = "system") -> None:
        super().__init__(app)
        self._app = app
        self._mode = mode
        self.current: Theme = LIGHT
        app.setStyle("Fusion")
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_system_changed)
        self._apply()

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._apply()

    def toggle(self) -> None:
        """Switch to the opposite of what is currently shown (leaves 'system' mode)."""
        self.set_mode("light" if self.current.is_dark else "dark")

    def _system_is_dark(self) -> bool:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark

    def _resolve(self) -> Theme:
        if self._mode == "dark":
            return DARK
        if self._mode == "light":
            return LIGHT
        return DARK if self._system_is_dark() else LIGHT

    def _on_system_changed(self, _scheme) -> None:
        if self._mode == "system":
            self._apply()

    def _apply(self) -> None:
        theme = self._resolve()
        self.current = theme
        self._app.setPalette(build_palette(theme))
        self._app.setStyleSheet(build_stylesheet(theme))
        log.debug("Applied theme %s (mode=%s)", theme.name, self._mode)
        self.theme_changed.emit(theme)
