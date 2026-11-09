"""Dropdown and radio answers: from the profile, declined, or left — never guessed.

The applier used to select the first non-placeholder option of every dropdown.
On a submitted application that is "Willing to relocate?" answered by list
order, the lowest salary bracket, and a false answer on equal-opportunity
questions. These tests pin the replacement's decisions, then drive the real
glue in a real browser against a representative form — no live portal.
"""
from __future__ import annotations

import pytest
from urllib.parse import quote

from backend.app.services.form_choices import (
    Option,
    choose_option,
    decline_option,
    is_placeholder,
    is_sensitive,
    resolve_llm_choice,
)


def opts(*texts: str) -> list[Option]:
    return [Option(value=f"v{i}", text=t) for i, t in enumerate(texts)]


# ── Placeholders ────────────────────────────────────────────────────────────

class TestPlaceholders:
    @pytest.mark.parametrize("text", ["", "Select", "-- Select --", "Please select",
                                      "Choose one", "---", "Select an option"])
    def test_recognised(self, text):
        assert is_placeholder(Option(value="", text=text))

    @pytest.mark.parametrize("text", ["Yes", "No", "Immediate", "Selected candidates only"])
    def test_real_options_are_not_placeholders(self, text):
        assert not is_placeholder(Option(value="x", text=text))


# ── Yes / No ────────────────────────────────────────────────────────────────

class TestYesNo:
    OPTIONS = opts("Select", "Yes, I am willing to relocate", "No, I prefer my current city")

    def test_yes(self):
        assert choose_option("willing_to_relocate", "Yes", self.OPTIONS).text.startswith("Yes")

    def test_no(self):
        assert choose_option("willing_to_relocate", "No", self.OPTIONS).text.startswith("No")

    def test_a_qualified_yes_still_reads_as_yes(self):
        assert choose_option("willing_to_relocate", "Yes, within India", self.OPTIONS) \
            .text.startswith("Yes")

    @pytest.mark.parametrize("unclear", ["", "Maybe", "Depends on the role"])
    def test_an_unclear_profile_value_selects_nothing(self, unclear):
        assert choose_option("willing_to_relocate", unclear, self.OPTIONS) is None

    def test_ambiguous_options_select_nothing(self):
        both = opts("Yes", "Yes, with support")
        assert choose_option("needs_visa_sponsorship", "Yes", both) is None


# ── Brackets ────────────────────────────────────────────────────────────────

class TestNoticePeriod:
    OPTIONS = opts("-- Select --", "Immediate", "15 days or less", "1 Month",
                   "2 Months", "More than 3 months")

    @pytest.mark.parametrize("value,expected", [
        ("30 days", "1 Month"),          # units reconcile
        ("1 month", "1 Month"),
        ("Immediate", "Immediate"),
        ("2 weeks", "15 days or less"),
        ("60 days", "2 Months"),
        ("4 months", "More than 3 months"),
    ])
    def test_maps_to_the_bracket(self, value, expected):
        assert choose_option("notice_period", value, self.OPTIONS).text == expected

    def test_no_bracket_fits_selects_nothing(self):
        assert choose_option("notice_period", "45 days", self.OPTIONS) is None


class TestExperience:
    OPTIONS = opts("Fresher", "0-1 years", "2-4 years", "5+ years")

    @pytest.mark.parametrize("value,expected", [
        ("2", "2-4 years"), ("3.5", "2-4 years"), ("7", "5+ years"), ("0", "Fresher"),
    ])
    def test_tightest_bracket_wins(self, value, expected):
        assert choose_option("total_experience_years", value, self.OPTIONS).text == expected

    def test_overlapping_equal_brackets_are_not_guessed(self):
        assert choose_option("total_experience_years", "3",
                             opts("2-4 years", "3-5 years")) is None


class TestCtc:
    def test_lakh_value_lands_in_a_lakh_bracket(self):
        options = opts("5-10 LPA", "10-20 LPA", "20L - 30L")
        assert choose_option("expected_ctc", "25 LPA", options).text == "20L - 30L"

    def test_a_different_currency_is_never_matched(self):
        """25 LPA must not become "$20k-$40k" just because 25 is in 20-40."""
        assert choose_option("expected_ctc", "25 LPA", opts("$20k-$40k", "$40k+")) is None


