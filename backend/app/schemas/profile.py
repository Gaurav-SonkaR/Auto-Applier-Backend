from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from backend.app.schemas.types import UtcDatetime


class ProfileResponse(BaseModel):
    """The account's master data, plus whether it is usable yet.

    `profile` is passed through as a document rather than a typed model: the
    resume prompt consumes the whole nested structure, and pinning every nested
    field here would mean editing two places to add one resume section. The
    service normalises and validates it instead.
    """

    profile: dict[str, Any]
    is_complete: bool = Field(
        description="Has enough (name, email, and skills or experience) to generate a resume"
    )
    updated_at: Optional[UtcDatetime] = None


class ProfileUpdateRequest(BaseModel):
    profile: dict[str, Any]


class BankQuestion(BaseModel):
    """One question in the account's question bank."""

    key: str
    question: str
    category: str
    mode: str = Field(description="fixed: typed as written. tailor: adapted to each company.")
    answer: str = ""
    source: str = Field(description="catalogue (unanswered) / user / generated")
    needs_review: bool = False
    used_count: int = 0
    qa_id: Optional[int] = None
    catalogue_id: Optional[str] = None
    hint: str = ""


class QuestionBankResponse(BaseModel):
    items: list[BankQuestion]
    categories: list[str]
    answered: int
    total: int
    needs_review: int


class SaveBankAnswerRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    answer: str = Field(default="", max_length=4000)
    mode: Optional[str] = Field(default=None, description="fixed / tailor")
    category: Optional[str] = Field(default=None, max_length=60)
