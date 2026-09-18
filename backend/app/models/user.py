from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class UserStatus(str, Enum):
    """Signup is open; *use* is not.

    A new account lands in PENDING and cannot log in. An admin moves it to
    ACTIVE (or REJECTED). This is what makes "anyone can sign up, nobody can
    use it without my approval" true — the gate is on login, not on signup.
    """

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    DISABLED = "DISABLED"   # was active, access revoked later


class UserRole(str, Enum):
    ADMIN = "ADMIN"   # can approve/reject/disable other accounts
    USER = "USER"


# Only these can sign in. Kept as a tuple so the login check and the tests
# agree on one definition.
LOGIN_ALLOWED_STATUSES = (UserStatus.ACTIVE,)


class User(SQLModel, table=True):
    __tablename__ = "user"

    user_id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True, description="Login name")
    email: Optional[str] = Field(default=None, description="For contacting the applicant")
    # SEC-001 said "no password fields in database schema", written when this
    # was a single-user tool whose one credential lived in .env. Multi-user
    # signup can't work that way — accounts have to be created at runtime, so
    # the hash moves into the DB. It is a scrypt hash, never a password.
    password_hash: str = Field(description="scrypt hash — never a plain password")
    status: UserStatus = Field(default=UserStatus.PENDING, index=True)
    role: UserRole = Field(default=UserRole.USER, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    approved_at: Optional[datetime] = Field(default=None)
    approved_by: Optional[str] = Field(default=None, description="Username of the approving admin")
    last_login_at: Optional[datetime] = Field(default=None)

    # ── Password reset ──────────────────────────────────────────────────────
    # A locked-out user can't log in to prove who they are, so the reset is
    # gated the same way signup is: they ask, an admin approves, only then can
    # they set a new password.
    #
    # The code is what stops the *approval* from being usable by anyone else.
    # Without it, anyone who knew the username could set the password during
    # the window between approve and reset. It is generated when the request is
    # made, shown once to whoever made it, and stored only as a hash.
    reset_requested_at: Optional[datetime] = Field(default=None)
    reset_code_hash: Optional[str] = Field(default=None, description="scrypt hash of the one-time code")
    reset_approved_at: Optional[datetime] = Field(default=None)
    reset_approved_by: Optional[str] = Field(default=None)

    def can_log_in(self) -> bool:
        return self.status in LOGIN_ALLOWED_STATUSES

    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def reset_state(self) -> str:
        """NONE / REQUESTED / APPROVED — what the admin's account list shows.

        Derived rather than stored so it can never disagree with the fields the
        reset actually checks.
        """
        if self.reset_requested_at is None:
            return "NONE"
        return "APPROVED" if self.reset_approved_at is not None else "REQUESTED"
