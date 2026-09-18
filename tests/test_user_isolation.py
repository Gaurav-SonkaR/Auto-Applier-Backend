"""Per-user data isolation.

The single most important property of going multi-user: one account must not
see, download, modify or even confirm the existence of another's data. Every
row was global before signup existed, so this is the boundary that had to be
added everywhere at once — and the one most likely to be silently missed on a
future endpoint.

Each test drives two real accounts through the real API.
"""
import pytest
from sqlmodel import Session, delete

from backend.app.database import engine
from backend.app.models.job import Job, JobStatus
from backend.app.models.resume import Resume
from backend.app.models.run_log import RunLog, RunType
from backend.app.models.user import User, UserRole, UserStatus
from backend.app.services import users as user_service

OTHER_USERNAME = "second-account"
OTHER_PASSWORD = "second-password-123"


@pytest.fixture
def other_user(anon_client):
    """A second ACTIVE account, independent of the suite's admin."""
    with Session(engine) as session:
        session.exec(delete(User).where(User.username == OTHER_USERNAME))
        session.commit()
        user = user_service.create_user(
            session, OTHER_USERNAME, OTHER_PASSWORD,
            status=UserStatus.ACTIVE, role=UserRole.USER,
        )
        session.expunge(user)

    yield user

    with Session(engine) as session:
        session.exec(delete(Job).where(Job.user_id == user.user_id))
        session.exec(delete(Resume).where(Resume.user_id == user.user_id))
        session.exec(delete(RunLog).where(RunLog.user_id == user.user_id))
        session.exec(delete(User).where(User.user_id == user.user_id))
        session.commit()


