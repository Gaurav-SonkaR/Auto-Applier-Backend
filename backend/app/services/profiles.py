"""Per-account master profile: the data every generated resume is built from.

Replaces the single `master_profile.json` that resume generation and form
autofill used to read. That file is still the seed for the original account (see
`seed_from_legacy_file`), but from here on each account owns its own copy —
otherwise every user's resume would carry the file owner's name and phone
number.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Session, func, select

from backend.app.models.user import User, UserRole
from backend.app.models.user_profile import UserProfile

logger = logging.getLogger("profiles")


class ProfileError(ValueError):
    """Profile input the caller should fix."""


# Guardrails on the LLM that rewrites the resume. These are app behaviour, not
# personal data, so every new account starts with the same set — they were
# previously hard-coded in master_profile.json.
DEFAULT_LLM_RULES: dict[str, Any] = {
    "locked_fields": [
        "personal.name", "personal.email", "personal.phone",
        "personal.linkedin", "personal.github", "personal.kaggle",
        "experience[*].company", "experience[*].client",
        "experience[*].title", "experience[*].start_date", "experience[*].end_date",
        "education.institution", "education.degree",
        "education.start_date", "education.end_date",
    ],
    "variable_fields": [
        "summary (rewrite per JD, max 70 words)",
        "primary_skills row order (reorder to match JD)",
        "experience[*].bullets (swap keywords, keep metrics)",
        "projects order (most relevant first)",
    ],
    "secondary_skill_rule": (
        "Secondary skills may only appear on resume if they appear in the JD's "
        "required_skills list."
    ),
    "content_limits": {
        "summary_max_words": 70,
        "summary_max_lines": 4,
        "experience_max_bullets_per_role": 5,
        "experience_bullet_max_words": 25,
        "project_max_bullets": 3,
        "project_bullet_max_words": 22,
        "max_projects_on_resume": 4,
        "achievements_max_lines": 3,
    },
}

# What portal application forms ask on every posting. Facts about the
# applicant, so they are answered from here rather than by an LLM — the same
# reason name and email are.
PREFERENCE_FIELDS = (
    "total_experience_years",
    "willing_to_relocate",
    "preferred_locations",
    "notice_period",
    "current_ctc",
    "expected_ctc",
    "needs_visa_sponsorship",
)

# The one preference that is a list, not a string.
_PREFERENCE_LIST_FIELDS = ("preferred_locations",)

PERSONAL_FIELDS = (
    "name", "email", "phone", "linkedin", "github", "kaggle", "location",
    # Somewhere public the resume can be read. Cold email links to this
    # instead of attaching a PDF, which is the loudest spam signal a first
    # email to a stranger can carry.
    "resume_url",
)

# Every key the rest of the app reads off a profile. Anything absent is filled
# in with an empty value of the right type, so consumers never have to guard.
_LIST_KEYS = ("primary_skills", "secondary_skills", "experience", "projects", "achievements")

# The whole profile is pasted into the resume prompt on every generation, so an
# oversized one costs tokens on every run rather than just disk. Generous — a
# long CV is well under 20k characters — but bounded.
MAX_PROFILE_CHARS = 200_000


def blank_profile() -> dict[str, Any]:
    """The shape a brand-new account starts from — correct, just empty."""
    return {
        "personal": {field: "" for field in PERSONAL_FIELDS},
        "preferences": {
            field: ([] if field in _PREFERENCE_LIST_FIELDS else "")
            for field in PREFERENCE_FIELDS
        },
        "primary_skills": [],
        "secondary_skills": [],
        "experience": [],
        "projects": [],
        "education": {
            "degree": "", "institution": "",
            "start_date": "", "end_date": "", "relevant_courses": [],
        },
        "achievements": [],
        "llm_rules": json.loads(json.dumps(DEFAULT_LLM_RULES)),  # deep copy
    }


def normalise(data: dict[str, Any]) -> dict[str, Any]:
    """Fill in missing keys and drop unknown top-level ones.

    Consumers (the resume prompt, form autofill) index into this document
    directly, so a profile saved with half the keys missing would surface as a
    KeyError mid-run rather than at the point of editing.
    """
    if not isinstance(data, dict):
        raise ProfileError("Profile must be a JSON object")

    out = blank_profile()

    personal = data.get("personal")
    if isinstance(personal, dict):
        # Keep only known fields, and only strings — a nested object here would
        # break the resume prompt.
        out["personal"].update({
            k: str(v or "").strip()
            for k, v in personal.items()
            if k in PERSONAL_FIELDS and not isinstance(v, (dict, list))
        })

    for key in _LIST_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            out[key] = value

    preferences = data.get("preferences")
    if isinstance(preferences, dict):
        for field, incoming in preferences.items():
            if field not in PREFERENCE_FIELDS:
                continue
            if field in _PREFERENCE_LIST_FIELDS:
                if isinstance(incoming, list):
                    out["preferences"][field] = [
                        str(v or "").strip() for v in incoming if str(v or "").strip()
                    ]
            elif not isinstance(incoming, (dict, list)):
                out["preferences"][field] = str(incoming or "").strip()

    education = data.get("education")
    if isinstance(education, dict):
        out["education"].update(education)

    rules = data.get("llm_rules")
    if isinstance(rules, dict):
        # Merged, not replaced: a client that omits llm_rules (the UI does not
        # edit them) must not wipe the guardrails.
        out["llm_rules"].update(rules)

    return out


def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise and reject what would break resume generation."""
    profile = normalise(data)

    for key in ("primary_skills", "secondary_skills", "achievements"):
        if any(not isinstance(item, str) for item in profile[key]):
            raise ProfileError(f"{key} must be a list of strings")

    for role in profile["experience"]:
        if not isinstance(role, dict):
            raise ProfileError("Each experience entry must be an object")
        if not str(role.get("title") or "").strip():
            raise ProfileError("Every experience entry needs a job title")

    for project in profile["projects"]:
        if not isinstance(project, dict):
            raise ProfileError("Each project must be an object")
        if not str(project.get("name") or "").strip():
            raise ProfileError("Every project needs a name")

    email = profile["personal"].get("email", "")
    if email and "@" not in email:
        raise ProfileError("Email address looks wrong")

    return profile


