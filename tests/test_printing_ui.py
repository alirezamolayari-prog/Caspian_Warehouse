from PySide6.QtWidgets import QWidget

from caspian.core.settings import Settings
from caspian.ui import printing
from helpers import wait_until


async def test_every_output_menu_action_runs(qtbot, monkeypatch):
    """Clicking «چاپ…» / «ذخیره PDF…» must reach the print/PDF code (#1)."""
    calls = []

    async def fake_print(html_text, parent=None):
        calls.append(("print", html_text))
        return True

    async def fake_pdf(parent, html_text, default_name, settings):
        calls.append(("pdf", html_text, default_name))

    monkeypatch.setattr(printing, "print_html", fake_print)
    monkeypatch.setattr(printing, "export_pdf", fake_pdf)
    parent = QWidget()
    qtbot.addWidget(parent)

    async def make_html():
        return "<p>گزارش</p>"

    menu = printing.output_menu(parent, make_html, lambda: "report.pdf", Settings())
    actions = menu.actions()
    assert [a.text() for a in actions] == ["چاپ…", "ذخیره PDF…"]
    for action in actions:
        action.trigger()  # emits triggered(checked=False), like a real click
    assert await wait_until(lambda: len(calls) == 2)
    assert calls == [("print", "<p>گزارش</p>"), ("pdf", "<p>گزارش</p>", "report.pdf")]


async def test_spawned_errors_reach_the_exception_hook(qtbot, monkeypatch):
    import sys

    from caspian.ui.tasks import callback

    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: seen.append(e))

    async def boom():
        raise RuntimeError("x")

    callback(boom)(False)
    assert await wait_until(lambda: seen)
    assert isinstance(seen[0], RuntimeError)
