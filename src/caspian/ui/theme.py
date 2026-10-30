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
    accent: str  # thin brass accent: active menu bar, links
    danger: str
    danger_hover: str
    danger_soft: str
    warning: str
    success: str
    shadow: str
    # Status badges, the same everywhere: green = approved/done, lemon = waiting, red = rejected.
    ok_fg: str
    ok_bg: str
    ok_line: str
    pending_fg: str
    pending_bg: str
    pending_line: str
    bad_fg: str
    bad_bg: str
    bad_line: str

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"

    def tone(self, kind: str) -> tuple[str, str, str]:
        """(text, background, border) of a status badge: kind is "ok", "pending" or "bad"."""
        return {
            "ok": (self.ok_fg, self.ok_bg, self.ok_line),
            "pending": (self.pending_fg, self.pending_bg, self.pending_line),
            "bad": (self.bad_fg, self.bad_bg, self.bad_line),
        }[kind]


# Calm slate + white (Caspian), minimal: one primary colour, status colours only where they mean something.
LIGHT = Theme(
    name="light",
    bg="#EDF1F5",
    surface="#FFFFFF",
    surface_alt="#F4F6F9",
    border="#D8DFE7",
    text="#0F172A",
    text_muted="#475569",
    primary="#334155",
    primary_hover="#1E293B",
    primary_soft="#E3E8EE",
    on_primary="#FFFFFF",
    accent="#8A6526",
    danger="#DC2626",
    danger_hover="#B91C1C",
    danger_soft="#FEE2E2",
    warning="#A16207",
    success="#15803D",
    shadow="#0F172A14",
    ok_fg="#166534",
    ok_bg="#DCFCE7",
    ok_line="#22C55E",
    pending_fg="#713F12",
    pending_bg="#FEF9C3",
    pending_line="#EAB308",
    bad_fg="#991B1B",
    bad_bg="#FEE2E2",
    bad_line="#EF4444",
)

# The same slate theme, darker: grey, never black; no gold buttons.
DARK = Theme(
    name="dark",
    bg="#2B333E",
    surface="#343D4A",
    surface_alt="#3C4655",
    border="#4B5767",
    text="#F1F5F9",
    text_muted="#C3CDD9",
    primary="#D3DCE6",
    primary_hover="#E8EDF3",
    primary_soft="#4A5668",
    on_primary="#1E293B",
    accent="#D9BC85",
    danger="#F87171",
    danger_hover="#FCA5A5",
    danger_soft="#5E2C31",
    warning="#FDE047",
    success="#4ADE80",
    shadow="#0000004D",
    ok_fg="#BBF7D0",
    ok_bg="#24513A",
    ok_line="#4ADE80",
    pending_fg="#FEF08A",
    pending_bg="#55501C",
    pending_line="#FACC15",
    bad_fg="#FECACA",
    bad_bg="#5E2C31",
    bad_line="#F87171",
)

_current: Theme = LIGHT


def current_theme() -> Theme:
    """The theme on screen now (for painting code that has no AppContext, e.g. table delegates)."""
    return _current


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
QWidget {{ color: {t.text}; font-size: 10.5pt; }}
QMainWindow, #ContentArea {{ background: {t.bg}; }}

/* Sidebar */
#Sidebar {{ background: {t.surface}; border-left: 1px solid {t.border}; }}
#SidebarTitle {{ font-size: 13pt; font-weight: 700; color: {t.text}; }}
#SidebarSubtitle {{ color: {t.text_muted}; font-size: 8.5pt; }}
#NavButton {{
    text-align: right; padding: 10px 14px; border: none; border-right: 3px solid transparent;
    border-radius: 10px; background: transparent; color: {t.text_muted}; font-weight: 600;
}}
#NavButton:hover {{ background: {t.surface_alt}; color: {t.text}; }}
/* Same weight when selected (a bolder label would be clipped); the tint and the bar show the page. */
#NavButton:checked {{
    background: {t.primary_soft}; color: {t.text}; border-right: 3px solid {t.accent};
}}

/* Header */
#Header {{ background: {t.bg}; }}
#PageTitle {{ font-size: 18pt; font-weight: 800; }}
QToolButton#UserChip {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 18px;
    padding: 6px 14px; color: {t.text}; font-weight: 500;
}}
QToolButton#UserChip:hover {{ background: {t.surface_alt}; }}
QToolButton#UserChip::menu-indicator {{ image: none; width: 0; }}

/* Cards */
#Card {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 14px; }}
#CardTitle {{ color: {t.text_muted}; font-size: 10pt; font-weight: 600; }}
#CardValue {{ font-size: 24pt; font-weight: 800; }}
StatCard#Card:hover {{ border-color: {t.primary}; }}
#CardHint {{ color: {t.text_muted}; font-size: 9pt; }}
#EmptyTitle {{ font-size: 13pt; font-weight: 700; }}
#EmptyText, #Muted, #StatusText {{ color: {t.text_muted}; }}
#StatusText[error="true"] {{ color: {t.danger}; }}

