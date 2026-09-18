"""The cold email run itself — the code path that mails real strangers.

This flow had no tests at all, which made it the least-covered and highest-
consequence function in the app: everything else fails by wasting a run, this
one fails by sending something wrong, under the user's own name, to people who
did not ask for it. A bad send cannot be undone.

Nothing here touches SMTP. `EmailSender.send` and `generate_resume` are
replaced with recorders, so what is asserted is exactly what *would* have gone
out.
"""
from __future__ import annotations

import pytest
from sqlmodel import delete, select

from backend.app.models.cold_email import ColdEmailLog, EmailStatus
from backend.app.models.run_log import RunLog
from backend.app.services import profiles
from backend.app.workflows import cold_email_flow
from backend.app.workflows.state import ColdEmailConfig

PROFILE = {
    "personal": {
        "name": "Asha Verma",
        "email": "asha@example.com",
        "phone": "+91-9000000000",
        "linkedin": "linkedin.com/in/asha",
        "github": "", "kaggle": "", "location": "Pune", "resume_url": "",
    },
    "primary_skills": ["Python", "Django", "FastAPI"],
    "secondary_skills": [],
    "experience": [{"title": "Backend Engineer", "company": "Northwind",
                    "bullets": ["Cut p95 latency by 40%"]}],
    "projects": [],
    "education": {},
    "achievements": [],
}


class SentMail:
    """What `send()` was called with, instead of an SMTP connection."""

    def __init__(self):
        self.messages: list[dict] = []
        self.raise_limit_after: int | None = None

    async def send(self, to, subject, body_text, body_html=None, attachment_path=None):
        if self.raise_limit_after is not None and len(self.messages) >= self.raise_limit_after:
            raise cold_email_flow.DailyLimitReached("daily limit reached")
        self.messages.append({
            "to": to, "subject": subject, "body": body_text,
            "attachment": attachment_path,
        })
        return True


@pytest.fixture
def outbox(monkeypatch):
    box = SentMail()
    monkeypatch.setattr(cold_email_flow, "get_email_sender", lambda: box)
    return box


@pytest.fixture
def no_real_resume(monkeypatch):
    """Resume generation is exercised elsewhere; here it only matters *whether*
    it was asked for."""
    calls = []

    async def _fake(job_id, parsed_jd, session=None, user_id=None):
        calls.append({"job_id": job_id, "user_id": user_id,
                      "skills": list(parsed_jd.required_skills)})
        return "C:/fake/resume.pdf"

    monkeypatch.setattr(cold_email_flow, "generate_resume", _fake)
    return calls


@pytest.fixture
def owner(session, admin_user):
    """The sending account, with a complete profile."""
    session.exec(delete(ColdEmailLog).where(ColdEmailLog.user_id == admin_user.user_id))
    session.commit()
    profiles.save_profile(session, admin_user.user_id, PROFILE)
    yield admin_user
    session.exec(delete(ColdEmailLog).where(ColdEmailLog.user_id == admin_user.user_id))
    session.commit()


def _contact_file(tmp_path, rows):
    """A CSV with tech_stack filled in, so classification never calls an LLM."""
    path = tmp_path / "contacts.csv"
    header = "company,email,role,tech_stack,first_name\n"
    body = "".join(
        f"{r.get('company','')},{r.get('email','')},{r.get('role','')},"
        f"{r.get('tech_stack','python')},{r.get('first_name','')}\n"
        for r in rows
    )
    path.write_text(header + body, encoding="utf-8")
    return str(path)


def _config(path, owner, **kwargs):
    return ColdEmailConfig(excel_path=path, user_id=owner.user_id, **kwargs)


# ── The guard that matters most ─────────────────────────────────────────────

class TestIncompleteProfileSendsNothing:
    async def test_no_mail_leaves_the_building(
        self, session, admin_user, outbox, tmp_path
    ):
        """An email signed by nobody is worse than a failed run — and it cannot
        be recalled once it is out."""
        from backend.app.models.user_profile import UserProfile

        session.exec(delete(UserProfile).where(UserProfile.user_id == admin_user.user_id))
        session.commit()

        path = _contact_file(tmp_path, [{"company": "Acme", "email": "a@acme.com"}])
        result = await cold_email_flow.run_cold_email(_config(path, admin_user))

        assert outbox.messages == []
        assert result.emails_sent == 0
        assert result.errors and "profile" in result.errors[0].lower()

    async def test_it_fails_before_even_reading_the_contact_list(
        self, session, admin_user, outbox
    ):
        """Checked up front, not per row: the file path is deliberately bogus
        and the run still reports the profile problem, not a read error."""
        from backend.app.models.user_profile import UserProfile

        session.exec(delete(UserProfile).where(UserProfile.user_id == admin_user.user_id))
        session.commit()

        result = await cold_email_flow.run_cold_email(
            _config("C:/no/such/file.csv", admin_user)
        )
        assert "profile" in result.errors[0].lower()
        assert "read" not in result.errors[0].lower()


# ── What actually gets sent ─────────────────────────────────────────────────

