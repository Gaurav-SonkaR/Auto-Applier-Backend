"""Phase 4: the uploaded-resume library.

Backs two user-facing features:
  * portal search driven by the master resume instead of a typed keyword
  * best-resume selection when no tailored resume could be generated
"""
import io
import json

import pytest
from sqlmodel import delete, select

from backend.app.models.resume import Resume
from backend.app.services import resume_library


def make_pdf(text: str) -> bytes:
    """A minimal single-page PDF whose text layer pypdf can extract."""
    from pypdf import PdfWriter

    # Build with reportlab if available; otherwise hand-roll a content stream.
    lines = text.split("\n")
    content = "BT /F1 10 Tf 40 750 Td 12 TL\n"
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += f"({escaped}) Tj T*\n"
    content += "ET"
    stream = content.encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()

    # Round-trip through pypdf so the fixture fails loudly if it is malformed.
    PdfWriter(clone_from=io.BytesIO(bytes(out)))
    return bytes(out)


BACKEND_RESUME = make_pdf(
    "Gaurav Sonkar\nSenior Backend Engineer\n"
    "Skills: Python, Django, FastAPI, PostgreSQL, Docker, AWS, Redis"
)
FRONTEND_RESUME = make_pdf(
    "Gaurav Sonkar\nFrontend Developer\n"
    "Skills: JavaScript, TypeScript, React, Vue, Tailwind CSS"
)


@pytest.fixture
def clean_library(session):
    session.exec(delete(Resume))
    session.commit()
    yield session
    session.exec(delete(Resume))
    session.commit()


@pytest.fixture
def upload(client, clean_library):
    def _upload(content: bytes, filename="resume.pdf", label="", is_master=False):
        return client.post(
            "/api/resumes/library",
            files={"file": (filename, content, "application/pdf")},
            data={"label": label, "is_master": str(is_master).lower()},
        )
    return _upload


class TestExtraction:
    def test_reads_text_and_page_count(self, tmp_path):
        p = tmp_path / "r.pdf"
        p.write_bytes(BACKEND_RESUME)
        text, pages = resume_library.extract_text(str(p))
        assert pages == 1
        assert "Django" in text

    def test_finds_skills_from_vocabulary(self):
        skills = resume_library.extract_skills(
            "Experienced with Python, Django and PostgreSQL",
            vocabulary={"python", "django", "postgresql", "react"},
        )
        assert set(skills) == {"python", "django", "postgresql"}

    def test_matching_is_word_boundary_aware(self):
        """'go' must not match 'google', 'r' must not match every word."""
        skills = resume_library.extract_skills(
            "Worked at Google on research", vocabulary={"go", "r", "research"}
        )
        assert "go" not in skills
        assert "r" not in skills

    def test_handles_dotted_and_plus_skills(self):
        skills = resume_library.extract_skills(
            "Built with React.js and C++", vocabulary={"react.js", "c++", "react"}
        )
        assert "react.js" in skills
        assert "c++" in skills

    @pytest.mark.parametrize("text,expected", [
        ("Gaurav Sonkar\nSenior Backend Engineer", "Senior Backend Engineer"),
        ("Jane Doe\nData Scientist\n", "Data Scientist"),
        ("Full Stack Developer", "Full Stack Developer"),
    ])
    def test_extracts_title_from_headline(self, text, expected):
        assert resume_library.extract_title(text).lower() == expected.lower()

    def test_title_falls_back_when_absent(self):
        assert resume_library.extract_title("Gaurav Sonkar\nsome prose") == "Software Engineer"

    def test_only_searches_the_top_of_the_document(self):
        """A title deep in the experience section must not win over the headline."""
        text = "Gaurav Sonkar\nBackend Engineer\n" + ("filler\n" * 60) + "Principal Architect\n"
        assert "principal" not in resume_library.extract_title(text).lower()


class TestUploadEndpoint:
    def test_uploads_and_extracts(self, upload):
        r = upload(BACKEND_RESUME, "backend.pdf", label="Backend")
        assert r.status_code == 201
        body = r.json()["resume"]
        assert body["label"] == "Backend"
        assert body["page_count"] == 1
        assert "python" in body["skills"]
        assert "django" in body["skills"]

    def test_first_upload_becomes_master_automatically(self, upload):
        """Otherwise resume-driven search stays silently off until the user
        finds a toggle nobody told them about."""
        assert upload(BACKEND_RESUME).json()["resume"]["is_master"] is True

    def test_second_upload_does_not_steal_master(self, upload):
        upload(BACKEND_RESUME, "a.pdf")
        assert upload(FRONTEND_RESUME, "b.pdf").json()["resume"]["is_master"] is False

    def test_rejects_non_pdf(self, upload):
        assert upload(b"not a pdf", "resume.docx").status_code == 422

    def test_rejects_empty_file(self, upload):
        assert upload(b"", "resume.pdf").status_code == 422

    def test_rejects_unreadable_pdf(self, upload):
        assert upload(b"%PDF-1.4 garbage", "resume.pdf").status_code == 422

    def test_sanitises_the_stored_filename(self, upload, session):
        """A traversal attempt in the upload name must not escape the library dir."""
        r = upload(BACKEND_RESUME, "../../evil.pdf")
        assert r.status_code == 201
        stored = session.get(Resume, r.json()["resume"]["resume_id"])
        assert ".." not in stored.file_path
        assert "library" in stored.file_path.replace("\\", "/")


