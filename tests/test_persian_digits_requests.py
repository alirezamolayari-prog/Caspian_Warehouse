"""Persian digits in the goods-request messages (test report issue 7, for the code of this PR).

The same source scan as tests/test_persian_digits.py (PR #7), limited to the goods-request code; once
both PRs are on main the general guard covers everything and this file goes away.
"""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "caspian"
SCOPE = [SRC / "services" / "server_link", SRC / "ui" / "requests_page.py", SRC / "ui" / "server_status.py"]
PERSIAN = re.compile("[؀-ۿ]")
WRAPPERS = {"to_persian_digits", "fa", "fa_digits", "format_qty", "format_date", "format_long", "ltr"}
NUMERIC_NAMES = re.compile(r"^(no|n|i|count|total|year|number|minutes|seconds|days|line_no|row_no|rows|"
                           r"lines|attempts|status|status_code|server_id|request_id|id|qty|.*_count|.*_no|"
                           r".*_number|.*_id)$")


def _numeric(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Call):
        return getattr(expr.func, "id", getattr(expr.func, "attr", "")) in ("len", "int", "round", "sum")
    if isinstance(expr, ast.Name):
        return bool(NUMERIC_NAMES.match(expr.id))
    if isinstance(expr, ast.Attribute):
        return bool(NUMERIC_NAMES.match(expr.attr))
    if isinstance(expr, ast.BinOp):
        return _numeric(expr.left) or _numeric(expr.right)
    return False


def _files():
    for entry in SCOPE:
        yield from (sorted(entry.rglob("*.py")) if entry.is_dir() else [entry])


def bare_numbers() -> list[str]:
    found = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wrapped = {id(arg) for call in ast.walk(tree) if isinstance(call, ast.Call)
                   and getattr(call.func, "id", getattr(call.func, "attr", "")) in WRAPPERS
                   for arg in call.args}
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr) or id(node) in wrapped:
                continue
            if not PERSIAN.search("".join(v.value for v in node.values if isinstance(v, ast.Constant))):
                continue
            for value in node.values:
                if isinstance(value, ast.FormattedValue) and _numeric(value.value):
                    found.append(f"{path.relative_to(SRC)}:{node.lineno}: {ast.unparse(value.value)}")
    return found


def test_goods_request_messages_use_persian_digits():
    assert bare_numbers() == []


def test_server_refusal_code_is_persian():
    from caspian.services.server_link.errors import ServerRejected

    assert ServerRejected(418).message == "سرور این کار را نپذیرفت (کد ۴۱۸)."