# ── Storage ─────────────────────────────────────────────────────────────────

def get_profile(session: Session, user_id: Optional[int]) -> dict[str, Any]:
    """This account's profile, or an empty one it can start filling in.

    A None user_id means the caller has an unsaved or ownerless row; return the
    blank profile rather than letting SQLAlchemy issue a NULL-key lookup.
    """
    if user_id is None:
        return blank_profile()
    row = session.get(UserProfile, user_id)
    if row is None:
        return blank_profile()
    try:
        return normalise(json.loads(row.data))
    except (json.JSONDecodeError, ProfileError) as exc:
        # Never sink a run over a corrupt row — degrade to blank and say so.
        logger.error("Profile for user %s is unreadable (%s) — using a blank one",
                     user_id, exc)
        return blank_profile()


def save_profile(session: Session, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    profile = validate(data)

    serialised = json.dumps(profile, ensure_ascii=False)
    if len(serialised) > MAX_PROFILE_CHARS:
        raise ProfileError(
            f"Profile is too large ({len(serialised):,} characters, limit "
            f"{MAX_PROFILE_CHARS:,}). Trim the longest bullets."
        )

    row = session.get(UserProfile, user_id)
    if row is None:
        row = UserProfile(user_id=user_id)
    row.data = serialised
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    logger.info("Profile saved for user %s", user_id)
    return profile


def is_complete(profile: dict[str, Any]) -> bool:
    """Enough to generate a resume that isn't obviously broken."""
    personal = profile.get("personal") or {}
    return bool(
        str(personal.get("name") or "").strip()
        and str(personal.get("email") or "").strip()
        and (profile.get("primary_skills") or profile.get("experience"))
    )


def seed_from_legacy_file(session: Session, path: str) -> Optional[int]:
    """One-time import of `master_profile.json` into the first admin's profile.

    Runs at startup. Only fires when no account has a profile yet, so it can't
    overwrite anything a user has edited, and can't hand one person's details to
    a second account created later — those start blank.

    Returns the user_id seeded, or None if there was nothing to do.
    """
    from backend.app.utils.profile import try_load_master_profile

    existing = session.exec(select(func.count()).select_from(UserProfile)).one()
    if existing:
        return None

    admin = session.exec(
        select(User).where(User.role == UserRole.ADMIN).order_by(User.user_id)
    ).first()
    if admin is None:
        return None  # nothing to attach it to yet; try again next startup

    # The same reader the file has always had: missing or malformed degrades to
    # None with a logged warning rather than blocking startup.
    data = try_load_master_profile(path)
    if data is None:
        return None

    save_profile(session, admin.user_id, data)
    logger.info("Imported %s into the profile of admin '%s'", path, admin.username)
    return admin.user_id
