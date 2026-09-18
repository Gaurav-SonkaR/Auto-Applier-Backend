"""Cold email composed from the sender's own profile.

The body was a Python string literal with one person's name, phone, LinkedIn
and three achievements baked in. Two things were wrong with that: nobody else
could use the feature at all, and every recipient got a byte-identical message,
which is one of the strongest signals a spam filter has.

These tests pin down that nothing about the sender is hard-coded any more, that
the wording varies per company while the *claims* never do, and the
deliverability guardrails (no attachment when a link exists, an opt-out line, a
warm-up ramp).
"""
from __future__ import annotations

import pytest

from backend.app.services import cold_email_composer as composer

SUBJECT_TEMPLATE = "Experienced {role} | Open to Opportunities at {company}"

PROFILE = {
    "personal": {
        "name": "Asha Verma",
        "email": "asha@example.com",
        "phone": "+91-9000000000",
        "linkedin": "linkedin.com/in/asha",
        "github": "github.com/asha",
        "kaggle": "",
        "location": "Pune",
        "resume_url": "",
    },
    "primary_skills": ["Python", "Django", "FastAPI", "PostgreSQL", "Docker"],
    "secondary_skills": ["Kubernetes"],
    "experience": [{
        "title": "Backend Engineer",
        "company": "Northwind",
        "bullets": ["Cut p95 latency by 40%", "Shipped 30 REST endpoints", "Ran the on-call rota"],
    }],
    "projects": [],
    "education": {},
    "achievements": ["Promoted within a year"],
}


def _compose(profile=PROFILE, **kwargs):
    defaults = dict(
        company="Acme Corp", first_name="Priya", role="Backend Developer",
        tech_area="Python/Django", subject_template=SUBJECT_TEMPLATE,
    )
    defaults.update(kwargs)
    return composer.compose(profile, **defaults)


class TestNothingIsHardCoded:
    def test_uses_the_senders_name_not_a_literal(self):
        _, body = _compose()
        assert "Asha Verma" in body
        assert "Gaurav" not in body

    def test_uses_the_senders_contact_details(self):
        _, body = _compose()
        assert "+91-9000000000" in body
        assert "asha@example.com" in body
        assert "linkedin.com/in/asha" in body

    def test_uses_the_senders_own_bullets(self):
        _, body = _compose()
        assert "Cut p95 latency by 40%" in body
        assert "Divyal" not in body

    def test_a_different_profile_produces_a_different_email(self):
        """The real test of 'not hard-coded' — swap the person, swap the email."""
        other = {
            **PROFILE,
            "personal": {**PROFILE["personal"], "name": "Ravi Nair", "email": "ravi@example.com",
                         "phone": "+91-9111111111", "linkedin": "linkedin.com/in/ravi"},
            "experience": [{"title": "Data Engineer", "company": "Contoso",
                            "bullets": ["Built an ETL pipeline"]}],
        }
        _, mine = _compose()
        _, theirs = _compose(other)
        assert "Ravi Nair" in theirs and "Asha Verma" not in theirs
        assert "Built an ETL pipeline" in theirs
        assert mine != theirs

    def test_refuses_to_send_without_a_name(self):
        blank = {**PROFILE, "personal": {**PROFILE["personal"], "name": ""}}
        with pytest.raises(composer.ComposeError, match="name"):
            _compose(blank)

    def test_refuses_to_send_without_an_email(self):
        blank = {**PROFILE, "personal": {**PROFILE["personal"], "email": ""}}
        with pytest.raises(composer.ComposeError, match="email"):
            _compose(blank)

    def test_missing_requirements_lists_what_to_fix(self):
        empty = {"personal": {}, "experience": [], "achievements": []}
        missing = composer.missing_requirements(empty)
        assert any("name" in m for m in missing)
        assert any("email" in m for m in missing)
        assert any("bullet" in m or "achievement" in m for m in missing)

    def test_a_complete_profile_has_nothing_missing(self):
        assert composer.missing_requirements(PROFILE) == []


