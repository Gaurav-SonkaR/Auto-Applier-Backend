"""ATS coverage is calculated from resume content, never model self-report."""
from backend.app.services.resume_generator import _compute_ats_score
from backend.app.workflows.types import ParsedJD


def test_unrendered_model_keyword_claims_do_not_increase_ats_score():
    parsed = ParsedJD(required_skills=["Python", "FastAPI"])
    result = {"ats_keywords_used": ["Python", "FastAPI"]}

    assert _compute_ats_score(result, parsed) == 0


def test_rendered_resume_content_is_what_the_score_measures():
    parsed = ParsedJD(required_skills=["Python", "FastAPI"])

    assert _compute_ats_score(
        {"rendered_resume": r"\textbf{Python} engineer"}, parsed
    ) == 50