class TestLocations:
    OPTIONS = opts("Bengaluru", "Pune", "Remote", "Hyderabad")

    def test_first_preference_wins(self):
        assert choose_option("preferred_locations", "Pune, Remote", self.OPTIONS).text == "Pune"

    def test_falls_back_to_the_next_preference(self):
        assert choose_option("preferred_locations", "Delhi, Remote", self.OPTIONS).text == "Remote"

    def test_no_listed_preference_selects_nothing(self):
        assert choose_option("preferred_locations", "Delhi", self.OPTIONS) is None

    def test_word_bounded(self):
        assert choose_option("preferred_locations", "Pune", opts("Punekar Residency")) is None


# ── Self-identification ─────────────────────────────────────────────────────

class TestSensitive:
    @pytest.mark.parametrize("question", [
        "Gender", "What is your race/ethnicity?", "Veteran status",
        "Do you have a disability?", "Pronouns", "Marital status", "Date of birth",
    ])
    def test_recognised(self, question):
        assert is_sensitive(question)

    @pytest.mark.parametrize("question", [
        "Willing to relocate?", "Notice period", "Preferred location", "Expected CTC",
    ])
    def test_ordinary_questions_are_not(self, question):
        assert not is_sensitive(question)

    def test_the_only_choice_offered_is_to_decline(self):
        options = opts("Select", "Male", "Female", "Prefer not to say")
        assert decline_option(options).text == "Prefer not to say"

    def test_no_decline_option_means_nothing_is_chosen(self):
        assert decline_option(opts("Male", "Female")) is None


# ── Model picks ─────────────────────────────────────────────────────────────

class TestModelChoiceIsVerbatimOnly:
    OPTIONS = opts("Select", "LinkedIn", "Referral", "Company website")

    def test_an_exact_option_is_accepted(self):
        assert resolve_llm_choice("Referral", self.OPTIONS).text == "Referral"

    def test_case_and_trailing_punctuation_are_tolerated(self):
        assert resolve_llm_choice('"referral."', self.OPTIONS).text == "Referral"

    @pytest.mark.parametrize("reply", [
        "UNKNOWN", "", "Referral or LinkedIn", "A referral from a friend",
        "I would choose Referral because", "Select",
    ])
    def test_anything_else_is_no_answer(self, reply):
        assert resolve_llm_choice(reply, self.OPTIONS) is None


# ── The real glue, in a real browser, against a representative form ─────────

FORM_HTML = """
<form>
  <label for="name">Full name</label><input id="name" name="name">
  <label for="reloc">Are you willing to relocate?</label>
  <select id="reloc" name="reloc">
    <option value="">Select</option><option value="no">No</option><option value="yes">Yes</option>
  </select>
  <label for="notice">Notice period</label>
  <select id="notice" name="notice">
    <option value="">-- Select --</option><option value="0">Immediate</option>
    <option value="30">1 Month</option><option value="60">2 Months</option>
  </select>
  <label for="exp">Total experience</label>
  <select id="exp" name="exp">
    <option value="">Select</option><option value="a">0-1 years</option>
    <option value="b">2-4 years</option><option value="c">5+ years</option>
  </select>
  <label for="gender">Gender</label>
  <select id="gender" name="gender">
    <option value="">Select</option><option value="m">Male</option>
    <option value="f">Female</option><option value="x">Prefer not to say</option>
  </select>
  <label for="veteran">Veteran status</label>
  <select id="veteran" name="veteran">
    <option value="">Select</option><option value="y">I am a veteran</option>
    <option value="n">I am not a veteran</option>
  </select>
  <label for="country">Country</label>
  <select id="country" name="country">
    <option value="">Select</option><option value="af">Afghanistan</option><option value="in">India</option>
  </select>
  <fieldset><legend>Do you require visa sponsorship?</legend>
    <label><input type="radio" name="visa" value="1"> Yes</label>
    <label><input type="radio" name="visa" value="0"> No</label>
  </fieldset>
  <label for="why">What interests you most about this company?</label>
  <textarea id="why" name="why"></textarea>
</form>
"""

PROFILE = {
    "personal": {"name": "Asha Verma", "email": "asha@example.com"},
    "preferences": {
        "willing_to_relocate": "Yes",
        "notice_period": "30 days",
        "total_experience_years": "2",
        "needs_visa_sponsorship": "No",
    },
}


@pytest.fixture
async def page():
    from selenium.common.exceptions import WebDriverException

    from backend.app.config import get_settings
    from backend.app.services.selenium_browser import selenium_session

    manager = selenium_session(get_settings(), headless=True)
    try:
        session = await manager.__aenter__()
    except WebDriverException as exc:
        pytest.skip(f"Chrome WebDriver unavailable: {exc}")
    try:
        page = session.page
        await page.navigate(f"data:text/html;charset=utf-8,{quote(FORM_HTML)}")
        yield page
    finally:
        await manager.__aexit__(None, None, None)


