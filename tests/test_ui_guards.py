"""Static guards against bug classes found in manual QA (see docs/CASPIAN_FIXES.md)."""

import ast
from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[1] / "src" / "caspian" / "ui"


def _modules():
    for path in sorted(UI_DIR.glob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_async_slot(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (isinstance(target, ast.Name) and target.id == "asyncSlot") or (
        isinstance(target, ast.Attribute) and target.attr == "asyncSlot")


def _functions_with_owner(tree: ast.AST):
    """(function, is_method) for every function definition in the module."""
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for child in body:
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                yield child, isinstance(node, ast.ClassDef)


def test_async_slots_are_methods():
    """PySide calls qasync's `(*args)` wrapper with no arguments, and qasync refuses a call with
    none; only a bound method survives (it still gets `self`). A free function or closure with
    @asyncSlot therefore never runs ("asyncSlot was not callable from Signal" — printing, #1).
    Use caspian.ui.tasks.callback/spawn for closures."""
    offenders = []
    for path, tree in _modules():
        for func, is_method in _functions_with_owner(tree):
            if not is_method and any(_is_async_slot(d) for d in func.decorator_list):
                offenders.append(f"{path.name}:{func.lineno} {func.name}()")
    assert not offenders, "@asyncSlot on non-method functions: " + ", ".join(offenders)


MODAL_CLASSES = {"QFileDialog", "QInputDialog", "QMessageBox", "QColorDialog", "QFontDialog"}
STATIC_MODALS = {"getSaveFileName", "getOpenFileName", "getOpenFileNames", "getExistingDirectory",
                 "getText", "getInt", "getDouble", "getItem", "getColor", "getFont", "question",
                 "warning", "critical", "information", "about"}


def _is_blocking(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in ("exec", "exec_"):  # any dialog/menu/event loop
        return True
    return (func.attr in STATIC_MODALS and isinstance(func.value, ast.Name)
            and func.value.id in MODAL_CLASSES)


def _calls_in(func: ast.AsyncFunctionDef):
    """Calls executed by the coroutine itself (not by nested functions/lambdas)."""
    stack = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def test_no_blocking_dialogs_inside_coroutines():
    """A modal exec()/static dialog inside a running task spins a nested event loop in which qasync
    steps other tasks: "Cannot enter into task … SchedulerRunner._loop … while another task"
    (#35). Use caspian.ui.file_dialogs / app_context.exec_dialog instead."""
    offenders = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for call in _calls_in(node):
                if _is_blocking(call):
                    offenders.append(f"{path.name}:{call.lineno} {node.name}() -> .{call.func.attr}()")
    assert not offenders, "blocking dialogs inside async functions: " + ", ".join(offenders)
