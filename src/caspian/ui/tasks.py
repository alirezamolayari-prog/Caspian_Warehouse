"""Running coroutines from Qt callbacks that are not bound methods.

`@asyncSlot` only works on methods: PySide calls a `(*args)` wrapper with no arguments, and
qasync refuses a call without any (a method still gets `self`). Closures use `spawn` instead.
"""

import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any

_running: set[asyncio.Task] = set()  # keep tasks alive until they finish


def _done(task: asyncio.Task) -> None:
    _running.discard(task)
    if task.cancelled():
        return
    if (exc := task.exception()) is not None:
        sys.excepthook(type(exc), exc, exc.__traceback__)  # same path as qasync's asyncSlot


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    _running.add(task)
    task.add_done_callback(_done)
    return task


def callback(coro_fn: Callable[[], Coroutine[Any, Any, Any]]) -> Callable[..., None]:
    """A plain callable for `connect`/`addAction` that starts `coro_fn()`; signal args are ignored."""

    def run(*_args) -> None:
        spawn(coro_fn())

    return run