class TestPerCompanyVariation:
    def test_two_companies_do_not_get_identical_bodies(self):
        """Bulk-identical content is a strong spam signal on its own."""
        _, a = _compose(company="Acme Corp")
        _, b = _compose(company="Globex")
        assert a != b

    def test_the_same_company_always_gets_the_same_wording(self):
        """Stable, so a resend after a bounce doesn't read as a different
        person writing — and so tests aren't flaky."""
        assert _compose(company="Acme Corp")[1] == _compose(company="Acme Corp")[1]

    def test_wording_survives_a_process_restart(self):
        """Python randomises str hashing per process, so `hash()` would give the
        same company different wording after every restart."""
        import subprocess
        import sys

        script = (
            "from backend.app.services import cold_email_composer as c;"
            "print(c._pick(c._OPENERS, 'Acme Corp'))"
        )
        runs = {
            subprocess.run([sys.executable, "-c", script], capture_output=True,
                           text=True, check=True).stdout.strip()
            for _ in range(2)
        }
        assert len(runs) == 1
        assert runs.pop() == composer._pick(composer._OPENERS, "Acme Corp")

    def test_the_company_and_role_actually_appear(self):
        subject, body = _compose(company="Globex", role="Platform Engineer")
        assert "Globex" in subject and "Globex" in body
        assert "Platform Engineer" in subject and "Platform Engineer" in body

    def test_the_recipient_is_greeted_by_name(self):
        _, body = _compose(first_name="Priya")
        assert body.startswith("Hi Priya,")

    def test_an_unknown_recipient_still_reads_naturally(self):
        _, body = _compose(first_name="")
        assert body.startswith("Hi there,")

    def test_claims_do_not_vary_even_though_wording_does(self):
        """Varying the phrasing is fine; varying what the sender claims to have
        done would be inventing a CV per recipient."""
        for company in ("Acme Corp", "Globex", "Initech", "Umbrella"):
            _, body = _compose(company=company)
            assert "Cut p95 latency by 40%" in body


class TestDeliverability:
    def test_every_email_carries_an_opt_out(self):
        _, body = _compose()
        assert composer.opt_out_sentence() in body

    def test_links_the_resume_when_a_public_url_exists(self):
        with_url = {**PROFILE,
                    "personal": {**PROFILE["personal"], "resume_url": "https://asha.dev/cv.pdf"}}
        _, body = _compose(with_url, resume_attached=True)
        assert "https://asha.dev/cv.pdf" in body
        assert "attached" not in body.lower()

    def test_falls_back_to_the_attachment_when_there_is_nowhere_to_link(self):
        _, body = _compose(resume_attached=True)
        assert "attached my resume" in body.lower()

    def test_says_nothing_about_a_resume_when_there_is_neither(self):
        _, body = _compose(resume_attached=False)
        assert "resume" not in body.lower()

    def test_warns_about_attaching_a_pdf_to_a_stranger(self):
        warnings = composer.spam_warnings(PROFILE, attaching_resume=True)
        assert any("spam signal" in w for w in warnings)

    def test_no_attachment_warning_once_a_link_is_set(self):
        with_url = {**PROFILE,
                    "personal": {**PROFILE["personal"], "resume_url": "https://asha.dev/cv.pdf"}}
        warnings = composer.spam_warnings(with_url, attaching_resume=False)
        assert not any("spam signal" in w for w in warnings)

    def test_warns_when_the_signature_would_be_bare(self):
        bare = {**PROFILE, "personal": {**PROFILE["personal"], "phone": "", "linkedin": ""}}
        warnings = composer.spam_warnings(bare, attaching_resume=False)
        assert any("phone" in w for w in warnings)
        assert any("LinkedIn" in w for w in warnings)

    def test_body_is_plain_text(self):
        """HTML from an unknown sender scores worse, and nothing here needs
        formatting."""
        _, body = _compose()
        assert "<" not in body and "</" not in body


