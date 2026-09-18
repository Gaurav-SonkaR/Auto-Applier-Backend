"""The question bank: the user's answers to what portals ask, and how forms get them.

Replaced a hidden cache that stored whatever a model wrote and replayed it
verbatim — including at the next company. "What interests you most about this
company?" answered for Acme was typed into Globex's form, naming Acme.

These tests pin: the catalogue's integrity, per-account isolation, that a model
draft never overwrites the user, that drafts are stored with company and role
abstracted, that fixed answers are typed as written and tailor answers adapted,
that self-identification never reaches any of it, and — in a real browser — that
the bank beats the keyword rules and fills radios and dropdowns from the user's
own words.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlmodel import delete

from backend.app.models.qa_answer import QaAnswer
from backend.app.models.user import User
from backend.app.services import question_bank as qb
from backend.app.services import users as user_service
from backend.app.services.form_choices import Option, choose_from_answer, is_sensitive
from backend.app.services.job_fit import skill_years_answer
from backend.app.services.question_catalogue import CATALOGUE, CATEGORIES


class Job:
    def __init__(self, company="Acme", title="Backend Engineer", jd="We build payments APIs.",
                 user_id=None):
        self.company, self.title, self.jd_text = company, title, jd
        self.portal, self.job_id, self.user_id = "wellfound", 1, user_id


class Router:
    """Records every prompt; replies from a script."""

    def __init__(self, *replies, fail=False):
        self.replies, self.calls, self.fail = list(replies), [], fail

    async def complete(self, task, prompt, system=""):
        self.calls.append({"prompt": prompt, "system": system})
        if self.fail:
            raise ConnectionError("provider down")
        return self.replies.pop(0) if self.replies else "UNKNOWN"


PROFILE = {
    "personal": {"name": "Asha Verma", "location": "Pune"},
    "primary_skills": ["Python", "FastAPI", "PostgreSQL"],
    "secondary_skills": ["Kubernetes"],
    "experience": [{"title": "Backend Engineer", "company": "Northwind",
                    "start_date": "Feb 2025", "end_date": "Present"}],
}


@pytest.fixture
def accounts(session, admin_user):
    username = "bank-neighbour"
    session.exec(delete(User).where(User.username == username))
    session.commit()
    other = user_service.create_user(session, username, "neighbour-pass-1",
                                     status=user_service.UserStatus.ACTIVE)
    ids = (admin_user.user_id, other.user_id)
    session.exec(delete(QaAnswer).where(QaAnswer.user_id.in_(ids)))
    session.commit()
    yield ids
    session.exec(delete(QaAnswer).where(QaAnswer.user_id.in_(ids)))
    session.exec(delete(User).where(User.username == username))
    session.commit()


# ── Catalogue ───────────────────────────────────────────────────────────────

class TestCatalogue:
    def test_it_is_substantial(self):
        assert len(CATALOGUE) >= 40

    def test_ids_are_unique(self):
        ids = [e.id for e in CATALOGUE]
        assert len(ids) == len(set(ids))

    def test_no_two_questions_share_a_wording(self):
        """A collision would silently give one question another's answer."""
        owner: dict[str, str] = {}
        for entry in CATALOGUE:
            for wording in (entry.question, *entry.aliases):
                key = qb.normalise_question(wording)
                assert owner.setdefault(key, entry.id) == entry.id, (wording, owner[key], entry.id)

    def test_nothing_sensitive_is_in_it(self):
        for entry in CATALOGUE:
            for wording in (entry.question, *entry.aliases):
                assert not is_sensitive(wording), wording

    def test_categories_and_modes_are_valid(self):
        for entry in CATALOGUE:
            assert entry.category in CATEGORIES, entry.id
            assert entry.mode in qb.MODES, entry.id

    def test_company_specific_questions_are_tailored(self):
        by_id = {e.id: e for e in CATALOGUE}
        for qid in ("why_company", "why_role", "cover_letter"):
            assert by_id[qid].mode == "tailor"


# ── Matching ────────────────────────────────────────────────────────────────

