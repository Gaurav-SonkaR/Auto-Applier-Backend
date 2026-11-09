from __future__ import annotations

import asyncio

from backend.app.config import get_settings

# One semaphore per event loop: asyncio primitives bind to the loop they're
# first awaited on, and tests spin up a fresh loop per test — a single
# module-level semaphore would leak across loops and raise. Keyed by loop id.
_semaphores: dict[int, asyncio.Semaphore] = {}


def browser_semaphore() -> asyncio.Semaphore:
    """
    Cap concurrent Selenium WebDriver sessions at SELENIUM_MAX_DRIVERS.

    Chromium costs hundreds of MB per instance; all browser workflows share
    this per-event-loop cap before creating a driver:

        async with browser_semaphore():
            async with selenium_session(settings) as session:
                ...
    """
    loop = asyncio.get_running_loop()
    sem = _semaphores.get(id(loop))
    if sem is None:
        sem = asyncio.Semaphore(get_settings().selenium_max_drivers)
        _semaphores[id(loop)] = sem
    return sem


async def random_human_delay(settings=None) -> None:
    """Async sleep for a random interval within configured human delay bounds."""
    import random
    s = settings or get_settings()
    ms = random.randint(s.human_delay_min_ms, s.human_delay_max_ms)
    await asyncio.sleep(ms / 1000)