class TestClaimsMatchTheProfile:
    def test_tech_area_only_names_skills_the_sender_has(self):
        area = composer.tech_area_from_profile(PROFILE, "python")
        assert "Django" in area or "Python" in area
        for claim in ("Rails", "Spring Boot", ".NET"):
            assert claim not in area

    def test_an_unmatched_stack_describes_what_they_do_have(self):
        """The old hard-coded map would happily tell a Ruby shop the sender
        knew Rails."""
        area = composer.tech_area_from_profile(PROFILE, "ruby")
        assert "Rails" not in area and "Ruby" not in area
        assert "Python" in area

    def test_empty_profile_falls_back_to_something_sayable(self):
        assert composer.tech_area_from_profile({}, "python") == "backend systems"

    def test_resume_skills_come_from_the_profile(self):
        skills = composer.parsed_jd_skills(PROFILE, "python")
        assert set(skills) <= set(PROFILE["primary_skills"])

    def test_matching_skills_are_ordered_first(self):
        skills = composer.parsed_jd_skills(PROFILE, "python")
        assert skills[0] in ("Python", "Django", "FastAPI")

    def test_no_skills_yields_no_claims(self):
        assert composer.parsed_jd_skills({"primary_skills": []}, "python") == []

    def test_bullets_fall_back_to_achievements(self):
        no_bullets = {**PROFILE, "experience": [{"title": "Backend Engineer", "bullets": []}]}
        assert composer.achievement_bullets(no_bullets) == ["Promoted within a year"]

    def test_bullets_are_capped(self):
        many = {**PROFILE, "experience": [{"title": "X", "bullets": [f"b{i}" for i in range(10)]}]}
        assert len(composer.achievement_bullets(many)) == composer.MAX_BULLETS


class TestPreviewEndpoint:
    def test_shows_the_real_body(self, client, session, admin_user):
        from backend.app.services import profiles

        profiles.save_profile(session, admin_user.user_id, PROFILE)
        body = client.get("/api/cold-email/preview").json()

        assert body["can_send"] is True
        assert "Asha Verma" in body["body"]
        assert body["subject"]
        assert composer.opt_out_sentence() in body["body"]

    def test_reports_what_is_missing_instead_of_a_body(
        self, client, session, admin_user
    ):
        from sqlmodel import delete

        from backend.app.models.user_profile import UserProfile

        session.exec(delete(UserProfile).where(UserProfile.user_id == admin_user.user_id))
        session.commit()

        body = client.get("/api/cold-email/preview").json()
        assert body["can_send"] is False
        assert body["missing"]
        assert body["body"] == ""

    def test_reports_the_attachment_warning(self, client, session, admin_user):
        from backend.app.services import profiles

        profiles.save_profile(session, admin_user.user_id, PROFILE)
        body = client.get("/api/cold-email/preview").json()
        assert body["attaches_resume"] is True
        assert any("spam signal" in w for w in body["warnings"])

    def test_a_resume_link_removes_the_attachment(self, client, session, admin_user):
        from backend.app.services import profiles

        profiles.save_profile(session, admin_user.user_id, {
            **PROFILE,
            "personal": {**PROFILE["personal"], "resume_url": "https://asha.dev/cv.pdf"},
        })
        body = client.get("/api/cold-email/preview").json()
        assert body["attaches_resume"] is False
        assert "https://asha.dev/cv.pdf" in body["body"]

    def test_preview_cannot_disagree_with_what_a_run_would_send(
        self, client, session, admin_user
    ):
        """The endpoint used to accept a `subject_template` that `/start` has no
        field for, so a caller could preview one subject and send another. It
        now takes no parameters, and the subject it shows is the one a run
        actually uses."""
        from backend.app.services import profiles
        from backend.app.workflows.state import DEFAULT_SUBJECT_TEMPLATE, ColdEmailConfig

        profiles.save_profile(session, admin_user.user_id, PROFILE)

        # Passing one anyway must be ignored, not honoured.
        shown = client.get("/api/cold-email/preview",
                           params={"subject_template": "HIJACKED {role}"}).json()["subject"]
        assert "HIJACKED" not in shown

        expected = DEFAULT_SUBJECT_TEMPLATE.format(
            role="Backend Developer", company="Acme Corp")
        assert shown == expected
        assert ColdEmailConfig(excel_path="x").subject_template == DEFAULT_SUBJECT_TEMPLATE

    def test_requires_auth(self, anon_client):
        assert anon_client.get("/api/cold-email/preview").status_code == 401


