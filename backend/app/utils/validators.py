"""Input validation and LaTeX sanitisation (SRS SEC-002, SEC-003).

Kept free of FastAPI and DB imports so every rule here is unit-testable on its
own. Pydantic schemas enforce shape and length at the API boundary; this module
holds the rules that are shared by more than one caller, or that need to run on
data which never passes through a schema — most importantly LLM output, which
is derived from scraped job descriptions and is therefore untrusted.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# ── SEC-002: input validation ────────────────────────────────────────────────

# Generous cap. Long enough for any real search term, short enough that a
# pathological input can't be used to blow up a LIKE query or a log line.
MAX_SEARCH_TERM_LEN = 200
MAX_URL_LEN = 2048

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


class ValidationError(ValueError):
    """Raised when user-supplied input fails a rule in this module."""


def validate_url(url: str) -> str:
    """Return `url` if it is a well-formed http(s) URL, else raise.

    Rejecting non-http schemes matters because these URLs are later handed to
    browser navigation, which could otherwise open `file://` and read local
    files into the scraped job description.
    """
    if not url or not url.strip():
        raise ValidationError("URL must not be empty")
    url = url.strip()
    if len(url) > MAX_URL_LEN:
        raise ValidationError(f"URL exceeds {MAX_URL_LEN} characters")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        raise ValidationError(
            f"URL scheme '{parsed.scheme}' not allowed — must be http or https"
        )
    if not parsed.netloc:
        raise ValidationError("URL has no host")
    return url


def validate_search_term(term: str, field: str = "search term", required: bool = True) -> str:
    """Trim and length-check a free-text search input (job title, location)."""
    term = (term or "").strip()
    if not term:
        if required:
            raise ValidationError(f"{field} must not be empty")
        return ""
    if len(term) > MAX_SEARCH_TERM_LEN:
        raise ValidationError(f"{field} exceeds {MAX_SEARCH_TERM_LEN} characters")
    return term


# ── SEC-003: LaTeX command denylist ──────────────────────────────────────────

# LaTeX commands with file-I/O or macro-programming power. If a scraped job
# description prompt-injects the LLM into echoing one of these into generated
# resume content, pdflatex executes it at compile time — \input and \include
# read arbitrary local files into the resulting PDF with no -shell-escape
# needed, so -no-shell-escape alone is not sufficient protection.
#
# Neutralised rather than stripped, so the surrounding sentence still renders
# and the tampering is visible in the output instead of silently vanishing.
DANGEROUS_LATEX_COMMANDS = re.compile(
    r"\\(input|include|write(?:18)?|immediate|openout|closeout"
    r"|catcode|csname|endcsname|read|directlua|InputIfFileExists"
    r"|usepackage|def|let|expandafter|noexpand)\b",
    re.IGNORECASE,
)

# Characters that break compilation if they reach LaTeX unescaped. Backslash
# and braces are deliberately absent: generated text may legitimately contain
# \textbf{...}, and the denylist above already defangs the dangerous commands.
_LATEX_ESCAPES = (
    ("&", r"\&"),
    ("%", r"\%"),
    ("#", r"\#"),
    ("$", r"\$"),
    ("_", r"\_"),
    ("^", r"\^{}"),
    ("~", r"\textasciitilde{}"),
)


def neutralize_latex_commands(text: str) -> str:
    """Defang file-I/O and macro commands by escaping their leading backslash."""
    return DANGEROUS_LATEX_COMMANDS.sub(
        lambda m: m.group(0).replace("\\", r"\textbackslash{}"), text
    )


def escape_latex(text: str) -> str:
    """Make LLM-generated text safe to inject into the resume template.

    Order matters: neutralise dangerous commands *before* escaping, so that a
    `\\write18` becomes `\\textbackslash{}write18` rather than having its
    backslash survive into the compiled document.
    """
    text = neutralize_latex_commands(text)
    for char, escaped in _LATEX_ESCAPES:
        text = text.replace(char, escaped)
    return text
