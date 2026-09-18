"""Builds the cold email from the sender's own profile.

The body used to be a Python string literal with one person's name, phone,
LinkedIn and three achievement bullets baked into it. That was wrong twice
over: nobody else could use the feature, and every recipient got a
near-identical message, which is one of the strongest signals a spam filter
has.

Everything here therefore comes from the account's profile, and the wording
varies per company. What does *not* vary is the factual content — this rewrites
phrasing, never claims.
"""
from __future__ import annotations

import zlib
from typing import Any

# Phrase pools. The index is derived from the company name, so the same company
# always gets the same wording (re-sending after a bounce doesn't look like a
# different person wrote it) while a list of 50 companies does not receive 50
# byte-identical emails.
_OPENERS = (
    "I came across {company} and wanted to get in touch.",
    "I've been following {company} and wanted to reach out directly.",
    "{company} came up while I was looking at teams working on {tech_area}.",
    "I was reading about {company} and thought it was worth writing to you.",
)

_ASKS = (
    "Would you have 15 minutes for a quick call this week?",
    "If it's worth a conversation, I'd be glad to find 15 minutes that suit you.",
    "Happy to talk it through if you think there's a fit — 15 minutes would do it.",
    "Would a short call this week or next be useful?",
)

_BRIDGES = (
    "I'd love to explore whether there's a fit for a {role} role at {company}.",
    "I'm interested in {role} work, and {company} looks like a good place to do it.",
    "If you're hiring for {role}, I'd like to be considered.",
    "I'm reaching out about {role} opportunities at {company}.",
)

# Required by CAN-SPAM for commercial mail, and filters look for it. Kept as a
# plain sentence rather than a link because there is no unsubscribe endpoint to
# point at — a dead link would be worse than an honest instruction.
_OPT_OUT = "If you'd rather not hear from me, just reply \"no thanks\" and I won't write again."

MAX_BULLETS = 3


class ComposeError(ValueError):
    """The profile is missing something the email cannot be written without."""


def _pick(pool: tuple[str, ...], key: str) -> str:
    """Stable choice from `pool` for this key.

    crc32 rather than `hash()`: Python randomises string hashing per process, so
    `hash()` would give the same company different wording on every restart.
    """
    return pool[zlib.crc32(key.strip().lower().encode()) % len(pool)]


def sender_identity(profile: dict[str, Any]) -> dict[str, str]:
    """Name and contact lines, from the profile rather than a literal."""
    personal = profile.get("personal") or {}
    return {key: str(personal.get(key) or "").strip() for key in
            ("name", "email", "phone", "linkedin", "github", "location", "resume_url")}


def _current_role(profile: dict[str, Any]) -> dict[str, Any]:
    experience = profile.get("experience") or []
    return experience[0] if experience and isinstance(experience[0], dict) else {}


def credential_line(profile: dict[str, Any], tech_area: str) -> str:
    """One sentence saying who the sender is, built from their own history."""
    role = _current_role(profile)
    title = str(role.get("title") or "").strip()
    company = str(role.get("company") or "").strip()
    name = sender_identity(profile)["name"]

    who = f"I'm {name}" if name else "I'm writing"
    if title and company:
        who += f", a {title} at {company}"
    elif title:
        who += f", a {title}"

    return f"{who}, working mainly on {tech_area}." if tech_area else f"{who}."


def achievement_bullets(profile: dict[str, Any], limit: int = MAX_BULLETS) -> list[str]:
    """The sender's own bullets — most recent role first, then achievements.

    Falls back to achievements so a profile with no experience entries still
    produces a message with something in it.
    """
    bullets = [
        str(b).strip()
        for b in (_current_role(profile).get("bullets") or [])
        if str(b).strip()
    ]
    if len(bullets) < limit:
        bullets += [
            str(a).strip()
            for a in (profile.get("achievements") or [])
            if str(a).strip() and str(a).strip() not in bullets
        ]
    return bullets[:limit]


def signature(profile: dict[str, Any]) -> str:
    me = sender_identity(profile)
    lines = ["Best regards,", me["name"] or ""]

    contact = " | ".join(p for p in (me["phone"], me["email"]) if p)
    if contact:
        lines.append(contact)
    if me["linkedin"]:
        lines.append(f"LinkedIn: {me['linkedin']}")

    return "\n".join(line for line in lines if line)


def resume_sentence(profile: dict[str, Any], attached: bool) -> str:
    """How the resume is offered.

    A link is preferred: an attachment on a first contact to a stranger is the
    single loudest spam signal this email can carry. It is only possible when
    the sender has somewhere public to host it, so `resume_url` on the profile
    decides, and the attachment stays as the fallback.
    """
    url = sender_identity(profile)["resume_url"]
    if url:
        return f"My resume is here if it's useful: {url}"
    if attached:
        return "I've attached my resume."
    return ""


