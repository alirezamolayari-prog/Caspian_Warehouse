"""First-run / reconnect dialog for the MariaDB connection."""

import logging

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.db.bootstrap import open_mariadb
from caspian.db.database import DEFAULT_PORT, Database, DbConfig, describe_error
from caspian.db.provision import create_app_user
from caspian.services.errors import ValidationError

log = logging.getLogger(__name__)


class DbSetupDialog(QDialog):
    """Collects connection details, verifies them and opens the database.

    On accept, `self.database`, `self.config` and `self.password` are set.
    """

    def __init__(self, config: DbConfig | None = None, password: str = "",
                 error: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("اتصال به پایگاه داده")
        self.setMinimumWidth(460)
        self.database: Database | None = None
        self.config: DbConfig | None = None
        self.password = ""
        config = config or DbConfig()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        layout.addWidget(QLabel("اتصال به پایگاه داده", objectName="PageTitle"))
        intro = QLabel(
            "اطلاعات سرور MariaDB را وارد کنید. برای استفاده تک‌کاربره، سرور روی همین رایانه است؛ "
            "برای کار شبکه‌ای، آدرس رایانه‌ای را که سرور روی آن نصب است وارد کنید.",
            objectName="Muted",
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        mode_row = QHBoxLayout()
        self.local_radio = QRadioButton("همین رایانه (تک‌کاربره)")
        self.network_radio = QRadioButton("سرور شبکه (چندکاربره)")
        group = QButtonGroup(self)
        group.addButton(self.local_radio)
        group.addButton(self.network_radio)
        mode_row.addWidget(self.local_radio)
        mode_row.addWidget(self.network_radio)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        form = QFormLayout()
        form.setSpacing(10)
        self.host_edit = QLineEdit(config.host)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.port_spin.setLocale(QLocale.c())  # Latin digits for technical fields
        self.port_spin.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.port_spin.setValue(config.port)
        self.name_edit = QLineEdit(config.name)
        self.user_edit = QLineEdit(config.user)
        self.password_edit = QLineEdit(password)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        for edit in (self.host_edit, self.name_edit, self.user_edit, self.password_edit):
            edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        form.addRow("آدرس سرور:", self.host_edit)
        form.addRow("پورت:", self.port_spin)
        form.addRow("نام پایگاه داده:", self.name_edit)
        form.addRow("نام کاربری:", self.user_edit)
        form.addRow("رمز عبور:", self.password_edit)
        layout.addLayout(form)

        self.create_check = QCheckBox("اگر پایگاه داده وجود ندارد، ایجاد شود")
        self.create_check.setChecked(True)
        layout.addWidget(self.create_check)

        # First install: let the app create its own MariaDB account from an admin one.
        self.provision_check = QCheckBox(
            "نصب جدید: ساخت کاربر اختصاصی برنامه با حساب مدیر MariaDB (پیشنهادی)")
        layout.addWidget(self.provision_check)
        self.provision_box = QWidget()
        pform = QFormLayout(self.provision_box)
        pform.setContentsMargins(24, 0, 0, 0)
        self.admin_user_edit = QLineEdit("root")
        self.admin_user_edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.admin_password_edit = QLineEdit()
        self.admin_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.lan_check = QCheckBox("اجازه اتصال سایر رایانه‌های شبکه (رمز بالا را برای آن‌ها یادداشت کنید)")
        pform.addRow("کاربر مدیر MariaDB:", self.admin_user_edit)
        pform.addRow("رمز مدیر:", self.admin_password_edit)
        pform.addRow("", self.lan_check)
        self.provision_box.setVisible(False)
        self.provision_check.toggled.connect(self._on_provision_toggled)
        layout.addWidget(self.provision_box)

        self.status = QLabel(objectName="StatusText")
        self.status.setWordWrap(True)
        self._show_status(error, is_error=True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("خروج")
        self.cancel_button.clicked.connect(self.reject)
        self.connect_button = QPushButton("اتصال و ادامه")
        self.connect_button.setProperty("variant", "primary")
        self.connect_button.setDefault(True)
        self.connect_button.clicked.connect(self.on_connect)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.connect_button)
        layout.addLayout(buttons)

        self.local_radio.toggled.connect(self._on_mode_changed)
        (self.local_radio if config.is_local else self.network_radio).setChecked(True)
        self._on_mode_changed()

    def _on_provision_toggled(self, on: bool) -> None:
        self.provision_box.setVisible(on)
        self.password_edit.setPlaceholderText(
            "خالی = رمز تصادفی (فقط برای همین رایانه)" if on else "")
        self.adjustSize()

    def _on_mode_changed(self) -> None:
        local = self.local_radio.isChecked()
        if local:
            self.host_edit.setText("localhost")
        elif self.host_edit.text() == "localhost":
            self.host_edit.clear()
        self.host_edit.setEnabled(not local)

    def current_config(self) -> DbConfig:
        return DbConfig(
            host=self.host_edit.text().strip() or "localhost",
            port=self.port_spin.value() or DEFAULT_PORT,
            name=self.name_edit.text().strip(),
            user=self.user_edit.text().strip(),
        )

    def _show_status(self, text: str, is_error: bool = False) -> None:
        self.status.setText(text)
        self.status.setProperty("error", is_error)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _set_busy(self, busy: bool) -> None:
        for w in (self.connect_button, self.host_edit, self.port_spin, self.name_edit,
                  self.user_edit, self.password_edit, self.create_check):
            w.setEnabled(not busy)
        if not busy:
            self.host_edit.setEnabled(not self.local_radio.isChecked())

    @asyncSlot()
    async def on_connect(self) -> None:
        config = self.current_config()
        if not config.name or not config.user:
            self._show_status("نام پایگاه داده و نام کاربری الزامی است.", is_error=True)
            return
        password = self.password_edit.text()
        self._set_busy(True)
        self._show_status("در حال اتصال…")
        try:
            if self.provision_check.isChecked():
                lan = self.lan_check.isChecked()
                if lan and len(password) < 8:
                    raise ValidationError("برای اتصال شبکه‌ای، رمز کاربر برنامه را (حداقل ۸ کاراکتر) "
                                          "خودتان تعیین کنید تا در سایر رایانه‌ها وارد شود.")
                password = await create_app_user(config, self.admin_user_edit.text().strip(),
                                                 self.admin_password_edit.text(), lan, password)
            db = await open_mariadb(config, password, create=self.create_check.isChecked())
        except ValidationError as exc:
            self._show_status(exc.message, is_error=True)
            self._set_busy(False)
            return
        except Exception as exc:
            log.warning("DB connection failed: %s", exc)
            self._show_status(describe_error(exc), is_error=True)
            self._set_busy(False)
            return
        self.database, self.config, self.password = db, config, password
        self.accept()
