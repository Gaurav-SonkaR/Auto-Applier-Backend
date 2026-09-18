from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParsedJD:
    """Structured output from the JD Parser Agent."""
    required_skills: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    experience_years: Optional[int] = None
    role_level: str = "mid"  # junior / mid / senior / lead
    raw: dict = field(default_factory=dict)

    def all_keywords(self) -> list[str]:
        return list({k.lower() for k in self.required_skills + self.nice_to_have + self.keywords})


@dataclass
class ApplyResult:
    """Result returned by the Apply Agent for a single job."""
    job_id: int
    status: str          # APPLIED / FAILED / FLAGGED
    reason: str = ""
    screenshot_path: Optional[str] = None
    redirect_url: Optional[str] = None