class TestMasterSelection:
    def test_promoting_demotes_the_previous_master(self, upload, client, session):
        first = upload(BACKEND_RESUME, "a.pdf").json()["resume"]["resume_id"]
        second = upload(FRONTEND_RESUME, "b.pdf").json()["resume"]["resume_id"]

        assert client.patch(f"/api/resumes/library/{second}/master").status_code == 200

        masters = session.exec(select(Resume).where(Resume.is_master == True)).all()  # noqa: E712
        assert [m.resume_id for m in masters] == [second]
        session.refresh(session.get(Resume, first))
        assert session.get(Resume, first).is_master is False

    def test_promoting_unknown_resume_is_404(self, client, clean_library):
        assert client.patch("/api/resumes/library/999999/master").status_code == 404

    def test_listing_reports_the_master(self, upload, client):
        rid = upload(BACKEND_RESUME).json()["resume"]["resume_id"]
        body = client.get("/api/resumes/library").json()
        assert body["master_resume_id"] == rid
        assert body["total"] == 1


class TestSearchTermsFromMaster:
    def test_returns_title_and_skills(self, upload, session):
        upload(BACKEND_RESUME)
        title, skills = resume_library.search_terms_from_master(session)
        assert "engineer" in title.lower()
        assert "python" in skills

    def test_none_when_library_is_empty(self, clean_library):
        assert resume_library.search_terms_from_master(clean_library) is None


class TestResolveSearchQuery:
    """Blank job_title on a portal run means 'search from my master resume'."""

    def test_typed_title_always_wins(self, upload):
        from backend.app.workflows.portal_apply_flow import resolve_search_query

        upload(BACKEND_RESUME)
        query, source = resolve_search_query("Data Engineer")
        assert query == "Data Engineer"
        assert source == "typed"

    def test_blank_title_uses_the_master_resume(self, upload):
        from backend.app.workflows.portal_apply_flow import resolve_search_query

        upload(BACKEND_RESUME)
        query, source = resolve_search_query("")
        assert source == "master_resume"
        assert "engineer" in query.lower()

    def test_falls_back_to_profile_when_no_resume(self, clean_library):
        from backend.app.workflows.portal_apply_flow import resolve_search_query

        query, source = resolve_search_query("")
        assert source in {"master_profile", "default"}
        assert query


class TestBestResumeForJd:
    def test_picks_the_closer_match(self, upload, session):
        upload(BACKEND_RESUME, "backend.pdf", label="Backend")
        upload(FRONTEND_RESUME, "frontend.pdf", label="Frontend")

        backend_jd = ["python", "django", "postgresql"]
        assert resume_library.best_for_jd(session, backend_jd).label == "Backend"

        frontend_jd = ["react", "typescript", "tailwind css"]
        assert resume_library.best_for_jd(session, frontend_jd).label == "Frontend"

    def test_scores_are_proportions(self, upload, session):
        upload(BACKEND_RESUME, "backend.pdf")
        ranked = resume_library.rank_for_jd(session, ["python", "django", "cobol", "fortran"])
        assert ranked[0].score == 0.5
        assert set(ranked[0].matched) == {"python", "django"}

    def test_ties_break_toward_the_master(self, upload, client, session):
        a = upload(BACKEND_RESUME, "a.pdf", label="A").json()["resume"]["resume_id"]
        upload(BACKEND_RESUME, "b.pdf", label="B")
        client.patch(f"/api/resumes/library/{a}/master")

        # No JD terms at all — every score is 0, so the master must win.
        assert resume_library.best_for_jd(session, []).resume_id == a

    def test_none_when_library_is_empty(self, clean_library):
        assert resume_library.best_for_jd(clean_library, ["python"]) is None


class TestMatchEndpoint:
    def test_ranks_library_against_a_job(self, client, upload, make_job):
        upload(BACKEND_RESUME, "backend.pdf", label="Backend")
        upload(FRONTEND_RESUME, "frontend.pdf", label="Frontend")
        job_id = make_job(parsed_jd=json.dumps({
            "required_skills": ["Python", "Django"], "keywords": ["PostgreSQL"],
        }))

        body = client.get(f"/api/resumes/match/{job_id}").json()
        assert body["job_id"] == job_id
        assert set(body["jd_terms"]) == {"python", "django", "postgresql"}
        assert body["matches"][0]["resume"]["label"] == "Backend"
        assert body["matches"][0]["score"] == 1.0

    def test_job_without_parsed_jd_still_responds(self, client, upload, make_job):
        upload(BACKEND_RESUME)
        body = client.get(f"/api/resumes/match/{make_job()}").json()
        assert body["jd_terms"] == []
        assert len(body["matches"]) == 1

    def test_unknown_job_is_404(self, client, clean_library):
        assert client.get("/api/resumes/match/999999").status_code == 404


class TestDeletion:
    def test_removes_row_and_file(self, upload, client, session, tmp_path):
        from pathlib import Path

        rid = upload(BACKEND_RESUME).json()["resume"]["resume_id"]
        path = Path(session.get(Resume, rid).file_path)
        assert path.exists()

        assert client.delete(f"/api/resumes/library/{rid}").status_code == 204
        assert session.get(Resume, rid) is None
        assert not path.exists()

    def test_unknown_resume_is_404(self, client, clean_library):
        assert client.delete("/api/resumes/library/999999").status_code == 404


class TestRouteOrdering:
    def test_library_is_not_shadowed_by_the_job_id_route(self, client, clean_library):
        """/api/resumes/{job_id} takes an int — if it were declared first,
        /api/resumes/library would 422 instead of listing."""
        assert client.get("/api/resumes/library").status_code == 200

    def test_generated_resume_route_still_works(self, client, make_job):
        """404 (no resume generated) proves it routed here, not to the library."""
        assert client.get(f"/api/resumes/{make_job()}").status_code == 404