class TestMatching:
    @pytest.mark.parametrize("asked,expected", [
        ("Why do you want to join Acme?", "why_company"),
        ("What interests you most about this company? *", "why_company"),
        ("Are you currently serving notice period?", "serving_notice"),
        ("Tell us about yourself (required)", "about_yourself"),
        ("How did you hear about us?", "heard_about_job"),
        ("Do you have valid passport?", "passport"),
        ("Cover Letter", "cover_letter"),
    ])
    def test_portal_wordings_find_the_catalogue_question(self, asked, expected):
        assert qb.catalogue_entry(asked, company="Acme").id == expected

    def test_the_same_question_at_two_companies_has_one_key(self):
        assert qb.canonical_key("Why do you want to join Acme?", "Acme") == \
            qb.canonical_key("Why do you want to join Globex?", "Globex")

    def test_unknown_questions_are_not_forced_onto_the_catalogue(self):
        assert qb.catalogue_entry("What is your favourite database index?") is None

    @pytest.mark.parametrize("question,mode", [
        ("Why do you want to work with our team?", "tailor"),
        ("What excites you about this role?", "tailor"),
        ("Do you have a GitHub account?", "fixed"),
    ])
    def test_mode_is_inferred_for_custom_questions(self, question, mode):
        assert qb.infer_mode(question) == mode


class TestPlaceholders:
    def test_a_draft_is_stored_with_the_names_abstracted(self):
        assert qb.templatize("Acme's payments work suits a Backend Engineer like me",
                             "Acme", "Backend Engineer") == \
            "{company}'s payments work suits a {role} like me"

    def test_rendering_fills_them_for_the_current_job(self):
        assert qb.render("I admire {company} and the {role} role", "Globex", "Data Engineer") == \
            "I admire Globex and the Data Engineer role"

    def test_a_short_name_is_not_swapped_inside_other_words(self):
        """A company called "AI" must not turn "maintained" into "m{company}ntained"."""
        assert qb.templatize("I maintained APIs at AI", "AI", "") == "I maintained APIs at {company}"


# ── Storage ─────────────────────────────────────────────────────────────────

class TestStorage:
    def test_the_bank_lists_every_catalogue_question(self, session, accounts):
        mine, _ = accounts
        items = qb.list_bank(session, mine)
        assert len(items) == len(CATALOGUE)
        assert all(i.source == "catalogue" and i.answer == "" for i in items)

    def test_every_alias_shares_one_answer(self, session, accounts):
        mine, _ = accounts
        qb.save_answer(session, mine, "How did you hear about us?", "LinkedIn")
        item = next(i for i in qb.list_bank(session, mine) if i.catalogue_id == "heard_about_job")
        assert item.answer == "LinkedIn" and item.source == "user"

    def test_accounts_never_see_each_other(self, session, accounts):
        mine, theirs = accounts
        qb.save_answer(session, mine, "Tell us about yourself.", "My pitch")
        assert all(i.answer == "" for i in qb.list_bank(session, theirs))

    def test_a_draft_never_overwrites_the_user(self, session, accounts):
        mine, _ = accounts
        qb.save_answer(session, mine, "Tell us about yourself.", "Written by me")
        qb.save_answer(session, mine, "Tell us about yourself.", "Model draft",
                       source=qb.SOURCE_GENERATED)
        item = next(i for i in qb.list_bank(session, mine) if i.catalogue_id == "about_yourself")
        assert item.answer == "Written by me" and not item.needs_review

    def test_saving_a_draft_marks_it_reviewed(self, session, accounts):
        mine, _ = accounts
        qb.save_answer(session, mine, "Do you have a laptop?", "Yes", source=qb.SOURCE_GENERATED)
        assert qb.list_bank(session, mine)[-1].needs_review
        qb.save_answer(session, mine, "Do you have a laptop?", "Yes, a MacBook")
        custom = [i for i in qb.list_bank(session, mine) if i.catalogue_id is None]
        assert custom[0].answer == "Yes, a MacBook" and not custom[0].needs_review

    def test_an_empty_answer_clears_the_entry(self, session, accounts):
        mine, _ = accounts
        qb.save_answer(session, mine, "Tell us about yourself.", "Something")
        qb.save_answer(session, mine, "Tell us about yourself.", "   ")
        assert session.exec(QaAnswer.__table__.select().where(QaAnswer.user_id == mine)).all() == []

    def test_sensitive_questions_are_never_stored(self, session, accounts):
        mine, _ = accounts
        assert qb.save_answer(session, mine, "What is your gender?", "Female") is None

    def test_delete_is_scoped_to_the_owner(self, session, accounts):
        mine, theirs = accounts
        row = qb.save_answer(session, mine, "Custom question?", "Custom answer")
        assert not qb.delete_entry(session, theirs, row.qa_id)
        assert qb.delete_entry(session, mine, row.qa_id)


