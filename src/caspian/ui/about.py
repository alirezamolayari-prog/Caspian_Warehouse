"""About dialog: version and third-party licenses."""

import platform

from PySide6 import __version__ as pyside_version
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from caspian import APP_DISPLAY_NAME, __version__

LICENSES = (
    ("Vazirmatn", "فونت فارسی — Saber Rastikerdar", "SIL Open Font License 1.1"),
    ("Lucide", "آیکون‌ها", "ISC"),
    ("Qt / PySide6", "رابط کاربری", "LGPL v3"),
    ("MariaDB", "پایگاه داده و ابزارهای پشتیبان‌گیری", "GPL v2"),
    ("SQLAlchemy / Alembic", "دسترسی به داده", "MIT"),
    ("openpyxl / python-docx / rapidfuzz / httpx / cryptography", "کتابخانه‌ها",
     "MIT / BSD / Apache-2.0"),
    ("Model Context Protocol SDK", "دسترسی فقط‌خواندنی ابزارهای هوش مصنوعی", "MIT"),
)


class AboutDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("درباره برنامه")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(10)
        layout.addWidget(QLabel(APP_DISPLAY_NAME, objectName="PageTitle"))
        layout.addWidget(QLabel(f"نسخه {__version__} — سامانه هوشمند مدیریت انبار، آفلاین‌محور",
                                objectName="Muted"))
        layout.addWidget(QLabel(f"Python {platform.python_version()} — PySide6 {pyside_version}",
                                objectName="Muted"))
        layout.addSpacing(8)
        layout.addWidget(QLabel("اجزای متن‌باز استفاده‌شده:", objectName="CardTitle"))
        for name, role, lic in LICENSES:
            label = QLabel(f"• <b>{name}</b> — {role} ({lic})")
            label.setWordWrap(True)
            layout.addWidget(label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("بستن")
        close.setProperty("variant", "primary")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