def compose(
    profile: dict[str, Any],
    *,
    company: str,
    first_name: str,
    role: str,
    tech_area: str,
    subject_template: str,
    resume_attached: bool = False,
) -> tuple[str, str]:
    """Return (subject, plain-text body) for one contact.

    Plain text only, deliberately — an HTML body from an unknown sender scores
    worse, and there is nothing here that needs formatting.
    """
    identity = sender_identity(profile)
    if not identity["name"]:
        raise ComposeError(
            "Your profile has no name — open Profile and fill it in before "
            "sending cold emails."
        )
    if not identity["email"]:
        raise ComposeError("Your profile has no email address to sign off with.")

    company = (company or "").strip() or "your team"
    role = (role or "").strip() or "Software Developer"

    subject = subject_template.format(role=role, company=company)

    paragraphs = [
        f"Hi {first_name or 'there'},",
        _pick(_OPENERS, company).format(company=company, tech_area=tech_area or "backend"),
        credential_line(profile, tech_area),
    ]

    bullets = achievement_bullets(profile)
    if bullets:
        paragraphs.append("\n".join(f"• {b}" for b in bullets))

    paragraphs.append(_pick(_BRIDGES, company).format(company=company, role=role))

    resume = resume_sentence(profile, resume_attached)
    if resume:
        paragraphs.append(resume)

    paragraphs.append(_pick(_ASKS, company))
    paragraphs.append(signature(profile))
    paragraphs.append(_OPT_OUT)

    return subject, "\n\n".join(paragraphs)


def tech_area_from_profile(
    profile: dict[str, Any], tech_stack: str, fallback: str = "backend systems"
) -> str:
    """Describe the sender's stack in the recipient's terms.

    Names only skills the sender actually lists, so the email cannot claim
    experience the profile does not contain — the previous hard-coded map would
    happily tell a Ruby shop the sender knew Rails.
    """
    owned = {
        str(s).strip(): str(s).strip()
        for s in (profile.get("primary_skills") or []) + (profile.get("secondary_skills") or [])
        if str(s).strip()
    }
    lowered = {k.lower(): v for k, v in owned.items()}

    wanted = _STACK_KEYWORDS.get((tech_stack or "").lower(), ())
    matched = [lowered[w] for w in wanted if w in lowered]

    if matched:
        return "/".join(matched[:3])
    # Nothing in common with that stack — describe what they do have instead of
    # inventing an overlap.
    top = [v for v in owned.values()][:3]
    return "/".join(top) if top else fallback


# Which of the sender's own skills to surface for a given classified stack.
_STACK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "python": ("python", "django", "fastapi", "flask", "django rest framework"),
    "java": ("java", "spring boot"),
    "nodejs": ("node.js", "express.js", "javascript (es6+)", "typescript"),
    "dotnet": (".net", "c#", "asp.net"),
    "php": ("php", "laravel"),
    "ruby": ("ruby", "rails"),
    "data": ("pandas", "scikit-learn", "pytorch", "sql", "power bi", "numpy"),
    "mobile": ("android", "react.js", "rest api design"),
    "fullstack": ("react.js", "django", "vue.js", "typescript", "python"),
    "other": ("python", "rest api design", "docker", "aws"),
}


def parsed_jd_skills(profile: dict[str, Any], tech_stack: str, limit: int = 8) -> list[str]:
    """Skills to tailor the cold-email resume around.

    Drawn from the sender's profile, ordered so anything matching the target
    stack comes first. The old version returned a fixed list per stack — which
    told the resume generator to emphasise skills the person might not have.
    """
    primary = [str(s).strip() for s in (profile.get("primary_skills") or []) if str(s).strip()]
    wanted = set(_STACK_KEYWORDS.get((tech_stack or "").lower(), ()))

    matching = [s for s in primary if s.lower() in wanted]
    rest = [s for s in primary if s.lower() not in wanted]
    return (matching + rest)[:limit]


def preview(profile: dict[str, Any], subject_template: str) -> dict[str, str]:
    """A sample of what recipients will get, for the UI to show before sending."""
    sample_company = "Acme Corp"
    tech_area = tech_area_from_profile(profile, "python")
    subject, body = compose(
        profile,
        company=sample_company,
        first_name="Priya",
        role="Backend Developer",
        tech_area=tech_area,
        subject_template=subject_template,
        resume_attached=not sender_identity(profile)["resume_url"],
    )
    return {"subject": subject, "body": body}


def missing_requirements(profile: dict[str, Any]) -> list[str]:
    """What the user still has to fill in before this feature works."""
    identity = sender_identity(profile)
    missing = []
    if not identity["name"]:
        missing.append("your name")
    if not identity["email"]:
        missing.append("your email address")
    if not achievement_bullets(profile):
        missing.append("at least one experience bullet or achievement")
    return missing


def spam_warnings(profile: dict[str, Any], attaching_resume: bool) -> list[str]:
    """Deliverability problems worth telling the user about before they send."""
    warnings: list[str] = []
    identity = sender_identity(profile)

    if attaching_resume and not identity["resume_url"]:
        warnings.append(
            "Attaching a PDF to a first email from a stranger is the strongest spam "
            "signal you can send. Add a public resume link to your profile and it "
            "will be linked instead."
        )
    if not identity["phone"]:
        warnings.append("No phone number in your profile — a signature without one reads as bulk mail.")
    if not identity["linkedin"]:
        warnings.append("No LinkedIn in your profile — recipients have no way to check who you are.")
    return warnings


def opt_out_sentence() -> str:
    """Exposed so the UI can show it verbatim rather than paraphrasing."""
    return _OPT_OUT