/* Buttons */
QPushButton {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px;
    padding: 8px 18px; min-height: 20px; font-weight: 600;
}}
QPushButton:hover {{ background: {t.surface_alt}; }}
QPushButton:pressed {{ background: {t.border}; }}
QPushButton:disabled {{ color: {t.text_muted}; }}
QPushButton[variant="primary"] {{
    background: {t.primary}; border-color: {t.primary}; color: {t.on_primary};
}}
QPushButton[variant="primary"]:hover {{ background: {t.primary_hover}; }}
QPushButton[variant="danger"] {{ background: {t.danger}; border-color: {t.danger}; color: {t.on_primary if t.is_dark else '#FFFFFF'}; }}
QPushButton[variant="danger"]:hover {{ background: {t.danger_hover}; }}
QPushButton[variant="link"] {{
    background: transparent; border: none; color: {t.accent}; padding: 6px 4px;
    text-decoration: underline;
}}
QPushButton[variant="link"]:hover {{ color: {t.text}; }}
QPushButton[variant="link"]:disabled {{ color: {t.text_muted}; text-decoration: none; }}
#AccountWindow QLineEdit {{ min-height: 34px; font-size: 11pt; }}
#AccountWindow QPushButton[variant="primary"] {{ min-height: 36px; font-size: 11pt; }}
QLineEdit:disabled, QComboBox:disabled, QPlainTextEdit:disabled, QAbstractSpinBox:disabled {{
    background: {t.surface_alt}; color: {t.text_muted};
}}
#NoticeBar {{ background: {t.pending_bg}; border: 1px solid {t.pending_line}; border-radius: 12px; }}
#NoticeBar QLabel {{ color: {t.pending_fg}; }}
/* Old data to check: a calm lemon notice, not an alarming red bar. */
QPushButton#HealthWarning {{
    background: {t.pending_bg}; border: 1px solid {t.pending_line}; color: {t.pending_fg};
    border-radius: 12px; padding: 10px 16px; text-align: right;
}}
QPushButton#HealthWarning:hover {{ border-width: 2px; }}
#LockedNote {{
    background: {t.primary_soft}; border: 1px solid {t.border}; border-radius: 10px;
    padding: 8px 12px; color: {t.text};
}}
#RuleOk {{ color: {t.success}; }}
#RuleBad {{ color: {t.text_muted}; }}
#WarningBox {{ background: {t.bad_bg}; border: 1px solid {t.bad_line}; border-radius: 12px; }}
#WarningBox QLabel {{ color: {t.bad_fg}; }}
QPushButton#Chip {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 16px;
    padding: 6px 14px; color: {t.text_muted}; font-size: 9.5pt;
}}
QPushButton#Chip:hover {{ border-color: {t.text_muted}; color: {t.text}; }}
#Transcript {{ border: none; background: {t.surface}; }}
QToolButton#IconButton {{
    background: transparent; border: none; border-radius: 8px; padding: 6px;
}}
QToolButton#IconButton:hover {{ background: {t.surface_alt}; }}

/* Inputs */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit, QTextEdit {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px;
    padding: 7px 12px; min-height: 20px; selection-background-color: {t.primary};
    selection-color: {t.on_primary};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border: 2px solid {t.primary}; }}
/* Branding logo: its white background is made transparent, so it sits on the theme's surface. */
#LogoCard {{ background: transparent; border-radius: 10px; padding: 8px; }}
QPushButton#FilterChip {{
    background: {t.primary_soft}; border: 1px solid {t.border}; border-radius: 16px;
    padding: 6px 14px; color: {t.text}; font-weight: 600;
}}
QLabel#Toast {{ background: {t.ok_bg}; color: {t.ok_fg}; border: 1px solid {t.ok_line}; border-radius: 12px;
    padding: 10px 18px; font-weight: 600; }}
/* A field the user must fix before saving (all problems are shown at once). */
QLineEdit[invalid="true"], QComboBox[invalid="true"], QSpinBox[invalid="true"],
QDoubleSpinBox[invalid="true"], QPlainTextEdit[invalid="true"] {{ border: 2px solid {t.danger}; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}
QComboBox::drop-down {{ border: none; width: 26px; subcontrol-origin: padding; subcontrol-position: center left; }}
QComboBox::down-arrow {{ image: url("{themed_svg('chevron-down', t.text_muted)}"); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{
    background: {t.surface}; border: 1px solid {t.border}; selection-background-color: {t.primary_soft};
    selection-color: {t.text};
}}

/* Check boxes & radios */
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border: 1.5px solid {t.text_muted}; border-radius: 4px;
    background: {t.surface};
}}
QCheckBox::indicator:hover {{ border-color: {t.primary}; }}
QCheckBox::indicator:checked {{
    background: {t.primary}; border-color: {t.primary};
    image: url("{themed_svg('check', t.on_primary)}");
}}
QCheckBox::indicator:disabled {{ border-color: {t.border}; }}

/* Tabs */
QTabWidget::pane {{ border: none; top: 0; }}
QTabBar::tab {{
    background: transparent; color: {t.text_muted}; border: none;
    padding: 9px 18px; margin-left: 4px; border-radius: 10px; font-weight: 500;
}}
QTabBar::tab:hover {{ background: {t.surface_alt}; color: {t.text}; }}
QTabBar::tab:selected {{ background: {t.primary_soft}; color: {t.text}; font-weight: 700; }}

/* Tables */
QTableView, QTreeView, QListView {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 12px;
    gridline-color: {t.border}; alternate-background-color: {t.surface_alt};
    selection-background-color: {t.primary_soft}; selection-color: {t.text};
}}
QHeaderView::section {{
    background: {t.surface}; color: {t.text_muted}; border: none;
    border-bottom: 1px solid {t.border}; padding: 10px 8px; font-weight: 600;
}}
QTableView::item, QTreeView::item {{ padding: 4px 8px; }}

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
QMenu {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px; padding: 4px; }}
QMenu::item {{ padding: 8px 18px; border-radius: 8px; }}
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
        global _current
        theme = self._resolve()
        self.current = theme
        _current = theme
        self._app.setPalette(build_palette(theme))
        self._app.setStyleSheet(build_stylesheet(theme))
        log.debug("Applied theme %s (mode=%s)", theme.name, self._mode)
        self.theme_changed.emit(theme)