# ── Resolving a form question ───────────────────────────────────────────────

class TestResolveAnswer:
    async def test_a_fixed_answer_is_typed_as_written_without_a_model(self, session, accounts):
        mine, _ = accounts
        qb.save_answer(session, mine, "How did you hear about this job?", "Through {company}'s careers page")
        router = Router()
        answer = await qb.resolve_answer(mine, "How did you hear about us?", Job(), PROFILE, router)
        assert answer == "Through Acme's careers page"
        assert router.calls == []

    async def test_a_tailor_answer_is_adapted_from_the_users_words(self, session, accounts):
        mine, _ = accounts
        qb.save_answer(session, mine, "What interests you most about this company?",
                       "I like teams building developer tooling at {company}.")
        router = Router("Acme's payments APIs are the developer tooling I want to build.")
        answer = await qb.resolve_answer(mine, "Why do you want to join Acme?", Job(), PROFILE, router)

        assert answer.startswith("Acme's payments")
        prompt = router.calls[0]["prompt"]
        assert "I like teams building developer tooling at Acme." in prompt
        assert "We build payments APIs." in prompt         # the JD reaches the model
        assert "add none" in router.calls[0]["system"]

    async def test_tailoring_failure_falls_back_to_the_users_own_answer(self, session, accounts):
        mine, _ = accounts
        qb.save_answer(session, mine, "Cover letter", "Dear {company} team, ...")
        answer = await qb.resolve_answer(mine, "Cover note", Job(), PROFILE, Router(fail=True))
        assert answer == "Dear Acme team, ..."

    async def test_an_unanswered_question_is_drafted_and_saved_for_review(self, session, accounts):
        mine, _ = accounts
        router = Router("I want to build Acme's payments platform as a Backend Engineer.")
        answer = await qb.resolve_answer(mine, "Why do you want to join Acme?", Job(), PROFILE, router)
        assert answer.startswith("I want to build Acme")

        item = next(i for i in qb.list_bank(session, mine) if i.catalogue_id == "why_company")
        assert item.source == "generated" and item.needs_review
        assert item.answer == "I want to build {company}'s payments platform as a {role}."

    async def test_the_next_company_never_sees_the_last_ones_name(self, session, accounts):
        """QA-20: the old cache typed Acme's answer into Globex's form."""
        mine, _ = accounts
        await qb.resolve_answer(mine, "Why do you want to join Acme?", Job("Acme"), PROFILE,
                                Router("Acme's mission fits me."))
        router = Router("Globex's platform fits me.")
        answer = await qb.resolve_answer(mine, "Why do you want to join Globex?",
                                         Job("Globex", jd="Globex builds logistics software."),
                                         PROFILE, router)
        assert "Acme" not in answer
        assert "Acme" not in router.calls[0]["prompt"]
        assert "Globex's mission fits me." in router.calls[0]["prompt"]   # base, re-rendered

    async def test_a_custom_question_naming_the_company_is_reused_elsewhere(self, session, accounts):
        mine, _ = accounts
        await qb.resolve_answer(mine, "Have you used Acme's public API?", Job("Acme"), PROFILE,
                                Router("No, but I have integrated similar payment APIs."))
        router = Router()
        again = await qb.resolve_answer(mine, "Have you used Globex's public API?",
                                        Job("Globex"), PROFILE, router)
        assert again == "No, but I have integrated similar payment APIs."
        assert router.calls == []

    async def test_unknown_means_blank_and_nothing_is_stored(self, session, accounts):
        mine, _ = accounts
        answer = await qb.resolve_answer(mine, "Do you have a valid passport?", Job(), PROFILE,
                                         Router("UNKNOWN"))
        assert answer is None
        assert all(i.answer == "" for i in qb.list_bank(session, mine))

    async def test_a_provider_outage_leaves_the_field_blank(self, session, accounts):
        mine, _ = accounts
        assert await qb.resolve_answer(mine, "Tell us about yourself.", Job(), PROFILE,
                                       Router(fail=True)) is None

    @pytest.mark.parametrize("question", ["Gender", "Date of birth", "Nationality", "Marital status"])
    async def test_self_identification_never_reaches_a_model(self, accounts, question):
        mine, _ = accounts
        router = Router("anything")
        assert await qb.resolve_answer(mine, question, Job(), PROFILE, router) is None
        assert router.calls == []


