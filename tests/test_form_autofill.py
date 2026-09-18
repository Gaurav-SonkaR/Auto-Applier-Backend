"""Phase 5: profile-driven form autofill, now wired into the applier.

The matching and gate-detection logic is pure, so it is tested directly without
a browser. `autofill_form` (the Playwright wrapper) is exercised against a
minimal fake page.
"""
import pytest

from backend.app.models.job import JobStatus
from backend.app.services.form_autofill import (
    FieldContext,
    Gate,
    build_autofill_values,
    detect_gate,
    gate_to_status,
    match_field,
)

PROFILE = {
    "personal": {
        "name": "Gaurav Sonkar",
        "email": "gauravsonkar.bhu@gmail.com",
        "phone": "+91-9131930576",
        "linkedin": "linkedin.com/in/gauravsonkar953bhu",
        "github": "github.com/Gaurav-SonkaR",
        "kaggle": "kaggle.com/gauravsonkarbhu2025",
        "location": "India (Open to Relocation)",
    },
    "experience": [{"title": "Software Developer", "company": "Divyal Technology Pvt. Ltd."}],
}


class TestBuildAutofillValues:
    def test_splits_name(self):
        v = build_autofill_values(PROFILE)
        assert v["full_name"] == "Gaurav Sonkar"
        assert v["first_name"] == "Gaurav"
        assert v["last_name"] == "Sonkar"

    def test_pulls_contact_details(self):
        v = build_autofill_values(PROFILE)
        assert v["email"] == "gauravsonkar.bhu@gmail.com"
        assert v["phone"] == "+91-9131930576"

    def test_pulls_current_role_from_most_recent_experience(self):
        v = build_autofill_values(PROFILE)
        assert v["current_company"] == "Divyal Technology Pvt. Ltd."
        assert v["current_title"] == "Software Developer"

    def test_pulls_every_profile_link(self):
        """kaggle was in master_profile.json but had no autofill rule, so a form
        asking for it was left blank while linkedin and github were filled."""
        v = build_autofill_values(PROFILE)
        assert v["linkedin"] == "linkedin.com/in/gauravsonkar953bhu"
        assert v["github"] == "github.com/Gaurav-SonkaR"
        assert v["kaggle"] == "kaggle.com/gauravsonkarbhu2025"

    def test_empty_profile_does_not_raise(self):
        v = build_autofill_values({})
        assert v["full_name"] == ""
        assert v["email"] == ""
        assert v["kaggle"] == ""


class TestMatchField:
    @pytest.mark.parametrize("autocomplete,expected", [
        ("email", "email"),
        ("tel", "phone"),
        ("given-name", "first_name"),
        ("family-name", "last_name"),
        ("organization", "current_company"),
    ])
    def test_autocomplete_wins(self, autocomplete, expected):
        """The HTML autocomplete attribute is the most reliable signal and is
        checked before any fuzzy keyword matching."""
        assert match_field(FieldContext(autocomplete=autocomplete)) == expected

    @pytest.mark.parametrize("label,expected", [
        ("Email Address", "email"),
        ("Mobile Number", "phone"),
        ("First Name", "first_name"),
        ("Last Name", "last_name"),
        ("LinkedIn Profile", "linkedin"),
        ("Current Company", "current_company"),
        ("City", "location"),
        ("Full Name", "full_name"),
    ])
    def test_label_matching(self, label, expected):
        assert match_field(FieldContext(label=label)) == expected

    def test_specific_rules_beat_the_generic_name_rule(self):
        """'First Name' and 'Company Name' both contain 'name' — the generic
        full_name rule must not swallow them."""
        assert match_field(FieldContext(label="First Name")) == "first_name"
        assert match_field(FieldContext(label="Company Name")) == "current_company"

    @pytest.mark.parametrize("label", ["Kaggle", "kaggle profile", "Your Kaggle URL"])
    def test_matches_kaggle(self, label):
        assert match_field(FieldContext(label=label)) == "kaggle"

    def test_unknown_field_returns_none(self):
        assert match_field(FieldContext(label="Why do you want this job?")) is None

    def test_no_signals_returns_none(self):
        assert match_field(FieldContext()) is None


