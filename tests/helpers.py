"""Async polling helpers for UI tests (CI runners can be many times slower than dev PCs)."""

import asyncio
from collections.abc import Callable

TIMEOUT = 15.0


async def wait_until(condition: Callable[[], bool], timeout: float = TIMEOUT) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return condition()


async def settle(dialog, timeout: float = TIMEOUT) -> None:
    """Wait for a FormDialog's async submit to finish (the button is re-enabled at the end)."""
    await asyncio.sleep(0.02)  # let the submit task start and disable the button
    await wait_until(dialog.submit_button.isEnabled, timeout)
