from __future__ import annotations

"""
Profile-driven form autofill for registration & application pages.

Fills what it legitimately can from the candidate's OWN master_profile (name,
email, phone, links, location, current role), then STOPS at any human-verification
gate — CAPTCHA, email verification, or a one-time code. It never attempts to solve
or evade those gates; it reports the gate as a JobStatus so the pipeline flags the
job for the user to complete the single manual step, then resume.

The matching logic (build_autofill_values / match_field / detect_gate) is pure and
unit-tested; the async autofill_form() wrapper is the only part that touches
Playwright, so the rules can be validated without a browser.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum

from backend.models.job import JobStatus

logger = logging.getLogger("agent.autofill")


class Gate(str, Enum):
    """A human-only step the automation must not attempt to pass on its own."""
    NONE = "none"
    CAPTCHA = "captcha"
    OTP = "otp"
    EMAIL_VERIFY = "email_verify"


_GATE_STATUS: dict[Gate, JobStatus | None] = {
    Gate.NONE: None,
    Gate.CAPTCHA: JobStatus.WAITING_CAPTCHA,
    Gate.OTP: JobStatus.WAITING_OTP,
    Gate.EMAIL_VERIFY: JobStatus.NEED_HUMAN_ACTION,
}


def gate_to_status(gate: Gate) -> JobStatus | None:
    return _GATE_STATUS[gate]


@dataclass
class FieldContext:
    """Signals scraped from a single form input used to guess what it wants."""
    name: str = ""
    field_id: str = ""
    placeholder: str = ""
    label: str = ""
    autocomplete: str = ""
    input_type: str = ""

    def haystack(self) -> str:
        return " ".join(
            s for s in (self.name, self.field_id, self.placeholder, self.label)
            if s
        ).lower()


@dataclass
class AutofillResult:
    filled_fields: list[str] = field(default_factory=list)
    gate: Gate = Gate.NONE

    @property
    def status(self) -> JobStatus | None:
        return gate_to_status(self.gate)

    @property
    def needs_human(self) -> bool:
        return self.gate is not Gate.NONE


# ── Profile → canonical autofill values ────────────────────────────────────────

def build_autofill_values(profile: dict) -> dict[str, str]:
    """Flatten the parts of master_profile a generic web form asks for."""
    personal = profile.get("personal") or {}
    full_name = (personal.get("name") or "").strip()
    parts = full_name.split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""

    experience = profile.get("experience") or []
    current = experience[0] if experience else {}

    return {
        "full_name": full_name,
        "first_name": first,
        "last_name": last,
        "email": personal.get("email", ""),
        "phone": personal.get("phone", ""),
        "linkedin": personal.get("linkedin", ""),
        "github": personal.get("github", ""),
        "location": personal.get("location", ""),
        "current_company": current.get("company", ""),
        "current_title": current.get("title", ""),
    }


# ── Field matching ─────────────────────────────────────────────────────────────

# The autocomplete attribute (HTML standard) is the most reliable signal; check it
# before falling back to fuzzy keyword matching on name/id/placeholder/label.
_AUTOCOMPLETE_MAP: dict[str, str] = {
    "email": "email",
    "tel": "phone",
    "given-name": "first_name",
    "family-name": "last_name",
    "name": "full_name",
    "url": "linkedin",
    "organization": "current_company",
    "organization-title": "current_title",
    "address-level2": "location",
}

# Ordered most-specific first: first_name/last_name/company must beat the generic
# "name" rule, which would otherwise swallow "first name" / "company name".
_MATCH_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("email", ("email", "e-mail", "mail")),
    ("phone", ("phone", "mobile", "tel", "contact number", "contactnumber")),
    ("first_name", ("first name", "firstname", "first_name", "given name", "given-name", "fname")),
    ("last_name", ("last name", "lastname", "last_name", "surname", "family name", "family-name", "lname")),
    ("linkedin", ("linkedin",)),
    ("github", ("github",)),
    ("current_company", ("current company", "employer", "company name", "organization")),
    ("current_title", ("current title", "job title", "current role", "designation")),
    ("location", ("city", "location", "town")),
    ("full_name", ("full name", "your name", "name")),  # generic fallback — must stay last
]


def match_field(ctx: FieldContext) -> str | None:
    """Return the canonical profile key this field is asking for, or None."""
    ac = ctx.autocomplete.strip().lower()
    if ac in _AUTOCOMPLETE_MAP:
        return _AUTOCOMPLETE_MAP[ac]

    hay = ctx.haystack()
    if not hay:
        return None
    for key, keywords in _MATCH_RULES:
        if any(kw in hay for kw in keywords):
            return key
    return None


# ── Verification-gate detection (never bypassed — only reported) ───────────────

_CAPTCHA_MARKERS = ("recaptcha", "hcaptcha", "captcha", "cf-turnstile", "turnstile")
_OTP_MARKERS = ("one-time-code", "one time code", " otp", "otp ", "verification code", "security code")
_EMAIL_VERIFY_MARKERS = (
    "verify your email", "confirm your email", "verify email", "check your inbox",
    "we sent you", "we've sent", "verification link", "confirm your account",
)


def detect_gate(page_text: str, autocomplete_values: tuple[str, ...] = ()) -> Gate:
    """
    Classify whether the page is presenting a human-only verification step.

    Order matters: CAPTCHA is the hardest block, then OTP, then a plain
    "check your email" prompt.
    """
    t = (page_text or "").lower()
    if any(m in t for m in _CAPTCHA_MARKERS):
        return Gate.CAPTCHA
    if "one-time-code" in autocomplete_values or any(m in t for m in _OTP_MARKERS):
        return Gate.OTP
    if any(m in t for m in _EMAIL_VERIFY_MARKERS):
        return Gate.EMAIL_VERIFY
    return Gate.NONE


# ── Playwright wrapper ─────────────────────────────────────────────────────────

_SKIP_TYPES = frozenset(
    {"password", "hidden", "file", "submit", "button", "checkbox", "radio", "image", "reset"}
)


async def autofill_form(page, profile: dict) -> AutofillResult:
    """
    Fill every recognizable text field on the current page from `profile`, then
    check for a verification gate. Password fields are deliberately never touched
    here — credential entry stays in the dedicated login/register helpers.
    """
    values = build_autofill_values(profile)
    filled: list[str] = []
    autocompletes: list[str] = []

    elements = await page.query_selector_all("input, textarea")
    for el in elements:
        input_type = (await el.get_attribute("type") or "text").lower()
        if input_type in _SKIP_TYPES:
            continue

        ctx = FieldContext(
            name=await el.get_attribute("name") or "",
            field_id=await el.get_attribute("id") or "",
            placeholder=await el.get_attribute("placeholder") or "",
            autocomplete=await el.get_attribute("autocomplete") or "",
            input_type=input_type,
            label=await _label_text(page, el),
        )
        autocompletes.append(ctx.autocomplete.strip().lower())

        key = match_field(ctx)
        if not key:
            continue
        value = values.get(key, "")
        if not value:
            continue
        try:
            await el.fill(value)
            filled.append(key)
        except Exception as exc:  # a hidden/disabled field that slipped the type filter
            logger.debug("Could not fill %s: %s", key, exc)

    gate = Gate.NONE
    try:
        page_text = await page.inner_text("body")
        gate = detect_gate(page_text, tuple(autocompletes))
    except Exception as exc:
        logger.debug("Gate detection skipped: %s", exc)

    if gate is not Gate.NONE:
        logger.info("Autofill stopped at %s gate — flagging for manual completion", gate.value)
    else:
        logger.info("Autofill filled %d field(s): %s", len(filled), ", ".join(filled) or "none")
    return AutofillResult(filled_fields=filled, gate=gate)


async def _label_text(page, el) -> str:
    """Best-effort <label for=id> lookup; returns '' if none."""
    field_id = await el.get_attribute("id")
    if not field_id:
        return ""
    try:
        label = await page.query_selector(f'label[for="{field_id}"]')
        if label:
            return (await label.inner_text()) or ""
    except Exception:
        pass
    return ""
