from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM Providers ────────────────────────────────────────────────────────
    groq_api_key: str = Field(..., description="Groq API key (required)")
    hf_api_token: str = Field(default="", description="HuggingFace Inference API token")

    # ── Job Discovery ─────────────────────────────────────────────────────────
    serper_api_key: str = Field(default="", description="Serper.dev search API key")
    daily_search_limit: int = Field(default=100, ge=1, le=500)
    discovery_recency_days: int = Field(default=2, ge=1, le=30)

    # ── Application Settings ──────────────────────────────────────────────────
    daily_apply_limit: int = Field(default=50, ge=1, le=200)
    scrape_batch_size: int = Field(default=10, ge=1, le=50)
    apply_batch_size: int = Field(default=3, ge=1, le=10)
    selenium_headless: bool = Field(
        default=True,
        validation_alias=AliasChoices("SELENIUM_HEADLESS", "PLAYWRIGHT_HEADLESS"),
    )
    selenium_max_drivers: int = Field(
        default=2,
        ge=1,
        le=5,
        validation_alias=AliasChoices("SELENIUM_MAX_DRIVERS", "PLAYWRIGHT_MAX_BROWSERS"),
    )
    chrome_binary: str = Field(
        default="",
        description="Optional Chrome/Chromium executable path",
        validation_alias=AliasChoices("CHROME_BINARY", "SELENIUM_CHROME_BINARY"),
    )
    human_delay_min_ms: int = Field(default=800, ge=200)
    human_delay_max_ms: int = Field(default=2500, ge=500)

    # ── Portal Credentials ────────────────────────────────────────────────────
    gmail_address: str = Field(default="", description="Gmail address for login + cold email")
    gmail_app_password: str = Field(default="", description="Gmail App Password (not regular)")
    # Per-portal login. Each portal resolves {portal}_email (falling back to
    # gmail_address) and {portal}_password. A blank password means "not configured"
    # — the apply flow flags the job for manual login rather than guessing creds.
    linkedin_email: str = Field(default="")
    linkedin_password: str = Field(default="")
    naukri_email: str = Field(default="")
    naukri_password: str = Field(default="")
    cutshort_email: str = Field(default="")
    cutshort_password: str = Field(default="")
    wellfound_email: str = Field(default="")
    wellfound_password: str = Field(default="")
    instahyre_email: str = Field(default="")
    instahyre_password: str = Field(default="")

    # ── Cold Email ────────────────────────────────────────────────────────────
    cold_email_daily_limit: int = Field(default=40, ge=1, le=100)
    cold_email_delay_seconds: int = Field(default=120, ge=30)
    cold_email_max_per_domain: int = Field(default=10, ge=1)
    # Ramp the daily cap up over this many days from the first send ever.
    # Going from zero to 40 mails a day on day one is how a fresh sending
    # address gets its reputation destroyed before anyone reads anything.
    # 0 disables the ramp.
    cold_email_warmup_days: int = Field(default=14, ge=0, le=90)
    email_sender_name: str = Field(default="")

    # ── Database & Storage ────────────────────────────────────────────────────
    database_url: str = Field(default="sqlite:///data/autoapply.db")
    resumes_dir: str = Field(default="resumes")
    # Where generated resumes are stored. "local" needs nothing; "s3" also
    # uploads each one and makes the download a presigned S3 link.
    storage_backend: str = Field(default="local")
    s3_bucket: str = Field(default="")
    s3_region: str = Field(default="")
    s3_prefix: str = Field(default="autoapply")
    s3_presign_ttl_seconds: int = Field(default=900, ge=60, le=604800)
    # Optional: without these, boto3's own chain (env, ~/.aws, instance role).
    aws_access_key_id: str = Field(default="")
    aws_secret_access_key: str = Field(default="")
    screenshots_dir: str = Field(default="screenshots")

    # ── LLM Models ───────────────────────────────────────────────────────────
    groq_model_heavy: str = Field(default="llama-3.3-70b-versatile")

    # ── Resume ────────────────────────────────────────────────────────────────
    resume_template_path: str = Field(default="templates/resume_template.tex")
    master_profile_path: str = Field(default="master_profile.json")
    ats_min_score: int = Field(default=85, ge=0, le=100)
    # Skip postings the profile clearly doesn't fit, before a resume is written.
    # Tolerance exists because postings overstate: "3+ years" routinely hires 2.
    fit_filter_enabled: bool = Field(default=True)
    # Rehearse every portal application: fill the form, screenshot it, never
    # click submit. Also available per run from the dashboard.
    apply_dry_run: bool = Field(default=False)
    fit_min_skill_overlap: float = Field(default=0.3, ge=0.0, le=1.0)
    resume_max_retries: int = Field(default=3, ge=1, le=5)

    # ── FastAPI Server ────────────────────────────────────────────────────────
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000, ge=1024, le=65535)
    frontend_port: int = Field(default=5173)

    # ── Authentication ────────────────────────────────────────────────────────
    # Only the token signing key lives here. Accounts moved into the database
    # when signup was added — they're created at runtime, so they can't be
    # env-configured. Bootstrap the first admin with scripts/set_password.py.
    auth_secret_key: str = Field(default="", description="HMAC key for signing session tokens")
    auth_token_ttl_hours: int = Field(default=12, ge=1, le=720)

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")
    log_dir: str = Field(default="logs")

    def auth_configured(self) -> bool:
        """True once a signing key exists. Whether any *admin* exists is a
        database question, answered by services.users.count_admins()."""
        return bool(self.auth_secret_key)

    @field_validator("api_host")
    @classmethod
    def host_must_be_local(cls, v: str) -> str:
        """Guard against network exposure — the API has no authentication.

        Note this setting is informational: uvicorn's actual bind address comes
        from its --host argument. In Docker the process binds 0.0.0.0 (required
        for port publishing to reach it) and docker-compose restricts exposure
        by publishing to 127.0.0.1 on the host instead.
        """
        if v not in ("127.0.0.1", "localhost"):
            raise ValueError(
                "API_HOST must be 127.0.0.1 or localhost — "
                "this is a local-only tool and must not be network-exposed."
            )
        return v

    @field_validator("human_delay_max_ms")
    @classmethod
    def max_delay_gt_min(cls, v: int, info) -> int:
        min_val = info.data.get("human_delay_min_ms", 0)
        if v <= min_val:
            raise ValueError("HUMAN_DELAY_MAX_MS must be greater than HUMAN_DELAY_MIN_MS")
        return v

    @field_validator("apply_batch_size")
    @classmethod
    def apply_batch_within_scrape_batch(cls, v: int, info) -> int:
        scrape = info.data.get("scrape_batch_size", 50)
        if v > scrape:
            raise ValueError("APPLY_BATCH_SIZE cannot exceed SCRAPE_BATCH_SIZE")
        return v

    @field_validator("cold_email_max_per_domain")
    @classmethod
    def per_domain_within_daily_limit(cls, v: int, info) -> int:
        daily = info.data.get("cold_email_daily_limit", 100)
        if v > daily:
            raise ValueError("COLD_EMAIL_MAX_PER_DOMAIN cannot exceed COLD_EMAIL_DAILY_LIMIT")
        return v

    @field_validator("frontend_port")
    @classmethod
    def frontend_port_differs_from_api(cls, v: int, info) -> int:
        if v == info.data.get("api_port"):
            raise ValueError("FRONTEND_PORT must differ from API_PORT")
        return v

    def ensure_directories(self) -> None:
        for path in (
            self.resumes_dir,
            self.screenshots_dir,
            self.log_dir,
            str(Path(self.database_url.replace("sqlite:///", "")).parent),
        ):
            Path(path).mkdir(parents=True, exist_ok=True)

    def validate_required_for_email(self) -> None:
        if not self.gmail_address or not self.gmail_app_password:
            raise ValueError(
                "GMAIL_ADDRESS and GMAIL_APP_PASSWORD are required for cold email. "
                "Set them in .env"
            )

    def validate_required_for_search(self) -> None:
        if not self.serper_api_key:
            raise ValueError("SERPER_API_KEY is required for Google job discovery.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
