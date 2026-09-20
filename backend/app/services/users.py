"""Account lifecycle: signup, approval, and the admin bootstrap.

The rule this module exists to enforce: **signup is open, use is not.** Anyone
can create an account; nobody can log in until an admin moves them to ACTIVE.
The gate lives on login (`authenticate`), not on signup, so there is exactly
one place it can be got wrong.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, func, select

from backend.app.models.user import User, UserRole, UserStatus
from backend.app.services.auth import hash_password, verify_password

logger = logging.getLogger("users")

MIN_PASSWORD_LENGTH = 8
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,150}$")

# How long an unused reset request stays valid. Long enough that an admin
# can approve it the next morning, short enough that a forgotten request
# does not sit there redeemable forever.
PASSWORD_RESET_TTL_HOURS = 24

# Statuses whose owner may ask for a reset. REJECTED and DISABLED are left
# out on purpose: their access was taken away deliberately, and a password
# reset must not become a way back in. The admin re-enables those instead.
RESET_ALLOWED_STATUSES = (UserStatus.ACTIVE, UserStatus.PENDING)


class SignupError(ValueError):
    """Signup input the caller should fix (bad username, taken, weak password)."""


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not _USERNAME_RE.fullmatch(username):
        raise SignupError(
            "Username must be 3-150 characters, using only letters, digits and . _ @ -"
        )
    return username


def validate_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise SignupError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def get_by_username(session: Session, username: str) -> Optional[User]:
    return session.exec(select(User).where(User.username == username)).first()


def create_user(
    session: Session,
    username: str,
    password: str,
    email: Optional[str] = None,
    *,
    status: UserStatus = UserStatus.PENDING,
    role: UserRole = UserRole.USER,
) -> User:
    """Create an account. Defaults to PENDING — it cannot log in until approved."""
    username = validate_username(username)
    validate_password(password)

    if get_by_username(session, username) is not None:
        raise SignupError("That username is already taken")

    user = User(
        username=username,
        email=(email or "").strip() or None,
        password_hash=hash_password(password),
        status=status,
        role=role,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Account created: %s (status=%s role=%s)", username, status.value, role.value)
    return user


def authenticate(session: Session, username: str, password: str) -> tuple[Optional[User], str]:
    """Return (user, reason). `user` is None unless the account may log in now.

    `reason` distinguishes *why* a login failed so the API can tell a pending
    applicant "waiting for approval" — genuinely useful — while still returning
    one indistinguishable message for wrong-username vs wrong-password, which
    would otherwise let someone enumerate accounts.
    """
    user = get_by_username(session, username)

    # Run the hash comparison even when the user doesn't exist, so both paths
    # take comparable time and can't be told apart by timing.
    stored_hash = user.password_hash if user else ""
    password_ok = verify_password(password, stored_hash)

    if user is None or not password_ok:
        return None, "bad_credentials"

    if user.status == UserStatus.PENDING:
        return None, "pending"
    if user.status == UserStatus.REJECTED:
        return None, "rejected"
    if user.status == UserStatus.DISABLED:
        return None, "disabled"
    if not user.can_log_in():
        return None, "not_allowed"

    user.last_login_at = datetime.now(timezone.utc)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user, "ok"


def set_status(
    session: Session, user_id: int, status: UserStatus, *, by_admin: str
) -> Optional[User]:
    """Approve / reject / disable an account."""
    user = session.get(User, user_id)
    if user is None:
        return None
    user.status = status
    if status == UserStatus.ACTIVE:
        user.approved_at = datetime.now(timezone.utc)
        user.approved_by = by_admin
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Account %s set to %s by %s", user.username, status.value, by_admin)
    return user


def list_users(session: Session) -> list[User]:
    """Pending first (they're the ones needing action), then newest."""
    users = session.exec(select(User)).all()
    return sorted(
        users,
        key=lambda u: (u.status != UserStatus.PENDING, -(u.user_id or 0)),
    )


def count_admins(session: Session, exclude_user_id: Optional[int] = None) -> int:
    stmt = select(func.count()).select_from(User).where(
        User.role == UserRole.ADMIN, User.status == UserStatus.ACTIVE
    )
    if exclude_user_id is not None:
        stmt = stmt.where(User.user_id != exclude_user_id)
    return session.exec(stmt).one()


def set_user_password(session: Session, username: str, password: str) -> Optional[User]:
    """Reset one account's password, leaving its role and status untouched.

    Separate from `ensure_admin` because the two used to be the same call, and
    resetting a normal member's forgotten password silently promoted them to
    admin.
    """
    validate_password(password)
    user = get_by_username(session, username)
    if user is None:
        return None
    user.password_hash = hash_password(password)
    _clear_reset(user)
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Password reset for %s (role unchanged)", username)
    return user


def set_role(session: Session, user_id: int, role: UserRole) -> Optional[User]:
    """Promote or demote. The only network-reachable path to ADMIN, guarded by
    `require_admin` and the last-admin check in the router."""
    user = session.get(User, user_id)
    if user is None:
        return None
    user.role = role
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Account %s role set to %s", user.username, role.value)
    return user


def ensure_admin(session: Session, username: str, password: str) -> User:
    """Create the account as an ACTIVE admin, or promote and reset an existing
    one — used by scripts/set_password.py to bootstrap the first admin.

    Promoting is a side effect, so callers that only want a password reset must
    use `set_user_password` instead.
    """
    validate_password(password)
    username = validate_username(username)

    user = get_by_username(session, username)
    if user is None:
        return create_user(
            session, username, password,
            status=UserStatus.ACTIVE, role=UserRole.ADMIN,
        )

    user.password_hash = hash_password(password)
    user.status = UserStatus.ACTIVE
    user.role = UserRole.ADMIN
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Admin account %s updated", username)
    return user


# -- Password reset ----------------------------------------------------------
#
# A locked-out user cannot log in to prove who they are, so the reset follows
# the same shape as signup: they ask, an admin approves, only then can they set
# a new password. Two separate secrets have to line up — the admin's approval
# and the one-time code handed to whoever made the request — so neither an
# unapproved stranger nor a bystander watching for approvals can take over an
# account on their own.


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; everything written is UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _clear_reset(user: User) -> None:
    """One place to forget a reset, so no field is ever left behind — a stale
    `reset_approved_at` would make the next request pre-approved."""
    user.reset_requested_at = None
    user.reset_code_hash = None
    user.reset_approved_at = None
    user.reset_approved_by = None


def request_password_reset(session: Session, username: str) -> str:
    """Record a reset request and return the one-time code to show the caller.

    A code comes back even when the username does not exist or is not eligible.
    Nothing is stored in that case, so the code simply never works — but the
    response is identical either way, which stops this endpoint from being used
    to find out which accounts exist.
    """
    code = secrets.token_urlsafe(9)

    user = get_by_username(session, (username or "").strip())
    if user is None or user.status not in RESET_ALLOWED_STATUSES:
        logger.info("Password reset requested for %r — no eligible account", username)
        return code

    user.reset_requested_at = datetime.now(timezone.utc)
    user.reset_code_hash = hash_password(code)
    # A fresh request drops any earlier approval: the admin approved the old
    # request, not this one.
    user.reset_approved_at = None
    user.reset_approved_by = None
    session.add(user)
    session.commit()
    logger.info("Password reset requested for %s", user.username)
    return code


def approve_password_reset(session: Session, user_id: int, *, by_admin: str) -> Optional[User]:
    user = session.get(User, user_id)
    if user is None or user.reset_requested_at is None:
        return None
    user.reset_approved_at = datetime.now(timezone.utc)
    user.reset_approved_by = by_admin
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Password reset for %s approved by %s", user.username, by_admin)
    return user


def deny_password_reset(session: Session, user_id: int, *, by_admin: str) -> Optional[User]:
    user = session.get(User, user_id)
    if user is None or user.reset_requested_at is None:
        return None
    _clear_reset(user)
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Password reset for %s denied by %s", user.username, by_admin)
    return user


def complete_password_reset(
    session: Session, username: str, code: str, new_password: str
) -> tuple[Optional[User], str]:
    """Set the new password. Returns (user, reason); user is None on failure.

    The code is checked *before* the approval state, deliberately. Reporting
    "not approved yet" to someone who has not got the code would tell them a
    reset is pending for that account; only the person holding the code — who
    already knows — ever sees that message.
    """
    validate_password(new_password)

    user = get_by_username(session, (username or "").strip())
    code_ok = verify_password(code or "", (user.reset_code_hash if user else None) or "")
    if user is None or user.reset_requested_at is None or not code_ok:
        return None, "invalid"

    requested = _as_utc(user.reset_requested_at)
    if datetime.now(timezone.utc) - requested > timedelta(hours=PASSWORD_RESET_TTL_HOURS):
        _clear_reset(user)
        session.add(user)
        session.commit()
        return None, "expired"

    if user.reset_approved_at is None:
        return None, "not_approved"

    user.password_hash = hash_password(new_password)
    # The approval is spent. Leaving it set would let the code be replayed to
    # change the password again later without asking anyone.
    _clear_reset(user)
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Password reset completed for %s", user.username)
    return user, "ok"
