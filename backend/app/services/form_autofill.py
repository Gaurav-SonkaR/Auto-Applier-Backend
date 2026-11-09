from __future__ import annotations

"""
Profile-driven form autofill for registration & application pages.

Fills what it legitimately can from the candidate's OWN master_profile (name,
email, phone, links, location, current role), then STOPS at any human-verification
gate — CAPTCHA, email verification, or a one-time code. It never attempts to solve
or evade those gates; it reports the gate as a JobStatus so the pipeline flags the
job for the user to complete the single manual step, then resume.

The matching logic (build_autofill_values / match_field / detect_gate) is pure and
unit-tested; the async autofill_form() wrapper is the only part that touches the
Selenium-backed page interface, so the rules remain testable without a browser.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Callable, Optional

from backend.app.models.job import JobStatus
from backend.app.services.job_fit import skill_years_answer
from backend.app.services.form_choices import (
    Option,
    choose_option,
    decline_option,
    is_placeholder,
    is_sensitive,
)

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

    preferences = profile.get("preferences") or {}
    locations = preferences.get("preferred_locations") or []
    preferred_locations = ", ".join(
        str(v).strip() for v in locations if str(v or "").strip()
    )

    return {
        # Answered from the profile because they are facts, not questions. An
        # LLM asked "are you willing to relocate?" answers it anyway — with
        # something the candidate never said.
        "total_experience_years": str(preferences.get("total_experience_years") or ""),
        "willing_to_relocate": str(preferences.get("willing_to_relocate") or ""),
        "preferred_locations": preferred_locations,
        "notice_period": str(preferences.get("notice_period") or ""),
        "current_ctc": str(preferences.get("current_ctc") or ""),
        "expected_ctc": str(preferences.get("expected_ctc") or ""),
        "needs_visa_sponsorship": str(preferences.get("needs_visa_sponsorship") or ""),
        "full_name": full_name,
        "first_name": first,
        "last_name": last,
        "email": personal.get("email", ""),
        "phone": personal.get("phone", ""),
        "linkedin": personal.get("linkedin", ""),
        "github": personal.get("github", ""),
        "kaggle": personal.get("kaggle", ""),
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
    ("kaggle", ("kaggle",)),
    # Preference questions, placed ahead of the generic rules below: "preferred
    # location" must not be swallowed by the plain "location" rule.
    ("total_experience_years", ("total experience", "years of experience",
                                "experience in years", "total work experience",
                                "yrs of experience")),
    ("willing_to_relocate", ("relocate", "relocation", "willing to move",
                             "open to relocation")),
    ("preferred_locations", ("preferred location", "desired location",
                             "location preference", "preferred city",
                             "where would you like to work")),
    ("notice_period", ("notice period", "when can you start",
                       "availability to start", "joining date", "earliest start")),
    ("current_ctc", ("current ctc", "current salary", "current compensation",
                     "present ctc", "current annual")),
    ("expected_ctc", ("expected ctc", "expected salary", "salary expectation",
                      "desired salary", "expected compensation")),
    ("needs_visa_sponsorship", ("sponsorship", "visa status", "work authorization",
                                "require a visa", "authorized to work")),
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
        if any(_mentions(hay, kw) for kw in keywords):
            return key
    return None


@lru_cache(maxsize=512)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Word-bounded matcher for one keyword, compiled once.

    Boundaries rather than containment: plain `"tel" in hay` matched "Tell us
    about a time...", so a behavioural question was treated as a phone field
    and filled with the applicant's number. The same trap sits under "city" in
    "capacity" and "name" inside any longer word.
    """
    return re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)")


def _mentions(haystack: str, keyword: str) -> bool:
    return _keyword_pattern(keyword).search(haystack) is not None


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


# ── Selenium page wrapper ──────────────────────────────────────────────────────

_SKIP_TYPES = frozenset(
    {"password", "hidden", "file", "submit", "button", "checkbox", "radio", "image", "reset"}
)


