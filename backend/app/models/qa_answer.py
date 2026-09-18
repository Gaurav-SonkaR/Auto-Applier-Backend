from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


class QaAnswer(SQLModel, table=True):
    """One answer in an account's question bank.

    Either written by the user on the Profile page (`source="user"`), or drafted
    by a model during an application and waiting for the user to check it
    (`source="generated"`, `needs_review=True`). Questions from the catalogue are
    keyed by the catalogue's own wording, so every alias shares one answer.

    Per account: the store this replaced had no `user_id`, and served one
    account's answers into another's application forms.
    """

    __tablename__ = "qa_answer"

    qa_id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.user_id", index=True)
    question_key: str = Field(index=True, description="Normalised question — the lookup key")
    question: str = Field(description="The question as shown to the user")
    answer: str
    # fixed: typed as written. tailor: adapted to each company without new claims.
    mode: str = Field(default="fixed")
    category: str = Field(default="Other")
    source: str = Field(default="user")
    needs_review: bool = Field(default=False)
    portal: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    used_count: int = Field(default=0, description="How often this answer has been used on a form")
