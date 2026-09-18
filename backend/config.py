from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
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
    ollama_base_url: str = Field(default="http://localhost:11434")

    # ── Job Discovery ─────────────────────────────────────────────────────────
    serper_api_key: str = Field(default="", description="Serper.dev search API key")
    daily_search_limit: int = Field(default=100, ge=1, le=500)

    # ── Application Settings ──────────────────────────────────────────────────
    daily_apply_limit: int = Field(default=50, ge=1, le=200)
    scrape_batch_size: int = Field(default=10, ge=1, le=50)
    apply_batch_size: int = Field(default=3, ge=1, le=10)
    playwright_headless: bool = Field(default=True)
    playwright_max_browsers: int = Field(default=2, ge=1, le=5)
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
    email_sender_name: str = Field(default="")

    # ── Database & Storage ────────────────────────────────────────────────────
    database_url: str = Field(default="sqlite:///data/autoapply.db")
    chroma_persist_dir: str = Field(default="data/chroma")
    resumes_dir: str = Field(default="resumes")
    screenshots_dir: str = Field(default="screenshots")

    # ── Q&A Cache ─────────────────────────────────────────────────────────────
    qa_similarity_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    qa_embedding_model: str = Field(default="nomic-embed-text")

    # ── LLM Models ───────────────────────────────────────────────────────────
    groq_model_heavy: str = Field(default="llama-3.3-70b-versatile")
    ollama_model_light: str = Field(default="phi3:mini")
    ollama_model_classify: str = Field(default="gemma2:2b")

    # ── Resume ────────────────────────────────────────────────────────────────
    resume_template_path: str = Field(default="gaurav_resume_final.tex")
    master_profile_path: str = Field(default="master_profile.json")
    ats_min_score: int = Field(default=85, ge=0, le=100)
    resume_max_retries: int = Field(default=3, ge=1, le=5)

    # ── FastAPI Server ────────────────────────────────────────────────────────
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000, ge=1024, le=65535)
    frontend_port: int = Field(default=5173)

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")
    log_dir: str = Field(default="logs")

    @field_validator("api_host")
    @classmethod
    def host_must_be_local(cls, v: str) -> str:
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
            self.chroma_persist_dir,
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
