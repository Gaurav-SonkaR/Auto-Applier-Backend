"""Async-safe Selenium sessions for the FastAPI/LangGraph browser workflows.

Selenium WebDriver is synchronous and must stay on one thread. Each session
owns a one-worker executor; every driver/WebElement command is serialized there,
while FastAPI's event loop remains available for LLM and API work.
"""
from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait

from backend.app.services.browser_pool import browser_semaphore

logger = logging.getLogger("browser.selenium")


class SeleniumElement:
    """Async façade around a WebElement, always dispatched to its driver thread."""

    def __init__(self, page: "SeleniumPage", element: Any):
        self._page = page
        self._element = element

    async def get_attribute(self, name: str) -> Optional[str]:
        return await self._page._run(self._element.get_attribute, name)

    async def find(self, selector: str) -> Optional["SeleniumElement"]:
        try:
            element = await self._page._run(
                self._element.find_element, By.CSS_SELECTOR, selector
            )
            return SeleniumElement(self._page, element)
        except NoSuchElementException:
            return None

    async def find_all(self, selector: str) -> list["SeleniumElement"]:
        elements = await self._page._run(
            self._element.find_elements, By.CSS_SELECTOR, selector
        )
        return [SeleniumElement(self._page, element) for element in elements]

    async def text(self) -> str:
        return await self._page._run(lambda: self._element.text)

    async def is_visible(self) -> bool:
        try:
            return bool(await self._page._run(self._element.is_displayed))
        except (StaleElementReferenceException, WebDriverException):
            return False

    async def is_enabled(self) -> bool:
        try:
            return bool(await self._page._run(self._element.is_enabled))
        except (StaleElementReferenceException, WebDriverException):
            return False

    async def is_selected(self) -> bool:
        try:
            return bool(await self._page._run(self._element.is_selected))
        except (StaleElementReferenceException, WebDriverException):
            return False

    async def click(self) -> None:
        await self._page._run(self._element.click)

    async def fill(self, value: str) -> None:
        def _fill() -> None:
            self._element.clear()
            self._element.send_keys(value)
        await self._page._run(_fill)

    async def input_value(self) -> str:
        return str(await self._page._run(self._element.get_attribute, "value") or "")

    async def set_file(self, path: str) -> None:
        await self._page._run(self._element.send_keys, str(Path(path).resolve()))

    async def select(self, value: str) -> None:
        await self._page._run(lambda: Select(self._element).select_by_value(value))

    async def check(self) -> None:
        def _check() -> None:
            if not self._element.is_selected():
                self._element.click()
        await self._page._run(_check)

    async def tag_name(self) -> str:
        return str(await self._page._run(lambda: self._element.tag_name)).lower()

    async def execute_script(self, script: str, *args: Any) -> Any:
        return await self._page.execute_script(script, self, *args)

    def _unwrap(self) -> Any:
        return self._element


class SeleniumPage:
    """Small async interface used by scraper/apply logic, backed by Selenium."""

    def __init__(self, driver: webdriver.Chrome, executor: ThreadPoolExecutor):
        self._driver = driver
        self._executor = executor

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args))

    @property
    def current_url(self) -> str:
        # Callers use this after a navigation/wait on the same session. For
        # thread safety, prefer current_url_async from async workflow code.
        return str(self._driver.current_url)

    async def current_url_async(self) -> str:
        return str(await self._run(lambda: self._driver.current_url))

    async def title(self) -> str:
        return str(await self._run(lambda: self._driver.title))

    async def navigate(self, url: str, timeout: int = 30_000) -> None:
        seconds = max(timeout / 1000, 0.1)

        def _navigate() -> None:
            self._driver.set_page_load_timeout(seconds)
            self._driver.get(url)
            WebDriverWait(self._driver, seconds).until(
                lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
            )

        await self._run(_navigate)

    async def wait_ready(self, timeout: int = 15_000) -> None:
        seconds = max(timeout / 1000, 0.1)
        await self._run(lambda: WebDriverWait(self._driver, seconds).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        ))

    async def find(self, selector: str) -> Optional[SeleniumElement]:
        if not selector:
            return None
        try:
            element = await self._run(
                self._driver.find_element, By.CSS_SELECTOR, selector
            )
            return SeleniumElement(self, element)
        except NoSuchElementException:
            return None

    async def find_all(self, selector: str) -> list[SeleniumElement]:
        if not selector:
            return []
        elements = await self._run(
            self._driver.find_elements, By.CSS_SELECTOR, selector
        )
        return [SeleniumElement(self, element) for element in elements]

    async def wait_for(self, selector: str, timeout: int = 8_000,
                       *, visible: bool = False) -> Optional[SeleniumElement]:
        if not selector:
            return None
        condition = (EC.visibility_of_element_located if visible
                     else EC.presence_of_element_located)
        try:
            element = await self._run(lambda: WebDriverWait(
                self._driver, max(timeout / 1000, 0.1)
            ).until(condition((By.CSS_SELECTOR, selector)))
            )
            return SeleniumElement(self, element)
        except TimeoutException:
            return None

    async def text(self, selector: str = "body") -> str:
        element = await self.find(selector)
        return await element.text() if element else ""

    async def execute_script(self, script: str, *args: Any) -> Any:
        raw_args = [arg._unwrap() if isinstance(arg, SeleniumElement) else arg for arg in args]
        return await self._run(self._driver.execute_script, script, *raw_args)

    async def check(self, selector: str) -> None:
        element = await self.wait_for(selector, timeout=5_000, visible=True)
        if element is None:
            raise NoSuchElementException(selector)
        await element.check()

    async def screenshot(self, path: str) -> bool:
        return bool(await self._run(self._driver.save_screenshot, path))

    async def handle_cookie_consent(self) -> bool:
        from backend.app.services.cookie_consent import handle_cookie_consent

        return bool(await self._run(handle_cookie_consent, self._driver))

    async def _switch_to_default(self) -> None:
        await self._run(self._driver.switch_to.default_content)


