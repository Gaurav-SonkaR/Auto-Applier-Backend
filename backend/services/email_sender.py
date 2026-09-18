from __future__ import annotations

import asyncio
import logging
import mimetypes
import smtplib
from collections import defaultdict
from datetime import date, datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from backend.config import get_settings

logger = logging.getLogger("email")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


class DailyLimitReached(Exception):
    """Raised when the daily email cap (40) has been hit."""


class EmailSender:
    """
    Sends cold emails via Gmail SMTP SSL.

    Enforces:
    - Global 40 emails/day hard cap (resets at midnight)
    - Per-domain 3 emails/day limit
    - Minimum delay between sends (configurable, default 90s)
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._sent_today: int = 0
        self._sent_date: date = date.today()
        self._domain_counts: dict[str, int] = defaultdict(int)
        self._last_send_ts: float = 0.0
        self._lock = asyncio.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def send(
        self,
        to: str,
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
        attachment_path: Optional[str] = None,
    ) -> bool:
        """Send an email. Returns True on success."""
        async with self._lock:
            self._maybe_reset_daily_counter()
            self._check_daily_limit()
            self._check_domain_limit(to)
            await self._enforce_min_delay()

            success = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._smtp_send(to, subject, body_text, body_html, attachment_path),
            )
            if success:
                self._sent_today += 1
                self._domain_counts[_extract_domain(to)] += 1
                logger.info(
                    "Email sent to=%s subject=%r (today=%d/%d)",
                    to,
                    subject,
                    self._sent_today,
                    self._settings.cold_email_daily_limit,
                )
                import time
                self._last_send_ts = time.monotonic()
            return success

    def daily_count(self) -> int:
        self._maybe_reset_daily_counter()
        return self._sent_today

    def remaining_today(self) -> int:
        return max(0, self._settings.cold_email_daily_limit - self.daily_count())

    # ── SMTP ───────────────────────────────────────────────────────────────────

    def _smtp_send(
        self,
        to: str,
        subject: str,
        body_text: str,
        body_html: Optional[str],
        attachment_path: Optional[str],
    ) -> bool:
        msg = MIMEMultipart("alternative") if not attachment_path else MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = self._settings.gmail_address
        msg["To"] = to
        msg["X-Mailer"] = "AutoApply AI"

        text_part = MIMEText(body_text, "plain")
        if body_html:
            html_part = MIMEText(body_html, "html")
            alt = MIMEMultipart("alternative")
            alt.attach(text_part)
            alt.attach(html_part)
            msg.attach(alt)
        else:
            msg.attach(text_part)

        if attachment_path:
            _attach_file(msg, attachment_path)

        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.login(self._settings.gmail_address, self._settings.gmail_app_password)
                smtp.sendmail(self._settings.gmail_address, [to], msg.as_string())
            return True
        except smtplib.SMTPException as exc:
            logger.error("SMTP error sending to %s: %s", to, exc)
            return False

    # ── Limit helpers ──────────────────────────────────────────────────────────

    def _maybe_reset_daily_counter(self) -> None:
        today = date.today()
        if today != self._sent_date:
            self._sent_today = 0
            self._domain_counts.clear()
            self._sent_date = today

    def _check_daily_limit(self) -> None:
        if self._sent_today >= self._settings.cold_email_daily_limit:
            raise DailyLimitReached(
                f"Daily email limit of {self._settings.cold_email_daily_limit} reached. "
                "Resumes at midnight."
            )

    def _check_domain_limit(self, to: str) -> None:
        domain = _extract_domain(to)
        per_domain_max = self._settings.cold_email_max_per_domain
        if self._domain_counts.get(domain, 0) >= per_domain_max:
            raise DailyLimitReached(
                f"Per-domain limit of {per_domain_max} reached for {domain}."
            )

    async def _enforce_min_delay(self) -> None:
        import time
        min_delay = self._settings.cold_email_delay_seconds
        elapsed = time.monotonic() - self._last_send_ts
        if self._last_send_ts and elapsed < min_delay:
            wait = min_delay - elapsed
            logger.info("Rate-limiting email send — sleeping %.1fs", wait)
            await asyncio.sleep(wait)


# ── Module helpers ─────────────────────────────────────────────────────────────

def _extract_domain(email: str) -> str:
    return email.split("@")[-1].lower() if "@" in email else email.lower()


def _attach_file(msg: MIMEMultipart, path: str) -> None:
    p = Path(path)
    mime_type, _ = mimetypes.guess_type(p.name)
    maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
    with p.open("rb") as f:
        part = MIMEApplication(f.read(), Name=p.name)
    part["Content-Disposition"] = f'attachment; filename="{p.name}"'
    msg.attach(part)


# Module-level singleton
_sender: Optional[EmailSender] = None


def get_email_sender() -> EmailSender:
    global _sender
    if _sender is None:
        _sender = EmailSender()
    return _sender