async def _value(page, selector):
    element = await page.find(selector)
    return await element.input_value() if element else ""


async def _checked(page, selector):
    element = await page.find(selector)
    return await element.is_selected() if element else False


class TestAutofillInABrowser:
    async def test_profile_facts_land_in_dropdowns_and_radios(self, page):
        from backend.app.services.form_autofill import autofill_form

        await autofill_form(page, PROFILE)

        assert await _value(page, "#reloc") == "yes"
        assert await _value(page, "#notice") == "30"        # "30 days" -> "1 Month"
        assert await _value(page, "#exp") == "b"            # 2 -> "2-4 years"
        assert await _checked(page, 'input[name="visa"][value="0"]')
        assert not await _checked(page, 'input[name="visa"][value="1"]')

    async def test_gender_is_declined_not_answered(self, page):
        from backend.app.services.form_autofill import autofill_form

        await autofill_form(page, PROFILE)
        assert await _value(page, "#gender") == "x"

    async def test_a_sensitive_dropdown_with_no_decline_option_is_left_alone(self, page):
        from backend.app.services.form_autofill import autofill_form

        await autofill_form(page, PROFILE)
        assert await _value(page, "#veteran") == ""

    async def test_an_unrecognised_dropdown_is_not_touched_by_autofill(self, page):
        from backend.app.services.form_autofill import autofill_form

        await autofill_form(page, PROFILE)
        assert await _value(page, "#country") == ""

    async def test_a_blank_preference_leaves_the_dropdown_blank(self, page):
        from backend.app.services.form_autofill import autofill_form

        await autofill_form(page, {"personal": {"name": "Asha"}, "preferences": {}})
        assert await _value(page, "#reloc") == ""
        assert await _value(page, "#notice") == ""

    async def test_a_value_already_set_by_the_portal_is_kept(self, page):
        from backend.app.services.form_autofill import autofill_form

        await (await page.find("#reloc")).select("no")
        await autofill_form(page, PROFILE)
        assert await _value(page, "#reloc") == "no"


class TestApplierDropdownPass:
    """The second pass — the one that used to pick the first option."""

    class Router:
        def __init__(self, reply):
            self.reply, self.prompts = reply, []

        async def complete(self, task, prompt, system=""):
            self.prompts.append((prompt, system))
            return self.reply

    class Job:
        job_id, company, title = 1, "Acme", "Backend Engineer"

    async def test_it_no_longer_picks_the_first_option(self, page):
        from backend.app.services.applier import _fill_remaining_dropdowns

        await _fill_remaining_dropdowns(page, "select", self.Job(), PROFILE,
                                        self.Router("UNKNOWN"))
        for sel in ("#reloc", "#notice", "#exp", "#veteran", "#country"):
            assert await _value(page, sel) == "", sel

    async def test_sensitive_questions_never_reach_the_model(self, page):
        from backend.app.services.applier import _fill_remaining_dropdowns

        router = self.Router("Male")
        await _fill_remaining_dropdowns(page, "select", self.Job(), PROFILE, router)
        assert await _value(page, "#gender") == "x"
        assert all("Gender" not in prompt and "Veteran" not in prompt
                   for prompt, _ in router.prompts)

    async def test_profile_owned_fields_are_left_for_a_human(self, page):
        """The profile pass recognised them and found no fit; a model must not
        overrule that with a guess."""
        from backend.app.services.applier import _fill_remaining_dropdowns

        router = self.Router("Yes")
        await _fill_remaining_dropdowns(page, "select", self.Job(), PROFILE, router)
        assert await _value(page, "#reloc") == ""
        assert all("relocate" not in prompt for prompt, _ in router.prompts)

    async def test_the_model_may_answer_an_ordinary_dropdown_verbatim(self, page):
        from backend.app.services.applier import _fill_remaining_dropdowns

        await _fill_remaining_dropdowns(page, "select", self.Job(), PROFILE,
                                        self.Router("India"))
        assert await _value(page, "#country") == "in"

    async def test_a_paraphrased_model_reply_is_not_used(self, page):
        from backend.app.services.applier import _fill_remaining_dropdowns

        await _fill_remaining_dropdowns(page, "select", self.Job(), PROFILE,
                                        self.Router("I live in India"))
        assert await _value(page, "#country") == ""
