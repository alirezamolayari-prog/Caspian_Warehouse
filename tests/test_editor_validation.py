"""QA Part A #3: the issue editor validated only after «آیا مطمئنید» and showed one error at a time.
Now every problem is checked first, all are listed together, and the fields turn red."""

from decimal import Decimal

import pytest

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocType, PersonKind
from caspian.services import documents as docs
from caspian.services import items, locations, master
from caspian.services.documents import DocumentInput
from caspian.services.items import ItemInput
from caspian.ui import documents_page
from caspian.ui.app_context import AppContext
from caspian.ui.documents_page import DocumentDialog
from helpers import settle


@pytest.fixture
def asked(monkeypatch):
    questions = []

    async def yes(parent, text, yes_text="بله", danger=False, title="تأیید"):
        questions.append(text)
        return True

    monkeypatch.setattr(documents_page, "confirm", yes)
    return questions


@pytest.fixture
async def env(themes, locations_required, admin):
    db = locations_required
    u = {x.name: x.id for x in await master.list_units(db)}
    lamp = await items.create_item(db, admin, ItemInput("3001", "لامپ", u["عدد"]))
    person = await master.save_person(db, admin, "حسن نگهبان", PersonKind.EMPLOYEE)
    return {"ctx": AppContext(db, DbConfig(), Settings(), themes, admin), "lamp": lamp, "person": person,
            "u": u}


async def _dialog(qtbot, env):
    db = env["ctx"].db
    dlg = DocumentDialog(env["ctx"], DocType.ISSUE, None, await master.list_warehouses(db),
                         await master.search_persons(db), await master.list_units(db), [])
    qtbot.addWidget(dlg)
    await dlg.load_lines()
    dlg.open()
    return dlg


def _red(widget) -> bool:
    return widget.property("invalid") is True


async def test_all_problems_at_once_before_the_question(qtbot, env, asked):
    dlg = await _dialog(qtbot, env)
    dlg.submit_button.click()  # no recipient, no place, no lines
    await settle(dlg)
    text = dlg.status.text()
    assert "تحویل‌گیرنده" in text and "محل مصرف" in text and "حداقل یک ردیف" in text
    assert text.count("•") == 3
    assert asked == []  # «آیا مطمئنید» was never shown
    assert _red(dlg.person) and _red(dlg.place.edit) and _red(dlg.item_input)
    assert await docs.list_documents(env["ctx"].db, env["ctx"].actor) == []  # nothing saved

    dlg.person.select_value(env["person"])  # fixing a field clears its mark
    assert not _red(dlg.person) and _red(dlg.place.edit)


async def test_floor_and_stock_problems_are_listed_together(qtbot, env, asked):
    dlg = await _dialog(qtbot, env)
    dlg.item_input.setText("3001")
    await dlg.on_item_entered()
    dlg.person.select_value(env["person"])
    index = await locations.load_index(env["ctx"].db)
    dlg.place.set_value(index.find("AB-ASD").id)  # needs a floor
    dlg.submit_button.click()
    await settle(dlg)
    assert "طبقه" in dlg.status.text() and asked == []
    dlg.place.set_value(index.find("AB-ASD").id, next(f.id for f in index.floors if f.code == "F2"))
    dlg.submit_button.click()  # now only the stock is missing (0 lamps on hand)
    await settle(dlg)
    assert "کافی نیست" in dlg.status.text() and asked == []
    assert _red(dlg.lines[0].qty)
    [row] = await docs.list_documents(env["ctx"].db, env["ctx"].actor)  # kept as a draft (#15)
    assert "پیش‌نویس ذخیره شد" in dlg.status.text() and row.status.name == "DRAFT"


async def test_valid_issue_asks_once_then_posts(qtbot, env, asked):
    db, actor = env["ctx"].db, env["ctx"].actor
    import datetime as dt

    wh = (await master.list_warehouses(db))[0].id
    await docs.create_and_post(db, actor, DocumentInput(DocType.RECEIPT, dt.date.today(), wh, [
        docs.LineInput(env["lamp"], env["u"]["عدد"], Decimal(5))]))
    dlg = await _dialog(qtbot, env)
    dlg.item_input.setText("3001")
    await dlg.on_item_entered()
    dlg.person.select_value(env["person"])
    dlg.place.set_value((await locations.load_index(db)).find("68").id)
    dlg.submit_button.click()
    await settle(dlg)
    assert len(asked) == 1 and dlg.posted and not dlg.status.isVisible()



# ----- QA Part A #6: future dates by the server's clock, not only this PC's -----


async def test_date_after_the_servers_today_is_refused(env, monkeypatch):
    import datetime as dt

    async def server_yesterday(_s):
        return dt.date.today() - dt.timedelta(days=1)  # this PC's clock runs a day ahead

    monkeypatch.setattr(docs, "server_today", server_yesterday)
    db, actor = env["ctx"].db, env["ctx"].actor
    wh = (await master.list_warehouses(db))[0].id
    data = DocumentInput(DocType.RECEIPT, dt.date.today(), wh, [
        docs.LineInput(env["lamp"], env["u"]["عدد"], Decimal(1))])
    problems = await docs.check(db, actor, data)
    assert [p.field for p in problems] == ["date"] and "بعد از امروز" in problems[0].message
    assert await docs.clock_skew_days(db) == 1


async def test_clock_warning_in_the_status_bar(qtbot, env, monkeypatch, tmp_path):
    from caspian.ui.main_window import MainWindow
    from helpers import wait_until

    async def three_days(_db):
        return 3

    monkeypatch.setattr("caspian.core.settings.settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(docs, "clock_skew_days", three_days)
    win = MainWindow(env["ctx"])
    qtbot.addWidget(win)
    assert await wait_until(lambda: not win.clock_warning.isHidden())
    assert "ساعت این رایانه" in win.clock_warning.text()


async def test_item_code_shows_persian_digits(qtbot, env):
    """QA Part A #5: the voucher table showed «1016»."""
    dlg = await _dialog(qtbot, env)
    dlg.item_input.setText("3001")
    await dlg.on_item_entered()
    assert dlg.table.item(0, 0).text() == "۳۰۰۱"
    assert dlg.collect().lines[0].item_id == env["lamp"]  # display only; the code itself is unchanged
