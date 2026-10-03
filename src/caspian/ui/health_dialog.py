"""«بررسی سلامت داده»: lists findings of services.health (read-only, QA round 1 #10)."""

from PySide6.QtWidgets import QLabel

from caspian.services import health
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.dialogs import FormDialog
from caspian.ui.widgets import DataTable

KIND_NAMES = {"future_date": "تاریخ آینده", "outside_year": "خارج از سال مالی باز",
              "fractional_balance": "موجودی اعشاری در واحد عددی",
              "duplicate_items": "کالاهای تکراری/مشابه"}


class HealthDialog(FormDialog):
    confirm_discard = False  # nothing to lose on closing

    def __init__(self, findings: list[health.Finding], parent=None) -> None:
        super().__init__("بررسی سلامت داده",
                         "این بررسی فقط می‌خواند و چیزی را تغییر نمی‌دهد. هر مورد را با سند عادی "
                         "اصلاح کنید تا سابقه حفظ شود.", submit_text="بستن", parent=parent)
        self.cancel_button.hide()
        self.setMinimumSize(760, 420)
        self.table = DataTable(("نوع", "مورد", "توضیح", "راه اصلاح"))
        self.table.set_empty_text("موردی پیدا نشد؛ داده‌ها سالم‌اند.")
        self.table.set_rows([(i, (KIND_NAMES[f.kind], f.title, f.detail, f.hint))
                             for i, f in enumerate(findings)])
        self.body.addWidget(self.table, 1)
        if findings:
            self.body.addWidget(QLabel(f"{len(findings)} مورد", objectName="Muted"))

    async def submit(self) -> None:
        pass


async def show_health(ctx: AppContext, parent=None) -> list[health.Finding]:
    findings = await health.check(ctx.db, ctx.actor)
    await exec_dialog(HealthDialog(findings, parent))
    return findings
