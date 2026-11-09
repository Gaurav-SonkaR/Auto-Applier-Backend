"""Cookie-consent label and context safety tests (no external site/browser)."""
from selenium.webdriver.common.by import By

from backend.app.services.cookie_consent import (
    _BUTTONS,
    _COOKIE_ROOTS,
    handle_cookie_consent,
    is_cookie_action,
)


class FakeRoot:
    def __init__(self, driver):
        self.driver = driver

    def is_displayed(self):
        return self.driver.banner_visible


class FakeButton:
    def __init__(self, driver, label, *, context=True, frame=False):
        self.driver = driver
        self.text = label
        self.context = context
        self.frame = frame
        self.clicked = False
        self.visible = True

    def get_attribute(self, name):
        return {"aria-label": "", "title": "", "value": ""}.get(name)

    def is_displayed(self):
        return self.visible and self.driver.banner_visible and self.driver.in_frame == self.frame

    def is_enabled(self):
        return True

    def click(self):
        self.clicked = True
        self.driver.banner_visible = False


class FakeFrame:
    def get_attribute(self, name):
        return {"id": "onetrust-consent-iframe", "name": "", "title": "", "src": ""}.get(name)


class FakeSwitch:
    def __init__(self, driver):
        self.driver = driver

    def frame(self, _frame):
        self.driver.in_frame = True

    def default_content(self):
        self.driver.in_frame = False


class FakeDriver:
    def __init__(self, buttons, *, iframe=False):
        self.buttons = buttons
        self.banner_visible = True
        self.in_frame = False
        self.frames = [FakeFrame()] if iframe else []
        self.switch_to = FakeSwitch(self)

    def find_elements(self, by, selector):
        if by == By.TAG_NAME and selector == "iframe":
            return self.frames
        if selector == _BUTTONS:
            return [button for button in self.buttons if button.is_displayed()]
        if selector in _COOKIE_ROOTS:
            return [FakeRoot(self)] if self.banner_visible and (not self.frames or self.in_frame) else []
        return []

    def execute_script(self, script, *args):
        if "let e = arguments[0]" in script:
            return bool(args[0].context)
        return None


def test_common_consent_actions_are_case_insensitive():
    labels = [
        "ACCEPT", "accept all", "ACCEPT ALL COOKIES", "Allow All Cookies",
        "I agree", "OK", "Okay", "Got it", "Consent", "Continue",
    ]
    assert all(is_cookie_action(label, cookie_context=True) for label in labels)


def test_generic_accept_requires_cookie_context():
    assert is_cookie_action("Accept", cookie_context=True)
    assert not is_cookie_action("Accept", cookie_context=False)


def test_unrelated_accept_actions_are_never_accepted():
    for label in ("Accept Job", "Accept Offer", "Accept Application",
                  "Accept Invitation", "Accept Terms"):
        assert not is_cookie_action(label, cookie_context=True)


def test_accepts_one_visible_cookie_button_and_verifies_banner_cleared():
    driver = FakeDriver([FakeButton(None, "Accept All")])
    driver.buttons[0].driver = driver

    assert handle_cookie_consent(driver) is True
    assert driver.buttons[0].clicked is True
    assert driver.banner_visible is False


def test_skips_unrelated_visible_button_even_inside_consent_container():
    driver = FakeDriver([FakeButton(None, "Accept Application")])
    driver.buttons[0].driver = driver

    assert handle_cookie_consent(driver) is False
    assert driver.buttons[0].clicked is False


def test_no_banner_is_a_safe_noop():
    driver = FakeDriver([])
    driver.banner_visible = False

    assert handle_cookie_consent(driver) is False


def test_hidden_cookie_button_is_never_clicked():
    driver = FakeDriver([FakeButton(None, "Accept All")])
    driver.buttons[0].driver = driver
    driver.buttons[0].visible = False

    assert handle_cookie_consent(driver) is False
    assert driver.buttons[0].clicked is False


def test_multiple_consent_choices_click_only_one_candidate():
    driver = FakeDriver([
        FakeButton(None, "Accept All"),
        FakeButton(None, "Okay"),
    ])
    for button in driver.buttons:
        button.driver = driver

    assert handle_cookie_consent(driver) is True
    assert sum(button.clicked for button in driver.buttons) == 1


def test_handles_cookie_button_inside_consent_iframe_and_restores_frame():
    driver = FakeDriver([], iframe=True)
    button = FakeButton(driver, "I Agree", frame=True)
    driver.buttons.append(button)

    assert handle_cookie_consent(driver) is True
    assert button.clicked is True
    assert driver.in_frame is False