@pytest.fixture
def other_headers(anon_client, other_user):
    r = anon_client.post(
        "/api/auth/login",
        json={"username": OTHER_USERNAME, "password": OTHER_PASSWORD},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def other_job(other_user):
    """A job belonging to the *other* account."""
    with Session(engine) as session:
        job = Job(
            user_id=other_user.user_id,
            url="https://example.com/private/other-user-job",
            title="Other User's Secret Job",
            company="OtherCorp",
            portal="linkedin",
            status=JobStatus.SCRAPED,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        session.expunge(job)
    return job


class TestJobIsolation:
    def test_job_list_excludes_other_users_jobs(self, client, other_job):
        items = client.get("/api/jobs/", params={"limit": 200}).json()["items"]
        assert all(j["job_id"] != other_job.job_id for j in items)
        assert all("Other User's Secret Job" != j["title"] for j in items)

    def test_cannot_fetch_another_users_job(self, client, other_job):
        """404, not 403 — a 403 would confirm the id exists and let anyone
        enumerate the table."""
        assert client.get(f"/api/jobs/{other_job.job_id}").status_code == 404

    def test_cannot_read_another_users_timeline(self, client, other_job):
        assert client.get(f"/api/jobs/{other_job.job_id}/timeline").status_code == 404

    def test_cannot_retry_another_users_job(self, client, other_job):
        assert client.post(f"/api/jobs/{other_job.job_id}/retry").status_code == 404

    def test_cannot_change_another_users_job_status(self, client, other_job):
        r = client.patch(f"/api/jobs/{other_job.job_id}/status", json={"status": "APPLIED"})
        assert r.status_code == 404

    def test_cannot_set_outcome_on_another_users_job(self, client, other_job):
        r = client.patch(f"/api/jobs/{other_job.job_id}/outcome", json={"outcome": "REJECTED"})
        assert r.status_code == 404

    def test_cannot_download_another_users_generated_resume(self, client, other_job):
        assert client.get(f"/api/resumes/{other_job.job_id}").status_code == 404

    def test_cannot_read_another_users_screenshot(self, client, other_job):
        assert client.get(f"/api/jobs/{other_job.job_id}/screenshot").status_code == 404

    def test_the_other_user_can_see_their_own_job(self, anon_client, other_headers, other_job):
        """The isolation must not be so broad that it hides a user's own data."""
        r = anon_client.get(f"/api/jobs/{other_job.job_id}", headers=other_headers)
        assert r.status_code == 200
        assert r.json()["title"] == "Other User's Secret Job"

    def test_same_url_can_be_tracked_by_both_users(self, client, other_user, make_job):
        """Job.url used to be globally unique, which would have stopped a second
        user from ever scraping a posting the first already had."""
        url = "https://example.com/shared-posting"
        make_job(url=url)
        with Session(engine) as session:
            session.add(Job(
                user_id=other_user.user_id, url=url, title="Same posting, other user",
                portal="linkedin", status=JobStatus.SCRAPED,
            ))
            session.commit()   # must not raise on the unique index


class TestDashboardIsolation:
    def test_stats_count_only_your_own_jobs(self, client, other_job, make_job):
        make_job()
        stats = client.get("/api/dashboard/stats").json()
        mine = client.get("/api/jobs/", params={"limit": 200}).json()["total"]
        assert stats["total_jobs"] == mine

    def test_human_queue_excludes_other_users(self, client, other_user):
        with Session(engine) as session:
            session.add(Job(
                user_id=other_user.user_id, url="https://example.com/other-stuck",
                title="Other stuck job", portal="linkedin",
                status=JobStatus.WAITING_CAPTCHA,
            ))
            session.commit()
        items = client.get("/api/dashboard/human-queue").json()["items"]
        assert all(j["title"] != "Other stuck job" for j in items)


class TestResumeIsolation:
    @pytest.fixture
    def other_resume(self, other_user, tmp_path):
        pdf = tmp_path / "other.pdf"
        pdf.write_bytes(b"%PDF-1.4 placeholder")
        with Session(engine) as session:
            resume = Resume(
                user_id=other_user.user_id, filename="other.pdf",
                file_path=str(pdf), label="Other user's resume", is_master=True,
            )
            session.add(resume)
            session.commit()
            session.refresh(resume)
            session.expunge(resume)
        return resume

    def test_library_excludes_other_users_resumes(self, client, other_resume):
        items = client.get("/api/resumes/library").json()["items"]
        assert all(r["resume_id"] != other_resume.resume_id for r in items)

    def test_cannot_download_another_users_resume(self, client, other_resume):
        r = client.get(f"/api/resumes/library/{other_resume.resume_id}/download")
        assert r.status_code == 404

    def test_cannot_delete_another_users_resume(self, client, other_resume):
        assert client.delete(f"/api/resumes/library/{other_resume.resume_id}").status_code == 404
        with Session(engine) as session:
            assert session.get(Resume, other_resume.resume_id) is not None

    def test_cannot_promote_another_users_resume(self, client, other_resume):
        r = client.patch(f"/api/resumes/library/{other_resume.resume_id}/master")
        assert r.status_code == 404

    def test_each_user_keeps_their_own_master(self, client, other_resume, session):
        """`is_master` is one-per-user. Promoting mine must not demote theirs."""
        from tests.test_resume_library import BACKEND_RESUME

        r = client.post(
            "/api/resumes/library",
            files={"file": ("mine.pdf", BACKEND_RESUME, "application/pdf")},
            data={"label": "Mine", "is_master": "true"},
        )
        assert r.status_code == 201
        session.expire_all()
        assert session.get(Resume, other_resume.resume_id).is_master is True


class TestRunIsolation:
    @pytest.fixture
    def other_run(self, other_user):
        with Session(engine) as session:
            run = RunLog(user_id=other_user.user_id, run_type=RunType.PORTAL_APPLY)
            session.add(run)
            session.commit()
            session.refresh(run)
            session.expunge(run)
        return run

    def test_run_list_excludes_other_users_runs(self, client, other_run):
        runs = client.get("/api/runs/").json()
        assert all(r["run_id"] != other_run.run_id for r in runs)

    def test_cannot_fetch_another_users_run(self, client, other_run):
        assert client.get(f"/api/runs/{other_run.run_id}").status_code == 404

    def test_cannot_stop_another_users_run(self, client, other_run):
        assert client.post(f"/api/runs/{other_run.run_id}/stop").status_code == 404


class TestColdEmailIsolation:
    def test_stats_count_only_your_own_emails(self, client, other_user):
        """The other account's SENT row must not appear in my totals."""
        from backend.app.models.cold_email import ColdEmailLog, EmailStatus

        before = client.get("/api/cold-email/stats").json()

        with Session(engine) as session:
            session.add(ColdEmailLog(
                user_id=other_user.user_id, company_name="OtherCorp",
                contact_email="them@othercorp.com", status=EmailStatus.SENT,
            ))
            session.commit()

        after = client.get("/api/cold-email/stats").json()
        assert after["total"] == before["total"]
        assert after["sent"] == before["sent"]
