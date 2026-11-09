"""Safe, reusable Selenium cookie-consent handling.

Only accepts controls inside a cookie/consent/privacy banner or a clearly
cookie-labelled control. Generic actions such as "Accept" and "Continue" are
never clicked outside a consent context.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger("browser.cookies")

_COOKIE_MARKERS = (
    "cookie", "consent", "gdpr", "privacy", "onetrust", "cookiebot", "trustarc", "cmp"
)
_COOKIE_ROOTS = (
    "#onetrust-consent-sdk",
    "#onetrust-banner-sdk",
    "#CybotCookiebotDialog",
    "[id*='cookie' i]",
    "[class*='cookie' i]",
    "[id*='consent' i]",
    "[class*='consent' i]",
    "[id*='gdpr' i]",
    "[class*='gdpr' i]",
    "[id*='onetrust' i]",
    "[class*='onetrust' i]",
    "[id*='cookiebot' i]",
    "[class*='cookiebot' i]",
    "[id*='trustarc' i]",
    "[class*='trustarc' i]",
    "[id*='cmp' i]",
    "[class*='cmp' i]",
)
_BUTTONS = "button, [role='button'], input[type='button'], input[type='submit'], a"
_ACCEPT_LABELS = {
    "accept", "accept all", "accept all cookies", "accept cookies",
    "allow", "allow all", "allow all cookies", "allow cookies",
    "agree", "i agree", "i agree to all", "got it", "okay", "ok",
    "consent", "continue", "continue with cookies",
}
_NOT_COOKIE_ACTION = re.compile(
    r"\b(job|offer|application|invitation|terms|conditions|employment)\b", re.I
)


def normalize_label(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def is_cookie_action(label: str, *, cookie_context: bool) -> bool:
    """Whether text is an allowed consent action in a verified consent context."""
    normalized = normalize_label(label)
    if not normalized or _NOT_COOKIE_ACTION.search(normalized):
        return False
    if normalized not in _ACCEPT_LABELS:
        return False
    # Explicit cookie wording is strong evidence; generic actions require an
    # ancestor known to be a cookie/consent/privacy dialog.
    # Even explicit text needs a contextual ancestor: a page can have unrelated
    # "Accept cookies" text in a job description or FAQ.
    return cookie_context


def _element_label(element) -> str:
    return " ".join(filter(None, (
        element.text,
        element.get_attribute("aria-label"),
        element.get_attribute("title"),
        element.get_attribute("value"),
    ))).strip()


def _has_cookie_context(driver, element) -> bool:
    try:
        return bool(driver.execute_script(
            """
            let e = arguments[0];
            for (let i = 0; e && i < 8; i++, e = e.parentElement) {
              const marker = `${e.id || ''} ${e.className || ''} ${e.getAttribute?.('role') || ''}`.toLowerCase();
              if (/cookie|consent|gdpr|privacy|onetrust|cookiebot|trustarc|cmp/.test(marker)) return true;
              const text = (e.innerText || '').slice(0, 1500).toLowerCase();
              if (/cookie|cookies|consent management|privacy preferences/.test(text)) return true;
            }
            return false;
            """,
            element,
        ))
    except WebDriverException:
        return False


def _visible_consent_root(driver) -> bool:
    for selector in _COOKIE_ROOTS:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed():
                    return True
        except WebDriverException:
            continue
    return False


def _find_candidate(driver) -> Optional[Any]:
    for element in driver.find_elements(By.CSS_SELECTOR, _BUTTONS):
        try:
            if not element.is_displayed() or not element.is_enabled():
                continue
            label = _element_label(element)
            context = _has_cookie_context(driver, element)
            if is_cookie_action(label, cookie_context=context):
                return element
        except (StaleElementReferenceException, WebDriverException):
            continue
    return None


def _click_candidate(driver, element) -> bool:
    try:
        element.click()
    except ElementClickInterceptedException:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            WebDriverWait(driver, 2).until(EC.element_to_be_clickable(element))
            element.click()
        except ElementClickInterceptedException:
            # One controlled JS fallback, only after context and visibility
            # checks have already established this is a consent control.
            driver.execute_script("arguments[0].click();", element)
        except (TimeoutException, StaleElementReferenceException, WebDriverException):
            return False
    except (StaleElementReferenceException, WebDriverException):
        return False

    def _consent_action_cleared(_driver) -> bool:
        try:
            if not element.is_displayed():
                return not _visible_consent_root(_driver)
        except (StaleElementReferenceException, WebDriverException):
            return not _visible_consent_root(_driver)
        # This also handles open-shadow-root buttons, which are not returned by
        # the regular document scan: the consent host itself must disappear.
        return _find_candidate(_driver) is None and not _visible_consent_root(_driver)

    try:
        WebDriverWait(driver, 2).until(_consent_action_cleared)
        return True
    except TimeoutException:
        # Some sites retain a hidden shell after accepting. Check visible
        # consent actions rather than repeatedly clicking the same control.
        return _find_candidate(driver) is None


def _try_current_document(driver) -> bool:
    candidate = _find_candidate(driver)
    return bool(candidate is not None and _click_candidate(driver, candidate))


def handle_cookie_consent(driver) -> bool:
    """Accept one clearly identified cookie banner; return whether it cleared.

    Tries visible top-level controls, consent-related iframes, then consent
    hosts with open shadow roots. At most one click is attempted per document.
    The driver's default frame is restored even when a frame is stale.
    """
    try:
        if _try_current_document(driver):
            logger.info("Cookie consent accepted in main document")
            return True
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException) as exc:
        logger.debug("Cookie consent scan skipped in main document: %s", exc)

    frames = []
    try:
        frames = driver.find_elements(By.TAG_NAME, "iframe")
    except WebDriverException as exc:
        logger.debug("Cookie iframe scan unavailable: %s", exc)

    for frame in frames[:15]:
        try:
            frame_hint = " ".join(filter(None, (
                frame.get_attribute("id"), frame.get_attribute("name"),
                frame.get_attribute("title"), frame.get_attribute("src"),
            ))).lower()
            if not any(marker in frame_hint for marker in _COOKIE_MARKERS):
                continue
            driver.switch_to.frame(frame)
            if _try_current_document(driver):
                logger.info("Cookie consent accepted in a consent iframe")
                return True
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException) as exc:
            logger.debug("Cookie consent iframe skipped: %s", exc)
        finally:
            try:
                driver.switch_to.default_content()
            except WebDriverException:
                pass

    # Traverse only open shadow roots attached to a consent-labelled host.
    try:
        candidate = driver.execute_script(
            """
            const markers = /cookie|consent|gdpr|privacy|onetrust|cookiebot|trustarc|cmp/i;
            const accepted = /^(accept|accept all|accept all cookies|accept cookies|allow|allow all|allow all cookies|allow cookies|agree|i agree|i agree to all|got it|okay|ok|consent|continue|continue with cookies)$/i;
            for (const host of document.querySelectorAll('*')) {
              const marker = `${host.id || ''} ${host.className || ''}`;
              if (!host.shadowRoot || !markers.test(marker)) continue;
              for (const button of host.shadowRoot.querySelectorAll('button,[role=button],a,input[type=button],input[type=submit]')) {
                const label = (button.innerText || button.getAttribute('aria-label') || button.title || button.value || '').trim();
                if (accepted.test(label) && !/job|offer|application|invitation|terms/i.test(label) && button.getClientRects().length && !button.disabled) return button;
              }
            }
            return null;
            """
        )
        if candidate is not None:
            # Context is established by the consent marker on the shadow host.
            if _click_candidate(driver, candidate):
                logger.info("Cookie consent accepted in an open consent shadow root")
                return True
    except WebDriverException as exc:
        logger.debug("Cookie consent shadow-root scan unavailable: %s", exc)

    return False