class TestChoiceFieldsUseOnlyTheUsersWords:
    def test_stored_answer_ignores_unreviewed_drafts(self, session, accounts):
        mine, _ = accounts
        qb.save_answer(session, mine, "Do you have a valid passport?", "Yes",
                       source=qb.SOURCE_GENERATED)
        assert qb.stored_answer(mine, "Do you have a valid passport?") is None
        qb.save_answer(session, mine, "Do you have a valid passport?", "Yes")
        assert qb.stored_answer(mine, "Do you have a valid passport?") == "Yes"

    @pytest.mark.parametrize("answer,expected", [
        ("Yes, hybrid preferred", "Yes"), ("No", "No"), ("Referral", "Referral"),
    ])
    def test_a_written_answer_picks_its_option(self, answer, expected):
        options = [Option("", "Select"), Option("y", "Yes"), Option("n", "No"),
                   Option("r", "Referral")]
        yes_no = [o for o in options if o.text in ("Select", "Yes", "No")]
        pool = yes_no if expected in ("Yes", "No") else options
        assert choose_from_answer(answer, pool).text == expected

    def test_a_vague_answer_picks_nothing(self):
        assert choose_from_answer("Depends", [Option("y", "Yes"), Option("n", "No")]) is None


class TestSkillYears:
    VOCAB = frozenset({"python", "kafka", "kubernetes", "fastapi"})
    TODAY = date(2026, 9, 14)

    @pytest.mark.parametrize("question,expected", [
        ("How many years of work experience do you have with Python?", "1"),
        ("How many years of experience do you have with Kafka?", "0"),
        ("Years of experience with Kubernetes", "1"),
        ("How many years of experience do you have in software development?", None),
    ])
    def test_per_skill_answers(self, question, expected):
        assert skill_years_answer(question, PROFILE, self.VOCAB, self.TODAY) == expected


class TestFitLength:
    def test_short_text_is_untouched(self):
        assert qb.fit_length("Short.", 100) == "Short."

    def test_cuts_at_a_sentence_boundary(self):
        text = "First sentence is here. Second sentence runs on and on and on."
        assert qb.fit_length(text, 40) == "First sentence is here."

    def test_never_cuts_mid_word(self):
        assert qb.fit_length("alpha beta gamma delta", 13) == "alpha beta"


# ── API ─────────────────────────────────────────────────────────────────────

class TestApi:
    def test_requires_auth(self, anon_client):
        assert anon_client.get("/api/profile/questions").status_code == 401

    def test_lists_the_catalogue_with_counts(self, client, accounts):
        body = client.get("/api/profile/questions").json()
        assert body["total"] == len(CATALOGUE)
        assert body["answered"] == 0
        assert body["categories"][:len(CATEGORIES)] == list(CATEGORIES)

    def test_save_then_list(self, client, accounts):
        body = client.put("/api/profile/questions", json={
            "question": "How did you hear about this job?", "answer": "LinkedIn",
        }).json()
        item = next(i for i in body["items"] if i["catalogue_id"] == "heard_about_job")
        assert item["answer"] == "LinkedIn" and body["answered"] == 1

    def test_a_custom_question_appears_after_the_catalogue(self, client, accounts):
        body = client.put("/api/profile/questions", json={
            "question": "Do you contribute to open source?", "answer": "Yes, to FastAPI plugins",
        }).json()
        assert body["items"][-1]["question"] == "Do you contribute to open source?"
        assert body["total"] == len(CATALOGUE) + 1

    def test_sensitive_questions_are_refused(self, client, accounts):
        assert client.put("/api/profile/questions", json={
            "question": "What is your gender?", "answer": "x"}).status_code == 422

    def test_an_invalid_mode_is_refused(self, client, accounts):
        assert client.put("/api/profile/questions", json={
            "question": "Tell us about yourself.", "answer": "x", "mode": "creative"}
        ).status_code == 422

    def test_deleting_another_accounts_entry_is_a_404(self, client, session, accounts):
        _, theirs = accounts
        row = qb.save_answer(session, theirs, "Their custom question?", "Their answer")
        assert client.delete(f"/api/profile/questions/{row.qa_id}").status_code == 404


