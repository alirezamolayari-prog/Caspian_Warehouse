"""Test report issue 7: numbers in Persian messages use Persian digits («ردیف ۱», not «ردیف 1»).

A source scan guards the whole app: a Persian f-string must not insert a number-like value (a row
number, a count, a year, a `len(...)`) without a digit wrapper. Behaviour tests check real messages.
"""

import ast
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pytest

from caspian.db.models import DocType
from caspian.services import documents, items, master, stocktake
from caspian.services.errors import ServiceError
from caspian.services.items import ItemInput

SRC = Path(__file__).resolve().parents[1] / "src" / "caspian"
PERSIAN = re.compile("[؀-ۿ]")
WRAPPERS = {"to_persian_digits", "fa", "fa_digits", "format_qty", "format_date", "format_long", "_fa",
            "persian_number", "ltr"}
NUMERIC_NAMES = re.compile(r"^(no|n|i|count|total|year|fiscal_year|number|minutes|seconds|days|line_no|"
                           r"row_no|rows|lines|done|missing|skipped|created|updated|matched|copy|copies|qty|"
                           r"keep|size|limit|attempts|errors|status|code_no|nr|num|.*_count|.*_no|.*_year|"
                           r".*_number|low|high|busy|drafts|status_code|y|closed|.*_days|.*_months|"
                           r"MIN_.*|.*_MINUTES|.*_LENGTH|.*_MONTHS|.*_DAYS)$")


def _numeric(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Call):
        name = getattr(expr.func, "id", getattr(expr.func, "attr", ""))
        return name in ("len", "int", "round", "sum", "max", "min", "abs")
    if isinstance(expr, ast.Name):
        return bool(NUMERIC_NAMES.match(expr.id))
    if isinstance(expr, ast.Attribute):
        return bool(NUMERIC_NAMES.match(expr.attr))
    if isinstance(expr, ast.BinOp):
        return _numeric(expr.left) or _numeric(expr.right)
    return False


def _bare_numbers() -> list[str]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wrapped = {id(arg) for call in ast.walk(tree) if isinstance(call, ast.Call)
                   and getattr(call.func, "id", getattr(call.func, "attr", "")) in WRAPPERS
                   for arg in call.args}
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr) or id(node) in wrapped:  # to_persian_digits(f"…")
                continue
            text = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
            if not PERSIAN.search(text):
                continue
            for value in node.values:
                if isinstance(value, ast.FormattedValue) and _numeric(value.value):
                    found.append(f"{path.relative_to(SRC)}:{node.lineno}: {ast.unparse(value.value)}")
    return found


def test_no_persian_message_inserts_a_bare_number():
    assert _bare_numbers() == []


@pytest.fixture
async def lamp(db, admin):
    units = {u.name: u.id for u in await master.list_units(db)}
    item = await items.create_item(db, admin, ItemInput("2001", "لامپ", units["عدد"]))
    return item, units["عدد"], (await master.list_warehouses(db))[0].id


async def test_document_row_numbers_are_persian(db, admin, lamp):
    item, unit, wh = lamp
    with pytest.raises(ServiceError) as exc:
        await documents.create_document(db, admin, documents.DocumentInput(
            DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(item, unit, Decimal("1.5"))]))
    assert "ردیف ۱" in exc.value.message and not re.search("[0-9]", exc.value.message)


async def test_stocktake_messages_are_persian(db, admin, lamp):
    _item, _unit, wh = lamp
    st = await stocktake.create_stocktake(db, admin, wh)
    sheet = await stocktake.count_sheet(db, admin, st)
    with pytest.raises(ServiceError) as exc:
        await stocktake.record_counts(db, admin, st, {sheet.lines[0].id: (Decimal(-1), "")})
    assert "ردیف ۱" in exc.value.message and not re.search("[0-9]", exc.value.message)
    with pytest.raises(ServiceError) as exc:
        await stocktake.submit_counts(db, admin, st)
    assert not re.search("[0-9]", exc.value.message)