async def autofill_form(page, profile: dict,
                        owns_question: Optional[Callable[[str], bool]] = None) -> AutofillResult:
    """
    Fill every recognizable text field on the current page from `profile`, then
    check for a verification gate. Password fields are deliberately never touched
    here — credential entry stays in the dedicated login/register helpers.
    """
    values = build_autofill_values(profile)
    filled: list[str] = []
    autocompletes: list[str] = []
    # The question bank wins over the keyword rules for any question it holds:
    # "Are you serving your notice period?" contains "notice period" but wants
    # a yes or no, not "30 days".
    owns = owns_question or (lambda _label: False)
    vocabulary = _skill_vocabulary()

    elements = await page.find_all("input, textarea")
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

        question = ctx.label or ctx.placeholder
        if owns(question):
            continue
        years = skill_years_answer(question, profile, vocabulary)
        if years is not None:
            try:
                await el.fill(years)
                filled.append("skill_experience_years")
            except Exception as exc:
                logger.debug("Could not fill skill years: %s", exc)
            continue

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

    # Text inputs are not where most of these questions live. "Willing to
    # relocate?", notice period and experience brackets are almost always a
    # dropdown or a Yes/No radio pair, and before this nothing answered those
    # from the profile at all.
    filled += await _fill_dropdowns(page, values, profile, owns, vocabulary)
    filled += await _fill_radio_groups(page, values, owns)

    gate = Gate.NONE
    try:
        page_text = await page.text("body")
        gate = detect_gate(page_text, tuple(autocompletes))
    except Exception as exc:
        logger.debug("Gate detection skipped: %s", exc)

    if gate is not Gate.NONE:
        logger.info("Autofill stopped at %s gate — flagging for manual completion", gate.value)
    else:
        logger.info("Autofill filled %d field(s): %s", len(filled), ", ".join(filled) or "none")
    return AutofillResult(filled_fields=filled, gate=gate)


_OPTIONS_JS = (
    "return Array.from(arguments[0].options).map(o => "
    "({value: o.value, text: (o.textContent || '').trim()}));"
)

# Tags every radio with a stable handle, then reports each group with its
# question text. Tagging in the page and clicking by tag avoids rebuilding a
# CSS selector out of arbitrary portal-supplied names and values.
RADIO_GROUPS_JS = """
return (() => {
  const radios = [...document.querySelectorAll('input[type=radio]')]
    .filter(r => r.name && !r.disabled);
  const groups = new Map();
  for (const r of radios) {
    if (!groups.has(r.name)) groups.set(r.name, []);
    groups.get(r.name).push(r);
  }
  const text = (el) => (el ? (el.textContent || '').trim() : '');
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return text(l);
    }
    return text(el.closest('label')) || el.value || '';
  };
  const questionFor = (first) => {
    const fieldset = first.closest('fieldset');
    if (fieldset && fieldset.querySelector('legend')) return text(fieldset.querySelector('legend'));
    const group = first.closest('[role=radiogroup]');
    if (group) {
      const by = group.getAttribute('aria-labelledby');
      if (by && document.getElementById(by)) return text(document.getElementById(by));
      if (group.getAttribute('aria-label')) return group.getAttribute('aria-label');
    }
    return first.name;
  };
  const out = [];
  let g = 0;
  for (const [name, members] of groups) {
    out.push({
      name,
      question: questionFor(members[0]),
      answered: members.some(r => r.checked),
      options: members.map((r, i) => {
        const tag = `${g}:${i}`;
        r.setAttribute('data-autoapply-radio', tag);
        return {value: r.value, text: labelFor(r), tag};
      }),
    });
    g += 1;
  }
  return out;
})();
"""


def _skill_vocabulary() -> frozenset[str]:
    from backend.app.services.resume_library import skill_vocabulary

    try:
        return skill_vocabulary()
    except Exception:
        return frozenset()


_UNANSWERED_REQUIRED_JS = """
return (() => {
  const visible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const required = (e) => e.required || e.getAttribute('aria-required') === 'true';
  const labelOf = (e) => {
    if (e.id) {
      const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
      if (l) return (l.textContent || '').trim();
    }
    const fs = e.closest('fieldset');
    if (fs && fs.querySelector('legend')) return (fs.querySelector('legend').textContent || '').trim();
    const wrap = e.closest('label');
    return (e.getAttribute('aria-label') || (wrap && wrap.textContent) || e.name || '').trim();
  };
  const out = new Set();
  for (const e of document.querySelectorAll('input, select, textarea')) {
    if (!visible(e) || !required(e) || e.disabled) continue;
    const type = (e.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'file'].includes(type)) continue;
    if (type === 'radio' || type === 'checkbox') {
      const group = [...document.querySelectorAll(`input[name="${CSS.escape(e.name)}"]`)];
      if (!group.some(g => g.checked)) out.add(labelOf(e));
    } else if (!(e.value || '').trim()) {
      out.add(labelOf(e));
    }
  }
  return [...out].filter(Boolean);
})();
"""


