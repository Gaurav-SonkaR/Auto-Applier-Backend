"""Choosing an option in a dropdown or radio group — without guessing.

The applier used to select the first non-placeholder option of every dropdown
it could see. On a real application that means "Willing to relocate?" answered
by whatever the portal listed first, the lowest salary bracket, the
alphabetically first country — and on equal-opportunity questions (gender,
veteran status, disability) a false self-declaration submitted to an employer.

Everything here is pure, so the decisions can be tested without a browser:

* a recognised field is matched against the profile value, and **no confident
  match means no selection** — a required field left blank stops the form from
  submitting and flags the job for a human, whereas a guessed answer is
  submitted as the applicant's own;
* self-identification questions are never answered from any source — the only
  permitted choice is an explicit "prefer not to say".
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Optional

_EPS = 1e-9


@dataclass(frozen=True)
class Option:
    value: str
    text: str


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9+]+", " ", (text or "").lower()).strip()


def _mentions(haystack: str, needle: str) -> bool:
    """Word-bounded containment, so "Pune" is not found inside "Punekar"."""
    needle = _norm(needle)
    return bool(needle) and re.search(
        rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", _norm(haystack)
    ) is not None


# ── Placeholders ────────────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(
    r"^(?:-+|-*\s*(?:select|choose|please select|please choose|pick one|"
    r"none selected)\b.*)$",
    re.I,
)


def is_placeholder(option: Option) -> bool:
    """The "Select…" entry a dropdown starts with. Not an answer."""
    text = option.text.strip()
    return not text or bool(_PLACEHOLDER_RE.match(text))


def real_options(options: Iterable[Option]) -> list[Option]:
    return [o for o in options if not is_placeholder(o)]


# ── Self-identification: never answered ─────────────────────────────────────

_SENSITIVE_RE = re.compile(
    r"\b(?:gender|sex|race|racial|ethnic\w*|hispanic|latin[ao]|veteran|"
    r"disabilit\w*|disabled|pronouns?|sexual|orientation|religio\w*|marital|"
    r"caste|date of birth|birth date|age range|nationality)\b",
    re.I,
)
_DECLINE_RE = re.compile(
    r"prefer not|decline|do not wish|don'?t wish|not to (?:say|disclose|answer|identify)|"
    r"rather not|choose not|don'?t want to answer",
    re.I,
)


def is_sensitive(question: str) -> bool:
    """Equal-opportunity and personal-status questions.

    Nothing in the profile answers these, and an automated answer is a
    declaration made on the applicant's behalf. The only acceptable automated
    action is to decline.
    """
    return bool(_SENSITIVE_RE.search(question or ""))


def decline_option(options: Iterable[Option]) -> Optional[Option]:
    matches = [o for o in real_options(options) if _DECLINE_RE.search(o.text)]
    return matches[0] if len(matches) == 1 else None


# ── Yes / No ────────────────────────────────────────────────────────────────

YES_NO_KEYS = frozenset({"willing_to_relocate", "needs_visa_sponsorship"})

_YES_WORDS = {"yes", "y", "true", "willing"}
_NO_WORDS = {"no", "n", "false", "not", "unwilling"}


def _yes_no(text: str) -> Optional[bool]:
    words = _norm(text).split()
    if not words:
        return None
    if words[0] in _YES_WORDS:
        return True
    if words[0] in _NO_WORDS:
        return False
    return None


# ── Numeric brackets ────────────────────────────────────────────────────────

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_BETWEEN_RE = re.compile(r"\d[\sa-z.]*?(?:-|–|\bto\b)\s*\d", re.I)

_DAY_UNITS = {"day": 1.0, "week": 7.0, "month": 30.0, "year": 365.0}
_YEAR_UNITS = {"month": 1 / 12, "year": 1.0, "yr": 1.0}


def _interval(text: str, units: dict[str, float]) -> Optional[tuple[float, float]]:
    """The numeric interval a piece of text describes, in the first unit's terms.

    Covers what portals put in dropdowns: "2-4 years", "5+ years", "more than
    3 months", "15 days or less", "less than 1 year", "Immediate", "Fresher".
    "1 month" and "30 days" come out equal.
    """
    norm = (text or "").lower()
    if re.search(r"\b(?:immediate(?:ly)?|fresher|no experience)\b", norm):
        return (0.0, 0.0)

    scale = 1.0
    for unit, factor in units.items():
        if re.search(rf"\b{unit}", norm):
            scale = factor
            break

    numbers = [float(n) * scale for n in _NUMBER_RE.findall(norm)]
    if not numbers:
        return None

    if len(numbers) >= 2 and _BETWEEN_RE.search(norm):
        low, high = sorted(numbers[:2])
        return (low, high)

    n = numbers[0]
    if re.search(r"\+|at least|or more", norm):
        return (n, math.inf)
    if re.search(r"more than|over|above", norm):
        return (n + _EPS, math.inf)
    if re.search(r"up to|or less|within", norm):
        return (0.0, n)
    if re.search(r"less than|under|below", norm):
        return (0.0, n - _EPS)
    return (n, n)


def _tightest_bracket(value: str, options: list[Option], units: dict[str, float]
                      ) -> Optional[Option]:
    wanted = _interval(value, units)
    if wanted is None:
        return None
    point = wanted[0]

    containing = sorted(
        ((span[1] - span[0], option)
         for option in options
         if (span := _interval(option.text, units)) and span[0] <= point <= span[1]),
        key=lambda pair: pair[0],
    )
    if not containing:
        return None
    # "2 years" belongs in "2-4", not in "0+". Two equally tight brackets means
    # the dropdown overlaps itself, and picking one would be a guess.
    if len(containing) > 1 and containing[0][0] == containing[1][0]:
        return None
    return containing[0][1]


_CTC_UNITS = (
    ("crore", re.compile(r"\b(?:cr|crore|crores)\b", re.I)),
    ("lakh", re.compile(r"\b(?:lpa|lakh|lakhs|lac|lacs)\b|\d\s*l\b", re.I)),
    ("thousand", re.compile(r"\d\s*k\b|\bthousand\b", re.I)),
)


def _ctc_unit(text: str) -> str:
    for name, pattern in _CTC_UNITS:
        if pattern.search(text or ""):
            return name
    return "unspecified"


# ── The decision ────────────────────────────────────────────────────────────

def choose_option(key: str, value: str, options: Iterable[Option]) -> Optional[Option]:
    """The option that says what the profile says, or None.

    None is a deliberate outcome, not a failure — see the module docstring.
    """
    candidates = real_options(options)
    value = (value or "").strip()
    if not value or not candidates:
        return None

    if key in YES_NO_KEYS:
        wanted = _yes_no(value)
        if wanted is None:
            return None
        matches = [o for o in candidates if _yes_no(o.text) is wanted]
        return matches[0] if len(matches) == 1 else None

    if key == "notice_period":
        return _tightest_bracket(value, candidates, _DAY_UNITS)

    if key == "total_experience_years":
        return _tightest_bracket(value, candidates, _YEAR_UNITS)

    if key in ("current_ctc", "expected_ctc"):
        # "20 LPA" must never land in a "$20k" bracket.
        unit = _ctc_unit(value)
        comparable = [o for o in candidates if _ctc_unit(o.text) == unit]
        return _tightest_bracket(value, comparable, {}) if comparable else None

    if key == "preferred_locations":
        # In the applicant's own order: their first preference wins.
        for place in (p.strip() for p in value.split(",") if p.strip()):
            matches = [o for o in candidates if _mentions(o.text, place)]
            if len(matches) == 1:
                return matches[0]
        return None

    exact = [o for o in candidates if _norm(o.text) == _norm(value)]
    if len(exact) == 1:
        return exact[0]
    contains = [o for o in candidates if _mentions(o.text, value)]
    return contains[0] if len(contains) == 1 else None


def resolve_llm_choice(reply: str, options: Iterable[Option]) -> Optional[Option]:
    """Accept a model's pick only if it names exactly one listed option.

    A paraphrase, two options, "UNKNOWN" or an explanation all count as no
    answer. Fuzzy-matching the reply to the nearest option would reintroduce
    exactly the guessing this module exists to remove.
    """
    reply_norm = _norm((reply or "").strip().strip("\"'").rstrip("."))
    if not reply_norm or reply_norm == "unknown":
        return None
    matches = [o for o in real_options(options) if _norm(o.text) == reply_norm]
    return matches[0] if len(matches) == 1 else None


def choose_from_answer(answer: str, options: Iterable[Option]) -> Optional[Option]:
    """The option a written answer points to, for a question-bank dropdown.

    "Yes, hybrid preferred" picks "Yes" from a Yes/No list; "Referral" picks
    "Referral". Anything less clear-cut selects nothing.
    """
    candidates = real_options(options)
    answer = (answer or "").strip()
    if not answer or not candidates:
        return None

    stated = _yes_no(answer)
    if stated is not None and all(_yes_no(o.text) is not None for o in candidates):
        matches = [o for o in candidates if _yes_no(o.text) is stated]
        return matches[0] if len(matches) == 1 else None

    exact = [o for o in candidates if _norm(o.text) == _norm(answer)]
    if len(exact) == 1:
        return exact[0]
    mentioned = [o for o in candidates if _mentions(answer, o.text)]
    return mentioned[0] if len(mentioned) == 1 else None