class TestWhatGoesOut:
    async def test_one_email_per_contact(self, owner, outbox, no_real_resume, tmp_path):
        path = _contact_file(tmp_path, [
            {"company": "Acme", "email": "a@acme.com", "first_name": "Priya"},
            {"company": "Globex", "email": "b@globex.com", "first_name": "Ravi"},
        ])
        result = await cold_email_flow.run_cold_email(_config(path, owner))

        assert result.emails_sent == 2
        assert [m["to"] for m in outbox.messages] == ["a@acme.com", "b@globex.com"]

    async def test_the_body_is_the_senders_own(self, owner, outbox, no_real_resume, tmp_path):
        path = _contact_file(tmp_path, [
            {"company": "Acme", "email": "a@acme.com", "first_name": "Priya"},
        ])
        await cold_email_flow.run_cold_email(_config(path, owner))

        body = outbox.messages[0]["body"]
        assert "Asha Verma" in body
        assert "Cut p95 latency by 40%" in body
        assert "Acme" in body
        assert composer_opt_out() in body

    async def test_each_company_gets_its_own_wording(
        self, owner, outbox, no_real_resume, tmp_path
    ):
        """Forty byte-identical messages is the signal this whole design exists
        to avoid."""
        path = _contact_file(tmp_path, [
            {"company": c, "email": f"x@{c.lower()}.com"}
            for c in ("Acme", "Globex", "Initech", "Umbrella")
        ])
        await cold_email_flow.run_cold_email(_config(path, owner, batch_size=4))

        bodies = [m["body"] for m in outbox.messages]
        assert len(bodies) == 4
        assert len(set(bodies)) == 4

    async def test_rows_with_no_email_are_skipped_not_guessed(
        self, owner, outbox, no_real_resume, tmp_path
    ):
        path = _contact_file(tmp_path, [
            {"company": "Acme", "email": ""},
            {"company": "Globex", "email": "b@globex.com"},
        ])
        result = await cold_email_flow.run_cold_email(_config(path, owner))

        assert [m["to"] for m in outbox.messages] == ["b@globex.com"]
        assert result.emails_skipped == 1

    async def test_batch_size_is_a_hard_stop(self, owner, outbox, no_real_resume, tmp_path):
        path = _contact_file(tmp_path, [
            {"company": f"Co{i}", "email": f"c{i}@example.com"} for i in range(10)
        ])
        await cold_email_flow.run_cold_email(_config(path, owner, batch_size=3))
        assert len(outbox.messages) == 3


class TestOneBadRowCannotSinkTheRun:
    """A hand-maintained spreadsheet always has a blank row in it eventually."""

    @pytest.mark.parametrize("value", ["", "   ", "@", "@example.com", None])
    def test_name_extraction_never_raises(self, value):
        """`"".split()[0]` raised before the "there" fallback could be reached,
        so one empty cell aborted the whole campaign with an IndexError."""
        assert cold_email_flow._extract_first_name(value)

    @pytest.mark.parametrize("email,expected", [
        ("priya.sharma@acme.com", "Priya"),
        ("ravi_nair@acme.com", "Ravi"),
        ("asha-verma@acme.com", "Asha"),
        ("contact@acme.com", "Contact"),
        ("@acme.com", "there"),
    ])
    def test_name_extraction_still_works(self, email, expected):
        assert cold_email_flow._extract_first_name(email) == expected

    async def test_a_blank_row_costs_one_contact_not_the_run(
        self, owner, outbox, no_real_resume, tmp_path
    ):
        """The regression, end to end: the blank row is in the middle, and
        everything after it must still be mailed."""
        path = _contact_file(tmp_path, [
            {"company": "Acme", "email": "a@acme.com"},
            {"company": "", "email": ""},                  # the blank row
            {"company": "Globex", "email": "b@globex.com"},
        ])
        result = await cold_email_flow.run_cold_email(_config(path, owner, batch_size=5))

        assert [m["to"] for m in outbox.messages] == ["a@acme.com", "b@globex.com"]
        assert result.emails_sent == 2
        assert result.emails_skipped == 1

    async def test_a_whitespace_only_address_is_treated_as_blank(
        self, owner, outbox, no_real_resume, tmp_path
    ):
        path = _contact_file(tmp_path, [{"company": "Acme", "email": "   "}])
        result = await cold_email_flow.run_cold_email(_config(path, owner))
        assert outbox.messages == []
        assert result.emails_skipped == 1