class SeleniumSession:
    """One driver and one page, thread-confined and deterministically closed."""

    def __init__(self, driver: webdriver.Chrome, executor: ThreadPoolExecutor):
        self.driver = driver
        self.executor = executor
        self.page = SeleniumPage(driver, executor)
        self._closed = False

    @classmethod
    async def create(cls, settings, *, headless: Optional[bool] = None,
                     user_agent: str = "", locale: str = "") -> "SeleniumSession":
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="selenium-driver")
        options = ChromeOptions()
        use_headless = settings.selenium_headless if headless is None else headless
        if use_headless:
            options.add_argument("--headless=new")
        options.add_argument("--window-size=1280,800")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        if user_agent:
            options.add_argument(f"--user-agent={user_agent}")
        if locale:
            options.add_argument(f"--lang={locale}")
        if settings.chrome_binary:
            options.binary_location = settings.chrome_binary
        options.accept_insecure_certs = False

        def _create_driver():
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(30)
            driver.set_script_timeout(15)
            return driver

        loop = asyncio.get_running_loop()
        try:
            driver = await loop.run_in_executor(executor, _create_driver)
        except Exception:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        return cls(driver, executor)

    async def restore_state(self, path: str, base_url: str) -> None:
        state_path = Path(path)
        if not state_path.is_file():
            return
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            origin = _origin(base_url)

            def _restore() -> None:
                self.driver.get(origin)
                host = (urlparse(origin).hostname or "").lower()
                for cookie in data.get("cookies", []):
                    domain = str(cookie.get("domain") or "").lstrip(".").lower()
                    if domain and not (host == domain or host.endswith("." + domain)):
                        continue
                    safe_cookie = {k: v for k, v in cookie.items()
                                   if k in {"name", "value", "path", "domain", "secure",
                                            "httpOnly", "expiry", "sameSite"}}
                    if safe_cookie.get("sameSite") == "None":
                        safe_cookie["sameSite"] = "None"
                    try:
                        self.driver.add_cookie(safe_cookie)
                    except WebDriverException as exc:
                        logger.debug("Saved cookie could not be restored: %s", exc)
                for saved_origin in data.get("origins", []):
                    if saved_origin.get("origin") != origin:
                        continue
                    for item in saved_origin.get("localStorage", []):
                        self.driver.execute_script(
                            "window.localStorage.setItem(arguments[0], arguments[1])",
                            item.get("name", ""), item.get("value", ""),
                        )
                self.driver.refresh()
                WebDriverWait(self.driver, 15).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )

            await self.page._run(_restore)
        except (OSError, ValueError, WebDriverException, TimeoutException) as exc:
            logger.warning("Could not restore Selenium browser session: %s", exc)

    async def save_state(self, path: str) -> None:
        def _capture() -> dict[str, Any]:
            url = self.driver.current_url
            origin = _origin(url)
            local_storage = self.driver.execute_script(
                "return Object.entries(window.localStorage).map(([name, value]) => ({name, value}))"
            )
            return {"cookies": self.driver.get_cookies(),
                    "origins": [{"origin": origin, "localStorage": local_storage}]}

        data = await self.page._run(_capture)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.shield(self.page._run(self.driver.quit))
        except (WebDriverException, RuntimeError) as exc:
            logger.debug("Selenium driver shutdown completed with: %s", exc)
        finally:
            self.executor.shutdown(wait=False, cancel_futures=True)


@asynccontextmanager
async def selenium_session(settings, *, headless: Optional[bool] = None,
                           user_agent: str = "", locale: str = "",
                           storage_path: str = "", base_url: str = ""):
    """Create a capped Selenium driver session and always quit its process."""
    async with browser_semaphore():
        session = await SeleniumSession.create(
            settings, headless=headless, user_agent=user_agent, locale=locale
        )
        try:
            if storage_path and base_url:
                await session.restore_state(storage_path, base_url)
            yield session
        finally:
            await session.close()


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Browser storage state requires an HTTP(S) origin")
    return f"{parsed.scheme}://{parsed.netloc}"
