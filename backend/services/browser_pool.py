from __future__ import annotations

import asyncio

from backend.config import get_settings

# One semaphore per event loop: asyncio primitives bind to the loop they're
# first awaited on, and tests spin up a fresh loop per test — a single
# module-level semaphore would leak across loops and raise. Keyed by loop id.
_semaphores: dict[int, asyncio.Semaphore] = {}


def browser_semaphore() -> asyncio.Semaphore:
    """
    Cap concurrent Playwright browser launches at PLAYWRIGHT_MAX_BROWSERS.

    Chromium costs ~600-800MB per instance on this app's 8GB-RAM target;
    every browser-launching call site must acquire this before launching:

        async with browser_semaphore():
            browser = await pw.chromium.launch(...)
    """
    loop = asyncio.get_running_loop()
    sem = _semaphores.get(id(loop))
    if sem is None:
        sem = asyncio.Semaphore(get_settings().playwright_max_browsers)
        _semaphores[id(loop)] = sem
    return sem
