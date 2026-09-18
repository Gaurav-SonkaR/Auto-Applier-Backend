"""Job preferences: the questions every portal asks, answered from the profile.

Wellfound, Naukri and LinkedIn ask the same handful on every application — will
you relocate, where, notice period, current and expected CTC, sponsorship,
years of experience.

Those are facts about the applicant, not questions. Routing them to an LLM is
the same defect as the resume summary inventing "3+ years": asked a factual
question it has no answer to, a model answers anyway. So the split these tests
protect is: facts come from the profile, and only genuinely open questions
("what interests you most about this company?") reach a model.
"""
from __future__ import annotations

import pytest

from backend.app.services import profiles
from backend.app.services.form_autofill import (
    FieldContext,
    build_autofill_values,
    match_field,
)

PROFILE = {
    "personal": {"name": "Asha Verma", "email": "asha@example.com", "location": "Pune"},
    "preferences": {
        "total_experience_years": "2",
        "willing_to_relocate": "Yes",
        "preferred_locations": ["Pune", "Remote"],
        "notice_period": "30 days",
        "current_ctc": "12 LPA",
        "expected_ctc": "20 LPA",
        "needs_visa_sponsorship": "No",
    },
}


# ── The profile carries them ────────────────────────────────────────────────

class TestProfileShape:
    def test_a_blank_profile_has_every_preference_key(self):
        """Autofill indexes straight into this; a missing key would surface
        mid-application rather than at the point of editing."""
        blank = profiles.blank_profile()["preferences"]
        for field in profiles.PREFERENCE_FIELDS:
            assert field in blank

    def test_locations_default_to_a_list_and_the_rest_to_strings(self):
        blank = profiles.blank_profile()["preferences"]
        assert blank["preferred_locations"] == []
        assert blank["notice_period"] == ""

    def test_preferences_round_trip(self, session, admin_user):
        saved = profiles.save_profile(session, admin_user.user_id, {
            **PROFILE, "primary_skills": ["Python"],
            "experience": [{"title": "Backend Engineer"}],
        })
        assert saved["preferences"]["notice_period"] == "30 days"
        assert saved["preferences"]["preferred_locations"] == ["Pune", "Remote"]

    def test_unknown_preference_keys_are_dropped(self):
        out = profiles.normalise({"preferences": {"notice_period": "Immediate",
                                                  "favourite_colour": "blue"}})
        assert out["preferences"]["notice_period"] == "Immediate"
        assert "favourite_colour" not in out["preferences"]

    def test_a_string_where_a_list_belongs_is_ignored_not_stored(self):
        """Storing "Pune" where a list is expected would make autofill emit
        "P, u, n, e" when it joins it."""
        out = profiles.normalise({"preferences": {"preferred_locations": "Pune"}})
        assert out["preferences"]["preferred_locations"] == []

    def test_blank_entries_in_the_location_list_are_dropped(self):
        out = profiles.normalise(
            {"preferences": {"preferred_locations": ["Pune", "  ", "", "Remote"]}}
        )
        assert out["preferences"]["preferred_locations"] == ["Pune", "Remote"]

    def test_omitting_preferences_does_not_wipe_the_rest(self):
        out = profiles.normalise({"personal": {"name": "X"}})
        assert out["preferences"]["notice_period"] == ""
        assert out["llm_rules"]["locked_fields"]


# ── Autofill exposes them ───────────────────────────────────────────────────

class TestAutofillValues:
    def test_every_preference_is_offered_to_a_form(self):
        values = build_autofill_values(PROFILE)
        for field in profiles.PREFERENCE_FIELDS:
            assert field in values

    def test_locations_are_joined_for_a_single_text_input(self):
        assert build_autofill_values(PROFILE)["preferred_locations"] == "Pune, Remote"

    def test_values_are_the_profile_s_own(self):
        values = build_autofill_values(PROFILE)
        assert values["notice_period"] == "30 days"
        assert values["expected_ctc"] == "20 LPA"
        assert values["willing_to_relocate"] == "Yes"

    def test_an_empty_profile_yields_blanks_not_errors(self):
        values = build_autofill_values({})
        assert values["notice_period"] == ""
        assert values["preferred_locations"] == ""


# ── The facts / questions split ─────────────────────────────────────────────

class TestQuestionRouting:
    @pytest.mark.parametrize("label,expected", [
        ("Are you willing to relocate?", "willing_to_relocate"),
        ("Open to relocation", "willing_to_relocate"),
        ("Preferred location", "preferred_locations"),
        ("Where would you like to work?", "preferred_locations"),
        ("What is your notice period?", "notice_period"),
        ("When can you start?", "notice_period"),
        ("Current CTC", "current_ctc"),
        ("Current Salary", "current_ctc"),
        ("Expected CTC", "expected_ctc"),
        ("Salary expectation", "expected_ctc"),
        ("Total experience (years)", "total_experience_years"),
        ("Years of experience", "total_experience_years"),
        ("Do you require visa sponsorship?", "needs_visa_sponsorship"),
        ("Are you authorized to work in the US?", "needs_visa_sponsorship"),
    ])
    def test_a_factual_question_resolves_to_a_profile_field(self, label, expected):
        assert match_field(FieldContext(label=label)) == expected

    @pytest.mark.parametrize("label", [
        "What interests you most about this company?",
        "Why do you want this role?",
        "Describe a project you are proud of",
        "Tell us about a time you disagreed with a teammate",
    ])
    def test_a_genuinely_open_question_is_left_to_the_model(self, label):
        """These are the only ones worth an LLM call — and the only ones where
        it is not inventing a fact."""
        assert match_field(FieldContext(label=label)) is None

    def test_preferred_location_is_not_swallowed_by_the_city_rule(self):
        """Rule order matters: "preferred location" contains "location", and
        the generic rule sits below it."""
        assert match_field(FieldContext(label="Preferred location")) == "preferred_locations"
        assert match_field(FieldContext(label="City")) == "location"

    def test_current_ctc_is_not_read_as_current_company(self):
        assert match_field(FieldContext(label="Current CTC")) == "current_ctc"
        assert match_field(FieldContext(label="Current Company")) == "current_company"

    def test_every_rule_maps_to_a_value_autofill_can_supply(self):
        """A rule naming a key `build_autofill_values` does not return would
        match a field and then fill it with nothing."""
        from backend.app.services.form_autofill import _MATCH_RULES

        available = set(build_autofill_values(PROFILE))
        for key, _ in _MATCH_RULES:
            assert key in available, key


class TestTheApplierDefersToAutofill:
    def test_the_skip_decision_is_derived_not_hand_listed(self):
        """The applier used to skip a hard-coded ("name","email","phone",...)
        list, so a new profile field left the LLM answering it. It now asks
        `match_field`, which is the same rule autofill uses."""
        import inspect

        from backend.app.services import applier

        source = inspect.getsource(applier._fill_form)
        assert "match_field(FieldContext(label=label))" in source

    @pytest.mark.parametrize("label", [
        "Notice period", "Expected CTC", "Willing to relocate", "Email",
    ])
    def test_anything_autofill_owns_is_recognised(self, label):
        assert match_field(FieldContext(label=label)) is not None
