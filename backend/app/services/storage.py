"""Where generated resumes live: local disk, or S3.

Local by default so the app works with nothing configured. Setting
STORAGE_BACKEND=s3 (plus a bucket) sends every generated resume to S3 as well,
and the download link becomes a short-lived presigned S3 URL.

The PDF is always kept on local disk too. Portal auto-apply uploads the file
through a browser file input, which needs a local path — S3 is the durable copy
and the thing the download button points at, not a replacement for the file.

Only generated (per-job) resumes go to S3. The uploaded resume library stays on
local disk.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Protocol

from backend.app.config import get_settings

logger = logging.getLogger("storage")

S3_SCHEME = "s3://"


class StorageConfigError(RuntimeError):
    """Storage was switched on but cannot work as configured."""


class ResumeStore(Protocol):
    name: str

    def put(self, local_path: str, key: str) -> Optional[str]:
        """Store the file; return a reference to save, or None if nothing stored."""

    def download_url(self, ref: str, filename: str) -> Optional[str]:
        """A URL the browser can fetch directly, or None to serve the local file."""


def resume_key(user_id: Optional[int], job_id: int, filename: str) -> str:
    """Object key for a generated resume.

    Per account, so an S3 lifecycle rule or an access policy can be written
    against one user's prefix — and so two accounts' resumes can never share a
    key even if they apply to the same posting.
    """
    # Dot runs collapsed and leading dots stripped: S3 keys are flat, but a key
    # that is ever mirrored to a filesystem must not start with "..".
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    safe_name = re.sub(r"\.{2,}", ".", safe_name).strip("._") or f"job_{job_id}.pdf"
    # No bucket prefix here: S3Store.put applies it, in exactly one place.
    owner = user_id if user_id is not None else "unowned"
    return f"users/{owner}/jobs/{job_id}/{safe_name}"


def parse_s3_ref(ref: str) -> Optional[tuple[str, str]]:
    if not (ref or "").startswith(S3_SCHEME):
        return None
    bucket, _, key = ref[len(S3_SCHEME):].partition("/")
    return (bucket, key) if bucket and key else None


class LocalStore:
    """Nothing to copy: the generator already wrote the PDF under RESUMES_DIR."""

    name = "local"

    def put(self, local_path: str, key: str) -> Optional[str]:
        return None

    def download_url(self, ref: str, filename: str) -> Optional[str]:
        return None


class S3Store:
    name = "s3"

    def __init__(self, bucket: str, *, region: str = "", prefix: str = "",
                 presign_ttl_seconds: int = 900, client=None) -> None:
        if not bucket:
            raise StorageConfigError("STORAGE_BACKEND=s3 requires S3_BUCKET")
        self.bucket = bucket
        self.prefix = prefix
        self.ttl = presign_ttl_seconds
        self._client = client
        self._region = region

    @property
    def client(self):
        if self._client is None:
            import boto3

            settings = get_settings()
            kwargs = {"region_name": self._region or None}
            # Explicit keys only if given; otherwise boto3's own chain (env,
            # ~/.aws, instance role) applies, which is the right default on AWS.
            if settings.aws_access_key_id and settings.aws_secret_access_key:
                kwargs.update(
                    aws_access_key_id=settings.aws_access_key_id,
                    aws_secret_access_key=settings.aws_secret_access_key,
                )
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def put(self, local_path: str, key: str) -> Optional[str]:
        full_key = f"{self.prefix.strip('/')}/{key}".strip("/") if self.prefix else key
        self.client.upload_file(
            local_path,
            self.bucket,
            full_key,
            ExtraArgs={
                "ContentType": "application/pdf",
                # A resume is personal data; encrypt at rest even if the bucket
                # default is not configured to.
                "ServerSideEncryption": "AES256",
            },
        )
        ref = f"{S3_SCHEME}{self.bucket}/{full_key}"
        logger.info("Uploaded resume to %s", ref)
        return ref

    def download_url(self, ref: str, filename: str) -> Optional[str]:
        parsed = parse_s3_ref(ref)
        if parsed is None:
            return None
        bucket, key = parsed
        safe_name = filename.replace('"', "")
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                # Makes S3 itself send it as a download. The browser ignores the
                # `download` attribute once a redirect leaves our origin.
                "ResponseContentDisposition": f'attachment; filename="{safe_name}"',
                "ResponseContentType": "application/pdf",
            },
            # Short on purpose: the link is minted per click, and a leaked one
            # should stop working almost immediately.
            ExpiresIn=self.ttl,
        )


_store: Optional[ResumeStore] = None


def get_resume_store() -> ResumeStore:
    global _store
    if _store is None:
        settings = get_settings()
        backend = (settings.storage_backend or "local").lower()
        if backend == "local":
            _store = LocalStore()
        elif backend == "s3":
            _store = S3Store(
                settings.s3_bucket,
                region=settings.s3_region,
                prefix=settings.s3_prefix,
                presign_ttl_seconds=settings.s3_presign_ttl_seconds,
            )
        else:
            raise StorageConfigError(
                f"STORAGE_BACKEND must be 'local' or 's3', not {settings.storage_backend!r}"
            )
    return _store


def reset_store_for_tests(store: Optional[ResumeStore] = None) -> None:
    global _store
    _store = store