class TestGateDetection:
    """Gates are reported, never bypassed."""

    @pytest.mark.parametrize("text", [
        "Please complete the reCAPTCHA",
        "hcaptcha challenge",
        "cf-turnstile widget",
    ])
    def test_captcha(self, text):
        assert detect_gate(text) is Gate.CAPTCHA

    @pytest.mark.parametrize("text", [
        "Enter the verification code we sent",
        "Your security code is required",
    ])
    def test_otp(self, text):
        assert detect_gate(text) is Gate.OTP

    def test_otp_via_autocomplete_attribute(self):
        assert detect_gate("", ("one-time-code",)) is Gate.OTP

    def test_email_verification(self):
        assert detect_gate("Please verify your email to continue") is Gate.EMAIL_VERIFY

    def test_clean_page(self):
        assert detect_gate("Apply for Senior Python Engineer") is Gate.NONE

    def test_captcha_outranks_other_gates(self):
        """Order matters — CAPTCHA is the hardest block."""
        assert detect_gate("recaptcha and verify your email") is Gate.CAPTCHA


class TestGateToStatus:
    @pytest.mark.parametrize("gate,status", [
        (Gate.NONE, None),
        (Gate.CAPTCHA, JobStatus.WAITING_CAPTCHA),
        (Gate.OTP, JobStatus.WAITING_OTP),
        (Gate.EMAIL_VERIFY, JobStatus.NEED_HUMAN_ACTION),
    ])
    def test_maps_to_human_queue_statuses(self, gate, status):
        assert gate_to_status(gate) is status

    def test_every_mapped_status_reaches_the_human_queue(self):
        """A gate that maps to a status outside the human queue would strand
        the job — nothing would ever surface it for the user to complete."""
        from backend.app.routers.dashboard import _HUMAN_QUEUE_STATUSES

        for gate in (Gate.CAPTCHA, Gate.OTP, Gate.EMAIL_VERIFY):
            assert gate_to_status(gate) in _HUMAN_QUEUE_STATUSES


class FakeElement:
    def __init__(self, attrs=None, input_type="text"):
        self._attrs = {"type": input_type, **(attrs or {})}
        self.filled = None

    async def get_attribute(self, name):
        return self._attrs.get(name)

    async def fill(self, value):
        self.filled = value


class FakePage:
    def __init__(self, elements, body_text=""):
        self._elements = elements
        self._body = body_text

    async def query_selector_all(self, _selector):
        return self._elements

    async def query_selector(self, _selector):
        return None

    async def inner_text(self, _selector):
        return self._body


class TestAutofillForm:
    async def test_fills_recognised_fields(self):
        from backend.app.services.form_autofill import autofill_form

        email = FakeElement({"autocomplete": "email"})
        phone = FakeElement({"name": "phone"})
        page = FakePage([email, phone], body_text="Apply now")

        result = await autofill_form(page, PROFILE)

        assert email.filled == "gauravsonkar.bhu@gmail.com"
        assert phone.filled == "+91-9131930576"
        assert not result.needs_human

    @pytest.mark.parametrize("skip_type", ["password", "file", "hidden", "submit", "checkbox"])
    async def test_never_touches_sensitive_or_non_text_inputs(self, skip_type):
        """Password entry belongs to the dedicated login helper, not autofill."""
        from backend.app.services.form_autofill import autofill_form

        el = FakeElement({"name": "password"}, input_type=skip_type)
        await autofill_form(FakePage([el]), PROFILE)
        assert el.filled is None

    async def test_reports_gate_and_flags_for_human(self):
        from backend.app.services.form_autofill import autofill_form

        page = FakePage([FakeElement({"autocomplete": "email"})],
                        body_text="Please complete the recaptcha")
        result = await autofill_form(page, PROFILE)

        assert result.needs_human
        assert result.status is JobStatus.WAITING_CAPTCHA

    async def test_unknown_fields_are_left_alone(self):
        from backend.app.services.form_autofill import autofill_form

        el = FakeElement({"name": "why_this_role"})
        await autofill_form(FakePage([el]), PROFILE)
        assert el.filled is None
