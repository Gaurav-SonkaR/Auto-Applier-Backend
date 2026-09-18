"""Generated resumes: local by default, S3 when configured.

No AWS here. The S3 client is a recorder, and one test builds a real boto3
client against a fake endpoint to prove the presigned URL is actually signed
for the object and the download disposition — both without a network call.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from backend.app.services import storage
from backend.app.services.storage import (
    LocalStore,
    S3Store,
    StorageConfigError,
    parse_s3_ref,
    resume_key,
)


class RecordingS3:
    """Stands in for a boto3 S3 client."""

    def __init__(self, fail_upload: bool = False):
        self.uploads: list[dict] = []
        self.presigned: list[dict] = []
        self.fail_upload = fail_upload

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        if self.fail_upload:
            raise ConnectionError("S3 unreachable")
        self.uploads.append({"file": filename, "bucket": bucket, "key": key,
                             "extra": ExtraArgs or {}})

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append({"op": op, "params": Params, "ttl": ExpiresIn})
        return f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}?sig=abc"


@pytest.fixture(autouse=True)
def fresh_store():
    storage.reset_store_for_tests()
    yield
    storage.reset_store_for_tests()


# ── Keys ────────────────────────────────────────────────────────────────────

class TestKeys:
    def test_keys_are_per_account_and_per_job(self):
        assert resume_key(7, 42, "Asha_Verma_Backend.pdf") == \
            "users/7/jobs/42/Asha_Verma_Backend.pdf"

    def test_two_accounts_on_the_same_posting_never_share_a_key(self):
        assert resume_key(1, 5, "r.pdf") != resume_key(2, 5, "r.pdf")

    def test_unsafe_filename_characters_are_replaced(self):
        key = resume_key(1, 1, "../../etc/pass wd?.pdf")
        name = key.split("/jobs/1/")[1]
        assert key.count("/") == 4            # nothing added a path segment
        assert not name.startswith(".") and ".." not in name
        assert " " not in name and "?" not in name
        assert name.endswith(".pdf")

    def test_the_bucket_prefix_is_not_applied_twice(self):
        client = RecordingS3()
        store = S3Store("bkt", prefix="autoapply", client=client)
        ref = store.put("C:/r.pdf", resume_key(1, 2, "r.pdf"))
        assert client.uploads[0]["key"] == "autoapply/users/1/jobs/2/r.pdf"
        assert ref == "s3://bkt/autoapply/users/1/jobs/2/r.pdf"

    @pytest.mark.parametrize("ref,expected", [
        ("s3://bkt/a/b.pdf", ("bkt", "a/b.pdf")),
        ("C:/resumes/b.pdf", None),
        ("s3://bkt", None),
        ("", None),
    ])
    def test_parse_ref(self, ref, expected):
        assert parse_s3_ref(ref) == expected


# ── Backends ────────────────────────────────────────────────────────────────

class TestLocal:
    def test_is_the_default(self):
        assert isinstance(storage.get_resume_store(), LocalStore)

    def test_stores_nothing_extra_and_serves_locally(self):
        store = LocalStore()
        assert store.put("C:/r.pdf", "k") is None
        assert store.download_url("anything", "r.pdf") is None


class TestS3:
    def test_upload_is_encrypted_and_typed(self):
        client = RecordingS3()
        S3Store("bkt", client=client).put("C:/r.pdf", "users/1/jobs/2/r.pdf")
        extra = client.uploads[0]["extra"]
        assert extra["ServerSideEncryption"] == "AES256"
        assert extra["ContentType"] == "application/pdf"

    def test_download_is_a_short_lived_attachment_link(self):
        client = RecordingS3()
        store = S3Store("bkt", presign_ttl_seconds=900, client=client)
        url = store.download_url("s3://bkt/users/1/jobs/2/r.pdf", 'Asha "CV".pdf')

        assert url.startswith("https://bkt.s3.amazonaws.com/users/1/jobs/2/r.pdf")
        call = client.presigned[0]
        assert call["op"] == "get_object" and call["ttl"] == 900
        assert call["params"]["ResponseContentDisposition"] == \
            'attachment; filename="Asha CV.pdf"'

    def test_a_non_s3_ref_is_served_locally(self):
        assert S3Store("bkt", client=RecordingS3()).download_url("C:/r.pdf", "r.pdf") is None

    def test_switching_on_s3_without_a_bucket_is_refused(self):
        with pytest.raises(StorageConfigError, match="S3_BUCKET"):
            S3Store("")

    def test_the_factory_refuses_an_unknown_backend(self, monkeypatch):
        from backend.app.config import get_settings

        monkeypatch.setattr(get_settings(), "storage_backend", "dropbox", raising=False)
        with pytest.raises(StorageConfigError, match="dropbox"):
            storage.get_resume_store()

    def test_the_factory_refuses_s3_with_no_bucket(self, monkeypatch):
        """Loud at startup — otherwise it looks configured until a resume
        quietly stays local."""
        from backend.app.config import get_settings

        monkeypatch.setattr(get_settings(), "storage_backend", "s3", raising=False)
        monkeypatch.setattr(get_settings(), "s3_bucket", "", raising=False)
        with pytest.raises(StorageConfigError):
            storage.get_resume_store()

    def test_a_real_boto3_client_signs_the_right_object(self):
        """No network: presigning is a local computation. This proves the URL
        boto3 produces names our bucket, key, disposition and expiry."""
        boto3 = pytest.importorskip("boto3")
        client = boto3.client(
            "s3", region_name="ap-south-1",
            aws_access_key_id="AKIAEXAMPLE", aws_secret_access_key="secret",
        )
        url = S3Store("resumes-bkt", client=client, presign_ttl_seconds=600).download_url(
            "s3://resumes-bkt/autoapply/users/1/jobs/2/r.pdf", "r.pdf"
        )
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert "resumes-bkt" in parsed.netloc + parsed.path
        assert parsed.path.endswith("/autoapply/users/1/jobs/2/r.pdf")
        assert query["X-Amz-Expires"] == ["600"]
        assert "attachment" in query["response-content-disposition"][0]
        assert "X-Amz-Signature" in query


# ── Generator + download endpoint ───────────────────────────────────────────

class TestGeneratorStores:
    def test_the_finished_pdf_is_uploaded_under_the_owners_key(self, make_job, session):
        from backend.app.models.job import Job
        from backend.app.services.resume_generator import _store_resume

        client = RecordingS3()
        storage.reset_store_for_tests(S3Store("bkt", prefix="aa", client=client))
        job = session.get(Job, make_job())

        ref = _store_resume(job, "C:/resumes/Asha_Backend.pdf")
        assert ref == f"s3://bkt/aa/users/{job.user_id}/jobs/{job.job_id}/Asha_Backend.pdf"

    def test_an_upload_failure_keeps_the_resume_rather_than_losing_it(self, make_job, session):
        from backend.app.models.job import Job
        from backend.app.services.resume_generator import _store_resume

        storage.reset_store_for_tests(S3Store("bkt", client=RecordingS3(fail_upload=True)))
        job = session.get(Job, make_job())
        assert _store_resume(job, "C:/resumes/r.pdf") is None

    def test_local_storage_records_no_ref(self, make_job, session):
        from backend.app.models.job import Job
        from backend.app.services.resume_generator import _store_resume

        job = session.get(Job, make_job())
        assert _store_resume(job, "C:/resumes/r.pdf") is None


class TestDownloadEndpoint:
    def _job_with_resume(self, make_job, session, tmp_path, ref=None):
        from backend.app.config import get_settings
        from backend.app.models.job import Job

        pdf = Path(get_settings().resumes_dir) / "download_test.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4 test")
        job_id = make_job(status="READY_TO_APPLY")
        job = session.get(Job, job_id)
        job.resume_path = str(pdf.resolve())
        job.resume_storage_ref = ref
        session.add(job)
        session.commit()
        return job_id

    def test_an_s3_stored_resume_redirects_to_a_presigned_link(
        self, client, make_job, session, tmp_path
    ):
        fake = RecordingS3()
        storage.reset_store_for_tests(S3Store("bkt", client=fake))
        job_id = self._job_with_resume(make_job, session, tmp_path,
                                       ref="s3://bkt/users/1/jobs/9/r.pdf")

        r = client.get(f"/api/resumes/{job_id}", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"].startswith("https://bkt.s3.amazonaws.com/")

    def test_a_locally_stored_resume_is_served_directly(
        self, client, make_job, session, tmp_path
    ):
        job_id = self._job_with_resume(make_job, session, tmp_path, ref=None)
        r = client.get(f"/api/resumes/{job_id}", follow_redirects=False)
        assert r.status_code == 200
        assert r.content.startswith(b"%PDF")

    def test_another_account_still_cannot_reach_it(
        self, anon_client, make_job, session, tmp_path
    ):
        """Moving storage must not move the ownership check."""
        storage.reset_store_for_tests(S3Store("bkt", client=RecordingS3()))
        job_id = self._job_with_resume(make_job, session, tmp_path,
                                       ref="s3://bkt/users/1/jobs/9/r.pdf")
        assert anon_client.get(f"/api/resumes/{job_id}",
                               follow_redirects=False).status_code == 401