class TestWarmUp:
    """A new sending address that emits 40 messages on day one is treated as a
    bulk sender before anyone has replied to anything."""

    @pytest.fixture
    def sender(self, monkeypatch):
        from backend.app.services.email_sender import EmailSender

        s = EmailSender()
        monkeypatch.setattr(s._settings, "cold_email_daily_limit", 40, raising=False)
        monkeypatch.setattr(s._settings, "cold_email_warmup_days", 14, raising=False)
        return s

    def _on_day(self, sender, monkeypatch, day: int) -> int:
        monkeypatch.setattr(sender, "_day_of_sending", lambda: day)
        return sender.effective_daily_limit()

    def test_first_day_is_far_below_the_configured_cap(self, sender, monkeypatch):
        assert self._on_day(sender, monkeypatch, 1) < 40

    def test_never_ramps_below_a_usable_floor(self, sender, monkeypatch):
        from backend.app.services.email_sender import WARMUP_FLOOR

        assert self._on_day(sender, monkeypatch, 1) >= WARMUP_FLOOR

    def test_rises_over_time(self, sender, monkeypatch):
        limits = [self._on_day(sender, monkeypatch, d) for d in (1, 5, 10, 14)]
        assert limits == sorted(limits)
        assert limits[0] < limits[-1]

    def test_reaches_the_configured_cap_at_the_end_of_the_ramp(self, sender, monkeypatch):
        assert self._on_day(sender, monkeypatch, 14) == 40

    def test_never_exceeds_the_configured_cap(self, sender, monkeypatch):
        assert self._on_day(sender, monkeypatch, 400) == 40

    @pytest.mark.parametrize("configured", [1, 2, 3, 4])
    def test_never_raises_a_cap_set_below_the_floor(
        self, sender, monkeypatch, configured
    ):
        """A user who deliberately sets a cap of 2 must get 2. The floor is on
        the ramp, not on their limit — applying it last silently sent 5."""
        monkeypatch.setattr(sender._settings, "cold_email_daily_limit", configured,
                            raising=False)
        assert self._on_day(sender, monkeypatch, 1) == configured

    def test_a_low_cap_stays_capped_on_every_day_of_the_ramp(self, sender, monkeypatch):
        monkeypatch.setattr(sender._settings, "cold_email_daily_limit", 3, raising=False)
        assert {self._on_day(sender, monkeypatch, d) for d in range(1, 20)} == {3}

    def test_ramp_can_be_switched_off(self, sender, monkeypatch):
        monkeypatch.setattr(sender._settings, "cold_email_warmup_days", 0, raising=False)
        assert self._on_day(sender, monkeypatch, 1) == 40

    def test_remaining_today_respects_the_ramp(self, sender, monkeypatch):
        monkeypatch.setattr(sender, "_day_of_sending", lambda: 1)
        assert sender.remaining_today() == sender.effective_daily_limit()

    def test_day_one_when_nothing_has_ever_been_sent(self, sender, monkeypatch):
        monkeypatch.setattr(sender, "_first_send_date", lambda: None)
        assert sender._day_of_sending() == 1

    def test_the_ramp_is_measured_from_the_first_send_not_from_startup(
        self, sender, monkeypatch
    ):
        """Tracked in memory, a restart would hand the sender a fresh day one
        and undo the whole point of the ramp."""
        from datetime import date, timedelta

        monkeypatch.setattr(sender, "_first_send_date", lambda: date.today() - timedelta(days=6))
        assert sender._day_of_sending() == 7
