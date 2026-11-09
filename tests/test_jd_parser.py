"""Deterministic parsing of minimum experience requirements from JD output/text."""
import pytest

from backend.app.services.jd_parser import _build_parsed_jd, extract_minimum_experience
from backend.app.services.job_fit import assess_fit


@pytest.mark.parametrize("value,expected", [
    (0, 0),
    (1, 1),
    ("1 year", 1),
    ("1+ years", 1),
    ("0-1 years", 0),
    ("0 - 1 years", 0),
    ("1-2 years", 1),
    ("1 to 2 years", 1),
    ("2–4 years", 2),
    ("minimum 2 years", 2),
    ("at least 2 years", 2),
    ("3+ years", 3),
    ("Fresher", 0),
    ("Freshers", 0),
    ("Entry Level", 0),
])
def test_model_experience_variations_use_the_minimum(value, expected):
    parsed = _build_parsed_jd({"experience_years": value})
    assert parsed.experience_years == expected


@pytest.mark.parametrize("text,expected", [
    ("Experience: 0 years", 0),
    ("Freshers can apply for this entry level role", 0),
    ("Minimum 2 years of relevant experience required", 2),
    ("At least 2 years' work experience", 2),
    ("Requires 0-1 years of experience", 0),
    ("1 - 2 years of professional experience", 1),
    ("Candidates need 1 to 3 years of experience", 1),
    ("2–4 years of experience with Python", 2),
    ("5+ years in Information Technology and 2+ years of experience in IAM", 5),
    ("3+ years of relevant experience required", 3),
])
def test_jd_text_extraction_returns_minimum(text, expected):
    assert extract_minimum_experience(text) == expected


def test_minimum_experience_cannot_be_overridden_by_skill_match():
    profile = {
        "preferences": {"total_experience_years": "1"},
        "primary_skills": ["Python", "FastAPI"],
    }
    parsed = _build_parsed_jd({
        "experience_years": "2-4 years",
        "required_skills": ["Python", "FastAPI"],
    })
    result = assess_fit(profile, {
        "experience_years": parsed.experience_years,
        "required_skills": parsed.required_skills,
    })
    assert result.fits is False
    assert result.years_required == 2


@pytest.mark.parametrize("jd,should_match", [
    ("0 years", True),
    ("1 year", True),
    ("0-1 years", True),
    ("1-2 years", True),
    ("0-2 years", True),
    ("1-3 years", True),
    ("2+ years", False),
    ("2-4 years", False),
    ("3+ years", False),
])
def test_one_year_candidate_experience_matrix(jd, should_match):
    profile = {
        "preferences": {"total_experience_years": "1"},
        "primary_skills": ["Python"],
    }
    parsed = _build_parsed_jd({"experience_years": jd})
    result = assess_fit(profile, {"experience_years": parsed.experience_years})
    assert result.fits is should_match


def test_text_minimum_corrects_an_underread_model_value():
    from backend.app.services.jd_parser import _merge_text_experience

    parsed = _build_parsed_jd({"experience_years": 2})
    _merge_text_experience(
        parsed,
        "Minimum 5+ years of Information Technology experience and 2+ years in IAM.",
    )
    assert parsed.experience_years == 5