class TestResumeHandling:
    async def test_attaches_when_there_is_no_public_link(
        self, owner, outbox, no_real_resume, tmp_path
    ):
        path = _contact_file(tmp_path, [{"company": "Acme", "email": "a@acme.com"}])
        await cold_email_flow.run_cold_email(_config(path, owner))

        assert outbox.messages[0]["attachment"] == "C:/fake/resume.pdf"
        assert no_real_resume, "a resume should have been generated"

    async def test_a_public_link_skips_generation_entirely(
        self, session, owner, outbox, no_real_resume, tmp_path
    ):
        """Not just 'does not attach' — it must not spend an LLM call and a
        LaTeX compile on a PDF nobody will receive."""
        profiles.save_profile(session, owner.user_id, {
            **PROFILE,
            "personal": {**PROFILE["personal"], "resume_url": "https://asha.dev/cv.pdf"},
        })
        path = _contact_file(tmp_path, [{"company": "Acme", "email": "a@acme.com"}])
        await cold_email_flow.run_cold_email(_config(path, owner))

        assert no_real_resume == []
        assert outbox.messages[0]["attachment"] is None
        assert "https://asha.dev/cv.pdf" in outbox.messages[0]["body"]

    async def test_the_resume_is_generated_for_the_sending_account(
        self, owner, outbox, no_real_resume, tmp_path
    ):
        """There is no Job row behind a cold-email resume, so the owner has to
        be passed explicitly or the profile lookup has nothing to go on."""
        path = _contact_file(tmp_path, [{"company": "Acme", "email": "a@acme.com"}])
        await cold_email_flow.run_cold_email(_config(path, owner))

        assert no_real_resume[0]["user_id"] == owner.user_id

    async def test_resume_skills_come_from_the_profile(
        self, owner, outbox, no_real_resume, tmp_path
    ):
        path = _contact_file(tmp_path, [{"company": "Acme", "email": "a@acme.com"}])
        await cold_email_flow.run_cold_email(_config(path, owner))

        assert set(no_real_resume[0]["skills"]) <= set(PROFILE["primary_skills"])

    async def test_one_resume_per_tech_stack_not_per_contact(
        self, owner, outbox, no_real_resume, tmp_path
    ):
        path = _contact_file(tmp_path, [
            {"company": "Acme", "email": "a@acme.com", "tech_stack": "python"},
            {"company": "Globex", "email": "b@globex.com", "tech_stack": "python"},
        ])
        await cold_email_flow.run_cold_email(_config(path, owner))
        assert len(no_real_resume) == 1


class TestDeduplication:
    async def test_the_same_contact_is_not_mailed_twice(
        self, owner, outbox, no_real_resume, tmp_path
    ):
        path = _contact_file(tmp_path, [{"company": "Acme", "email": "a@acme.com"}])
        await cold_email_flow.run_cold_email(_config(path, owner))
        await cold_email_flow.run_cold_email(_config(path, owner))

        assert len(outbox.messages) == 1

    async def test_a_send_is_recorded_against_the_sending_account(
        self, session, owner, outbox, no_real_resume, tmp_path
    ):
        path = _contact_file(tmp_path, [{"company": "Acme", "email": "a@acme.com"}])
        await cold_email_flow.run_cold_email(_config(path, owner))

        logs = session.exec(
            select(ColdEmailLog).where(ColdEmailLog.contact_email == "a@acme.com")
        ).all()
        assert len(logs) == 1
        assert logs[0].user_id == owner.user_id
        assert logs[0].status == EmailStatus.SENT


class TestDailyLimit:
    async def test_hitting_the_limit_stops_the_run(
        self, owner, outbox, no_real_resume, tmp_path
    ):
        outbox.raise_limit_after = 2
        path = _contact_file(tmp_path, [
            {"company": f"Co{i}", "email": f"c{i}@example.com"} for i in range(5)
        ])
        result = await cold_email_flow.run_cold_email(_config(path, owner, batch_size=5))

        assert len(outbox.messages) == 2
        assert result.emails_sent == 2

    async def test_skipped_counts_only_what_was_in_the_batch(
        self, owner, outbox, no_real_resume, tmp_path
    ):
        """It used to subtract from the whole file, reporting 497 skipped for a
        10-row batch of a 500-row list."""
        outbox.raise_limit_after = 2
        path = _contact_file(tmp_path, [
            {"company": f"Co{i}", "email": f"c{i}@example.com"} for i in range(50)
        ])
        result = await cold_email_flow.run_cold_email(_config(path, owner, batch_size=5))

        assert result.emails_sent == 2
        assert result.emails_skipped == 3          # the rest of the batch of 5
        assert result.emails_sent + result.emails_skipped == 5


class TestRunAccounting:
    async def test_the_run_is_recorded_and_completed(
        self, session, owner, outbox, no_real_resume, tmp_path
    ):
        path = _contact_file(tmp_path, [{"company": "Acme", "email": "a@acme.com"}])
        await cold_email_flow.run_cold_email(_config(path, owner))

        run = session.exec(
            select(RunLog).where(RunLog.user_id == owner.user_id)
            .order_by(RunLog.run_id.desc())
        ).first()
        assert run is not None
        assert run.completed_at is not None
        assert run.jobs_applied == 1

    async def test_an_unreadable_contact_list_is_reported_not_raised(
        self, owner, outbox, tmp_path
    ):
        result = await cold_email_flow.run_cold_email(
            _config(str(tmp_path / "missing.csv"), owner)
        )
        assert outbox.messages == []
        assert result.errors and "read" in result.errors[0].lower()


def composer_opt_out() -> str:
    from backend.app.services import cold_email_composer

    return cold_email_composer.opt_out_sentence()
