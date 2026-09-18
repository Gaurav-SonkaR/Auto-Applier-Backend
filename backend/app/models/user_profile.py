from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


class UserProfile(SQLModel, table=True):
    """The master data a resume is built from, per account.

    This used to be a single `master_profile.json` on disk, read by resume
    generation and form autofill. That was correct for a one-person tool, but
    once signup was opened up it meant every account's generated resume carried
    the *file owner's* name, email and phone — the profile is the one place in
    the app that is pure personal data, so it is also the one that most needed
    isolating.

    Stored as a JSON string rather than columns because the shape is a nested
    document (experience, projects, LLM rules) that the resume prompt consumes
    whole, and SQLite has no JSON type worth modelling against.
    """

    __tablename__ = "user_profile"

    user_id: Optional[int] = Field(
        default=None, primary_key=True, foreign_key="user.user_id"
    )
    data: str = Field(default="{}", description="The profile document, as JSON text")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