async def unanswered_required(page) -> list[str]:
    """Labels of required, visible fields still empty on the current page.

    What a rehearsal reports, and what a human would have to fill before this
    application could be submitted for real.
    """
    try:
        return list(await page.execute_script(_UNANSWERED_REQUIRED_JS))
    except Exception as exc:
        logger.debug("Required-field scan skipped: %s", exc)
        return []


async def _fill_dropdowns(page, values: dict[str, str], profile: dict,
                          owns: Callable[[str], bool],
                          vocabulary: frozenset[str]) -> list[str]:
    """Answer recognised dropdowns from the profile; decline sensitive ones.

    Never picks an option it cannot justify. A dropdown already set to a real
    value (by the portal, from the account) is left as it is.
    """
    filled: list[str] = []
    for el in await page.find_all("select"):
        try:
            if not await el.is_visible():
                continue
            options = [Option(**o) for o in await el.execute_script(_OPTIONS_JS)]
            current = await el.execute_script("return arguments[0].value;")
            if any(o.value == current and not is_placeholder(o) for o in options):
                continue

            ctx = FieldContext(
                name=await el.get_attribute("name") or "",
                field_id=await el.get_attribute("id") or "",
                label=await _label_text(page, el),
            )
            question = ctx.label or ctx.name or ctx.field_id

            if is_sensitive(question):
                decline = decline_option(options)
                if decline:
                    await el.select(decline.value)
                    filled.append("declined_self_identification")
                continue
            if owns(question):
                continue

            years = skill_years_answer(question, profile, vocabulary)
            if years is not None:
                key = "total_experience_years"
                choice = choose_option(key, years, options)
            else:
                key = match_field(ctx)
                choice = choose_option(key, values.get(key, ""), options) if key else None
            if choice:
                await el.select(choice.value)
                filled.append(key)
        except Exception as exc:
            logger.debug("Dropdown skipped: %s", exc)
    return filled


async def _fill_radio_groups(page, values: dict[str, str],
                             owns: Callable[[str], bool]) -> list[str]:
    """Same rules as dropdowns, for Yes/No style radio groups."""
    filled: list[str] = []
    try:
        groups = await page.execute_script(RADIO_GROUPS_JS)
    except Exception as exc:
        logger.debug("Radio discovery skipped: %s", exc)
        return filled

    for group in groups:
        if group["answered"]:
            continue
        options = [Option(value=o["value"], text=o["text"]) for o in group["options"]]
        tags = {(o["value"], o["text"]): o["tag"] for o in group["options"]}
        question = group["question"] or ""

        if is_sensitive(question):
            choice, label = decline_option(options), "declined_self_identification"
        elif owns(question):
            continue
        else:
            key = match_field(FieldContext(name=group["name"], label=question))
            choice = choose_option(key, values.get(key, ""), options) if key else None
            label = key or ""

        if not choice:
            continue
        try:
            await page.check(f'[data-autoapply-radio="{tags[(choice.value, choice.text)]}"]')
            filled.append(label)
        except Exception as exc:
            logger.debug("Radio %s skipped: %s", group["name"], exc)
    return filled


async def _label_text(page, el) -> str:
    """The question a control is asking: <label for>, aria-label, or a wrapping
    <label>. Returns '' when the markup gives nothing to go on."""
    try:
        field_id = await el.get_attribute("id")
        if field_id:
            label = await page.find(f'label[for="{field_id}"]')
            if label:
                return (await label.text()).strip()
        aria = await el.get_attribute("aria-label")
        if aria:
            return aria.strip()
        return (await el.execute_script(
            "const l = arguments[0].closest('label'); return l ? (l.textContent || '').trim() : '';"
        )) or ""
    except Exception:
        return ""