# ── In a real browser ───────────────────────────────────────────────────────

FORM = """
<form>
  <label for="serving">Are you currently serving your notice period?</label>
  <input id="serving" name="serving">
  <label for="notice">Notice period</label><input id="notice" name="notice">
  <label for="py">How many years of work experience do you have with Python?</label>
  <input id="py" name="py">
  <label for="kafka">How many years of work experience do you have with Kafka?</label>
  <input id="kafka" name="kafka">
  <fieldset><legend>Are you comfortable working from the office?</legend>
    <label><input type="radio" name="wfo" value="1"> Yes</label>
    <label><input type="radio" name="wfo" value="0"> No</label>
  </fieldset>
  <label for="source">How did you hear about us?</label>
  <select id="source" name="source">
    <option value="">Select</option><option value="li">LinkedIn</option>
    <option value="ref">Referral</option>
  </select>
</form>
"""


@pytest.fixture
async def page():
    async_playwright = pytest.importorskip("playwright.async_api").async_playwright
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        page = await browser.new_page()
        await page.set_content(FORM)
        yield page
        await browser.close()


async def _value(page, selector):
    return await page.eval_on_selector(selector, "e => e.value")


class TestInABrowser:
    async def test_the_bank_beats_the_notice_period_keyword_rule(self, page, session, accounts):
        """"Are you serving your notice period?" contains "notice period" and
        used to be filled with "30 days"."""
        from backend.app.services.form_autofill import autofill_form

        mine, _ = accounts
        profile = {**PROFILE, "preferences": {"notice_period": "30 days"}}
        owns = qb.owns_question(session, mine, "Acme", "Backend Engineer")
        await autofill_form(page, profile, owns_question=owns)

        assert await _value(page, "#serving") == ""          # left for the bank
        assert await _value(page, "#notice") == "30 days"    # the basic still works

    async def test_per_skill_years_are_answered_honestly(self, page, session, accounts):
        from backend.app.services.form_autofill import autofill_form

        mine, _ = accounts
        await autofill_form(page, PROFILE, owns_question=qb.owns_question(session, mine))
        assert await _value(page, "#py") == "1"
        assert await _value(page, "#kafka") == "0"

    async def test_radios_and_dropdowns_follow_the_users_written_answers(
        self, page, session, accounts
    ):
        from backend.app.services.applier import _fill_remaining_dropdowns, _fill_remaining_radios

        mine, _ = accounts
        qb.save_answer(session, mine, "Are you comfortable working from the office?",
                       "Yes, three days a week")
        qb.save_answer(session, mine, "How did you hear about this job?", "Referral")
        job = Job(user_id=mine)
        owns = qb.owns_question(session, mine, job.company, job.title)
        router = Router("LinkedIn")                      # must not be consulted

        await _fill_remaining_radios(page, job, PROFILE, router, owns)
        await _fill_remaining_dropdowns(page, "select", job, PROFILE, router, owns)

        assert await page.is_checked('input[name="wfo"][value="1"]')
        assert await _value(page, "#source") == "ref"
        assert router.calls == []

    async def test_an_unanswered_bank_choice_is_left_blank(self, page, session, accounts):
        from backend.app.services.applier import _fill_remaining_dropdowns, _fill_remaining_radios

        mine, _ = accounts
        job = Job(user_id=mine)
        owns = qb.owns_question(session, mine, job.company, job.title)
        router = Router("Yes", "LinkedIn")
        await _fill_remaining_radios(page, job, PROFILE, router, owns)
        await _fill_remaining_dropdowns(page, "select", job, PROFILE, router, owns)

        assert not await page.is_checked('input[name="wfo"][value="1"]')
        assert await _value(page, "#source") == ""
        assert router.calls == []
