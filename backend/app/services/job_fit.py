"""Does this job fit the candidate at all? Decided before any resume is written.

The JD parser has always extracted `experience_years` and `required_skills`, and
nothing used them: every scraped posting went on to resume generation and, on
portals, to an application. A role asking for eight years got the same
treatment as one asking for one — tokens spent, a daily apply slot used, and a
prompt under pressure to make the candidate sound more senior than they are.

The rule here is conservative on purpose. A job is only skipped when the profile
gives clear grounds to skip it; when the profile cannot answer the question
(no dates, no skills) the job goes ahead rather than being silently dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
    start=1,
)}


@dataclass
class FitResult:
    fits: bool
    reasons: list[str] = field(default_factory=list)
    years_required: Optional[float] = None
    years_have: Optional[float] = None
    skill_overlap: Optional[float] = None
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return "; ".join(self.reasons)


# ── Candidate side ──────────────────────────────────────────────────────────

def _parse_month(text: str, today: date) -> Optional[date]:
    """"Jun 2025", "June 2025", "2025", "Present"."""
    norm = (text or "").strip().lower()
    if not norm:
        return None
    if norm in ("present", "current", "now", "till date", "ongoing"):
        return today
    year = re.search(r"\b(19|20)\d{2}\b", norm)
    if not year:
        return None
    month = next((n for name, n in _MONTHS.items() if name in norm), 1)
    return date(int(year.group(0)), month, 1)


def candidate_years(profile: dict[str, Any], today: Optional[date] = None) -> Optional[float]:
    """Years of experience the profile supports.

    The applicant's own stated figure wins. Otherwise it is measured from the
    earliest role's start to the latest role's end — a span, not a sum, so
    overlapping roles are not double-counted. None when neither is available.
    """
    today = today or date.today()

    stated = str((profile.get("preferences") or {}).get("total_experience_years") or "")
    number = re.search(r"\d+(?:\.\d+)?", stated)
    if number:
        return float(number.group(0))

    intervals: list[tuple[int, int]] = []
    for role in profile.get("experience") or []:
        if not isinstance(role, dict):
            continue
        start = _parse_month(str(role.get("start_date") or ""), today)
        raw_end = str(role.get("end_date") or "").strip()
        # A missing end date is ambiguous; only explicit present/current
        # markers indicate the role is ongoing.
        if not raw_end:
            continue
        end = _parse_month(raw_end, today)
        if not start or not end or end < start:
            continue

        start_month = start.year * 12 + start.month - 1
        end_month = end.year * 12 + end.month - 1
        end_exclusive = end_month
        if end_exclusive > start_month:
            intervals.append((start_month, end_exclusive))

    if not intervals:
        return None

    # Union employment intervals so overlapping roles aren't double-counted
    # and unrelated career gaps aren't treated as employment.
    intervals.sort()
    merged: list[list[int]] = []
    for start_month, end_month in intervals:
        if merged and start_month <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end_month)
        else:
            merged.append([start_month, end_month])
    months = sum(end - start for start, end in merged)
    return round(max(months, 0) / 12, 1)


def _skill_norm(skill: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", " ", (skill or "").lower()).strip()


def _skill_matches(required: str, owned: set[str]) -> bool:
    """Equal, or one names the other as whole words: "react" fits "react js",
    "rest api" fits "rest api design", but "java" does not fit "javascript"."""
    req = _skill_norm(required)
    if not req:
        return False
    for have in owned:
        if req == have:
            return True
        shorter, longer = sorted((req, have), key=len)
        if re.search(rf"(?<![a-z0-9]){re.escape(shorter)}(?![a-z0-9])", longer):
            return True
    return False


# ── The decision ────────────────────────────────────────────────────────────

def assess_fit(
    profile: dict[str, Any],
    parsed_jd: dict[str, Any],
    *,
    min_skill_overlap: float = 0.3,
    today: Optional[date] = None,
) -> FitResult:
    """Whether to spend a resume and an application on this job.

    Skips only on evidence: required experience clearly above what the profile
    shows (beyond a tolerance — postings routinely overstate), or too few of the
    required skills. Missing information on either side is not evidence.
    """
    result = FitResult(fits=True)

    raw_years = parsed_jd.get("experience_years")
    try:
        required_years = float(raw_years) if raw_years not in (None, "") else None
    except (TypeError, ValueError):
        required_years = None
    have_years = candidate_years(profile, today)
    result.years_required, result.years_have = required_years, have_years

    if required_years is not None and required_years > 0:
        if have_years is None:
            result.fits = False
            result.reasons.append(
                f"requires at least {required_years:g} years, but profile experience is unknown"
            )
        elif required_years > have_years:
            result.fits = False
            result.reasons.append(
                f"requires at least {required_years:g} years, profile shows {have_years:g}"
            )

    required_skills = [s for s in (parsed_jd.get("required_skills") or []) if str(s).strip()]
    owned = {
        _skill_norm(s)
        for s in (profile.get("primary_skills") or []) + (profile.get("secondary_skills") or [])
        if _skill_norm(str(s))
    }
    if required_skills and owned:
        matched = [s for s in required_skills if _skill_matches(str(s), owned)]
        result.matched_skills = matched
        result.missing_skills = [s for s in required_skills if s not in matched]
        result.skill_overlap = round(len(matched) / len(required_skills), 2)
        if result.skill_overlap < min_skill_overlap:
            result.fits = False
            result.reasons.append(
                f"matches {len(matched)} of {len(required_skills)} required skills "
                f"({result.skill_overlap:.0%}, minimum {min_skill_overlap:.0%})"
            )

    if result.fits:
        result.reasons.append("fits the profile")
    return result


# ── "How many years of experience do you have with X?" ──────────────────────

_SKILL_YEARS_RE = re.compile(
    r"years?\s+of\s+(?:work\s+|relevant\s+|professional\s+|hands[- ]on\s+)?"
    r"experience\s+(?:do\s+you\s+have\s+)?(?:with|in|using|on|working\s+with)\s+"
    r"(?P<skill>[^?.,;]+)",
    re.I,
)


def skill_years_answer(question: str, profile: dict[str, Any],
                       vocabulary: frozenset[str] = frozenset(),
                       today: Optional[date] = None) -> Optional[str]:
    """A whole-number answer for years with one named technology, or None.

    LinkedIn asks this per skill, and the generic "years of experience" rule
    used to answer every one of them with total experience — claiming years
    of Kafka for a profile that never mentions it.

    * a skill the profile lists: total experience, rounded down (secondary
      skills capped at one year — listed, but not the main stack);
    * a known technology the profile does not list: "0";
    * anything that isn't a recognised technology ("software development"):
      None, so the generic rules and the question bank handle it.
    """
    match = _SKILL_YEARS_RE.search(question or "")
    if not match:
        return None
    skill = _skill_norm(match.group("skill"))
    if not skill:
        return None

    primary = {_skill_norm(s) for s in profile.get("primary_skills") or [] if str(s).strip()}
    secondary = {_skill_norm(s) for s in profile.get("secondary_skills") or [] if str(s).strip()}
    known = {_skill_norm(v) for v in vocabulary} | primary | secondary

    if not _skill_matches(skill, known):
        return None
    years = candidate_years(profile, today)
    if years is None:
        return None
    if _skill_matches(skill, primary):
        return str(int(years))
    if _skill_matches(skill, secondary):
        return str(min(1, int(years)))
    return "0"
