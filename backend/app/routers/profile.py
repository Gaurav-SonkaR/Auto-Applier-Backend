from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from backend.app.database import engine
from backend.app.models.user import User
from backend.app.models.user_profile import UserProfile
from backend.app.routers.auth import require_auth
from backend.app.schemas.profile import (
    BankQuestion,
    ProfileResponse,
    ProfileUpdateRequest,
    QuestionBankResponse,
    SaveBankAnswerRequest,
)
from backend.app.services import profiles, question_bank
from backend.app.services.form_choices import is_sensitive
from backend.app.services.question_catalogue import CATEGORIES

logger = logging.getLogger("api.profile")

profile_router = APIRouter(prefix="/api/profile", tags=["profile"])


def _response(session: Session, user_id: int) -> ProfileResponse:
    profile = profiles.get_profile(session, user_id)
    row = session.get(UserProfile, user_id)
    return ProfileResponse(
        profile=profile,
        is_complete=profiles.is_complete(profile),
        updated_at=row.updated_at if row else None,
    )


@profile_router.get("", response_model=ProfileResponse)
def get_profile(user: User = Depends(require_auth)) -> ProfileResponse:
    """This account's master data.

    Always returns a usable document: an account that has never saved one gets
    the blank skeleton, so the editor has the right shape to render.
    """
    with Session(engine) as session:
        return _response(session, user.user_id)


@profile_router.put("", response_model=ProfileResponse)
def update_profile(
    body: ProfileUpdateRequest, user: User = Depends(require_auth)
) -> ProfileResponse:
    """Replace this account's master data.

    Deliberately a whole-document PUT rather than a patch: the editor holds the
    entire profile in state, and merging partial updates into a nested document
    is where silent data loss lives.

    There is no admin override — a profile is the most personal thing in the
    app, and admins have no more access to it than to anyone's jobs.
    """
    with Session(engine) as session:
        try:
            profiles.save_profile(session, user.user_id, body.profile)
        except profiles.ProfileError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _response(session, user.user_id)


# ── Question bank ───────────────────────────────────────────────────────────

def _bank(session: Session, user_id: int) -> QuestionBankResponse:
    items = [BankQuestion(**asdict(i)) for i in question_bank.list_bank(session, user_id)]
    extra = sorted({i.category for i in items} - set(CATEGORIES))
    return QuestionBankResponse(
        items=items,
        categories=[*CATEGORIES, *extra],
        answered=sum(1 for i in items if i.answer.strip()),
        total=len(items),
        needs_review=sum(1 for i in items if i.needs_review),
    )


@profile_router.get("/questions", response_model=QuestionBankResponse)
def get_question_bank(user: User = Depends(require_auth)) -> QuestionBankResponse:
    """Every question portals ask, with this account's answer where there is one.

    Answers drafted during an application appear here marked for review, with
    the company and role already swapped for placeholders.
    """
    with Session(engine) as session:
        return _bank(session, user.user_id)


@profile_router.put("/questions", response_model=QuestionBankResponse)
def save_question_answer(
    body: SaveBankAnswerRequest, user: User = Depends(require_auth)
) -> QuestionBankResponse:
    """Write, correct or clear one answer. Saving marks a drafted answer reviewed."""
    if is_sensitive(body.question):
        raise HTTPException(
            status_code=422,
            detail="Questions about gender, ethnicity, disability, veteran status, religion, "
                   "marital status or age are never answered automatically.",
        )
    if body.mode is not None and body.mode not in question_bank.MODES:
        raise HTTPException(status_code=422, detail="mode must be 'fixed' or 'tailor'")
    with Session(engine) as session:
        question_bank.save_answer(session, user.user_id, body.question, body.answer,
                                  mode=body.mode, category=body.category)
        return _bank(session, user.user_id)


@profile_router.delete("/questions/{qa_id}", response_model=QuestionBankResponse)
def delete_question(qa_id: int, user: User = Depends(require_auth)) -> QuestionBankResponse:
    """Remove a custom question, or clear the answer to a catalogue one."""
    with Session(engine) as session:
        if not question_bank.delete_entry(session, user.user_id, qa_id):
            # 404 rather than 403 for another account's entry: a 403 would
            # confirm the id exists.
            raise HTTPException(status_code=404, detail=f"Question {qa_id} not found")
        return _bank(session, user.user_id)
