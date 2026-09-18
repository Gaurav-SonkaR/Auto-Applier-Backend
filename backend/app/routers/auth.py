from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from backend.app.config import get_settings
from backend.app.database import engine
from backend.app.models.user import User, UserRole, UserStatus
from backend.app.schemas.auth import (
    CurrentUser,
    LoginRequest,
    LoginResponse,
    PasswordResetCompleteRequest,
    PasswordResetRequest,
    PasswordResetRequestResponse,
    SetPasswordResetRequest,
    SetUserRoleRequest,
    SetUserStatusRequest,
    SignupRequest,
    SignupResponse,
    UserListResponse,
    UserSummary,
)
from backend.app.services import users as user_service
from backend.app.services.auth import create_token, decode_token, token_expiry

logger = logging.getLogger("api.auth")

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])

# auto_error=False so a missing header reaches our own handler and produces a
# consistent 401 body, rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False)

# One shared message for wrong-username and wrong-password — distinguishing
# them lets someone enumerate which accounts exist.
_BAD_CREDENTIALS = "Incorrect username or password"

# These *are* distinguished, deliberately: telling an applicant their account
# is awaiting approval is useful and leaks nothing they didn't already know
# (they only see it after proving the password).
_STATUS_MESSAGES = {
    "pending": "Your account is waiting for admin approval. You'll be able to sign in once it's approved.",
    "rejected": "This account was not approved.",
    "disabled": "This account has been disabled.",
    "not_allowed": "This account cannot sign in.",
}

# Brute-force damper. A delay rather than a lockout — a lockout would let
# anyone who can reach the port deny the real owner access to their own app.
_FAILED_ATTEMPT_DELAYS = [0.0, 0.5, 1.0, 2.0, 4.0]
_failed_attempts = 0


def _throttle_delay() -> float:
    return _FAILED_ATTEMPT_DELAYS[min(_failed_attempts, len(_FAILED_ATTEMPT_DELAYS) - 1)]


def _to_summary(u: User) -> UserSummary:
    return UserSummary(
        user_id=u.user_id, username=u.username, email=u.email,
        status=u.status.value, role=u.role.value,
        created_at=u.created_at, approved_at=u.approved_at,
        approved_by=u.approved_by, last_login_at=u.last_login_at,
        reset_state=u.reset_state(), reset_requested_at=u.reset_requested_at,
    )


# ── Public ───────────────────────────────────────────────────────────────────

@auth_router.post("/signup", response_model=SignupResponse, status_code=201)
def signup(body: SignupRequest) -> SignupResponse:
    """Create an account. It starts PENDING and cannot sign in yet.

    Open on purpose — anyone may apply. What's gated is *use*: `login` refuses
    anything that isn't ACTIVE, and only an admin can make it ACTIVE. So
    "anyone can sign up, nobody gets in without approval" holds without this
    endpoint needing any check of its own.
    """
    with Session(engine) as session:
        try:
            user = user_service.create_user(
                session, body.username, body.password, body.email
            )
        except user_service.SignupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return SignupResponse(
        username=user.username,
        status=user.status.value,
        message="Account created and waiting for admin approval. "
                "You'll be able to sign in once it's approved.",
    )


@auth_router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    global _failed_attempts
    settings = get_settings()

    delay = _throttle_delay()
    if delay:
        time.sleep(delay)

    with Session(engine) as session:
        user, reason = user_service.authenticate(session, body.username, body.password)

        if user is None:
            _failed_attempts += 1
            detail = _STATUS_MESSAGES.get(reason, _BAD_CREDENTIALS)
            # A blocked-but-valid account is a 403: the credentials were right,
            # the account just isn't allowed in yet. 401 would tell the client
            # to re-prompt for a password that is already correct.
            code = (
                status.HTTP_401_UNAUTHORIZED if reason == "bad_credentials"
                else status.HTTP_403_FORBIDDEN
            )
            logger.warning("Login refused for %r: %s", body.username, reason)
            raise HTTPException(
                status_code=code, detail=detail, headers={"WWW-Authenticate": "Bearer"}
            )

        _failed_attempts = 0
        token = create_token(user.username, settings.auth_secret_key, settings.auth_token_ttl_hours)
        exp = token_expiry(token)
        logger.info("Login succeeded for %s (%s)", user.username, user.role.value)
        return LoginResponse(
            access_token=token,
            username=user.username,
            role=user.role.value,
            expires_at=datetime.fromtimestamp(exp, tz=timezone.utc) if exp else None,
        )


@auth_router.get("/status")
def auth_status() -> dict:
    """Public. Tells the login page whether any admin exists yet, so a brand
    new install can explain how to bootstrap one."""
    with Session(engine) as session:
        return {
            "configured": bool(get_settings().auth_secret_key)
            and user_service.count_admins(session) > 0,
            "accepts_signup": True,
        }


_RESET_REQUEST_MESSAGE = (
    "Reset requested. Keep the code below — it is shown only once. "
    "An admin has to approve the request before you can use it."
)

_RESET_FAILURE_MESSAGES = {
    "invalid": "That reset code isn't valid, or it has already been used.",
    "expired": "That reset request has expired. Request a new one.",
    "not_approved": "Your reset request is still waiting for admin approval.",
}


@auth_router.post(
    "/password-reset/request",
    response_model=PasswordResetRequestResponse,
    status_code=202,
)
def request_password_reset(body: PasswordResetRequest) -> PasswordResetRequestResponse:
    """Ask an admin to allow a password reset.

    Public, because someone who has forgotten their password cannot
    authenticate to ask. The reply is identical whether or not the account
    exists, so this cannot be used to discover usernames — the code it hands
    back is simply dead if there was nothing to record it against.
    """
    with Session(engine) as session:
        code = user_service.request_password_reset(session, body.username)
    return PasswordResetRequestResponse(code=code, message=_RESET_REQUEST_MESSAGE)


@auth_router.post("/password-reset/complete")
def complete_password_reset(body: PasswordResetCompleteRequest) -> dict:
    """Set a new password, once an admin has approved the request.

    Needs both halves: the one-time code from the request, and the admin's
    approval. Redeeming it consumes the approval, so the same code can't be
    replayed to change the password again later.
    """
    global _failed_attempts

    delay = _throttle_delay()
    if delay:
        time.sleep(delay)

    with Session(engine) as session:
        try:
            user, reason = user_service.complete_password_reset(
                session, body.username, body.code, body.new_password
            )
        except user_service.SignupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if user is None:
        # Shares the login throttle on purpose: both are unauthenticated
        # guessing games against the same accounts.
        _failed_attempts += 1
        logger.warning("Password reset refused for %r: %s", body.username, reason)
        code = (
            status.HTTP_403_FORBIDDEN if reason == "not_approved"
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=_RESET_FAILURE_MESSAGES[reason])

    _failed_attempts = 0
    return {"username": user.username, "message": "Password changed. You can sign in now."}


# ── Dependencies ─────────────────────────────────────────────────────────────

def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    token: Optional[str] = Query(default=None, include_in_schema=False),
) -> User:
    """Guards every protected route. Returns the signed-in User.

    Applied once in main.py via `include_router(..., dependencies=[...])`, so a
    router added later has to be put on that list to be reachable at all and
    can't accidentally ship unguarded.

    The `?token=` fallback exists because the browser issues some requests
    itself with no way to attach a header: resume/screenshot downloads via
    `<a href>` / `<img src>`, and the WebSocket handshake. A token in a URL can
    leak into logs and history — the accepted trade for a loopback app.

    The account is re-read from the DB on every request rather than trusted
    from the token, so revoking access (DISABLED) takes effect immediately
    instead of whenever the token happens to expire.
    """
    settings = get_settings()
    if not settings.auth_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured. Run `python scripts/set_password.py` "
                   "on the machine hosting the backend, then restart it.",
        )

    raw_token = (credentials.credentials if credentials else None) or token
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    username = decode_token(raw_token, settings.auth_secret_key)
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session — sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        )

    with Session(engine) as session:
        user = user_service.get_by_username(session, username)
        if user is None or not user.can_log_in():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="This account can no longer sign in",
                headers={"WWW-Authenticate": "Bearer"},
            )
        session.expunge(user)
    return user


def require_admin(user: User = Depends(require_auth)) -> User:
    if not user.is_admin():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


def authenticate_token(token: Optional[str]) -> Optional[User]:
    """Verify a raw token, for transports that can't send headers (WebSocket)."""
    settings = get_settings()
    if not settings.auth_secret_key or not token:
        return None
    username = decode_token(token, settings.auth_secret_key)
    if username is None:
        return None
    with Session(engine) as session:
        user = user_service.get_by_username(session, username)
        if user is None or not user.can_log_in():
            return None
        session.expunge(user)
    return user


# ── Authenticated ────────────────────────────────────────────────────────────

@auth_router.get("/me", response_model=CurrentUser)
def me(user: User = Depends(require_auth)) -> CurrentUser:
    return CurrentUser(
        user_id=user.user_id, username=user.username, email=user.email,
        role=user.role.value, status=user.status.value, is_admin=user.is_admin(),
    )


# ── Admin only ───────────────────────────────────────────────────────────────

@auth_router.get("/users", response_model=UserListResponse)
def list_users(_admin: User = Depends(require_admin)) -> UserListResponse:
    with Session(engine) as session:
        users = user_service.list_users(session)
        return UserListResponse(
            items=[_to_summary(u) for u in users],
            pending_count=sum(1 for u in users if u.status == UserStatus.PENDING),
            reset_request_count=sum(1 for u in users if u.reset_state() == "REQUESTED"),
        )


@auth_router.patch("/users/{user_id}/status", response_model=UserSummary)
def set_user_status(
    user_id: int, body: SetUserStatusRequest, admin: User = Depends(require_admin)
) -> UserSummary:
    """Approve, reject or disable an account."""
    try:
        new_status = UserStatus(body.status.upper())
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="status must be one of: ACTIVE, REJECTED, DISABLED, PENDING",
        )

    with Session(engine) as session:
        target = session.get(User, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found")

        # Guard against locking everyone out: the last active admin can't
        # disable or reject themselves, leaving nobody able to approve anyone.
        if (
            target.role == UserRole.ADMIN
            and new_status != UserStatus.ACTIVE
            and user_service.count_admins(session, exclude_user_id=user_id) == 0
        ):
            raise HTTPException(
                status_code=409,
                detail="This is the only active admin — make someone else an admin first.",
            )

        updated = user_service.set_status(
            session, user_id, new_status, by_admin=admin.username
        )
        return _to_summary(updated)


@auth_router.patch("/users/{user_id}/role", response_model=UserSummary)
def set_user_role(
    user_id: int, body: SetUserRoleRequest, admin: User = Depends(require_admin)
) -> UserSummary:
    """Promote to admin, or demote back to a normal member.

    This exists because the last-admin guard tells you to make someone else an
    admin first, and until now there was no way to do that short of running
    scripts/set_password.py on the server.
    """
    try:
        new_role = UserRole(body.role.upper())
    except ValueError:
        raise HTTPException(status_code=422, detail="role must be ADMIN or USER")

    with Session(engine) as session:
        target = session.get(User, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found")

        # Same lockout guard as status: demoting the last admin would leave
        # nobody able to approve anyone, including a replacement admin.
        if (
            target.role == UserRole.ADMIN
            and new_role != UserRole.ADMIN
            and user_service.count_admins(session, exclude_user_id=user_id) == 0
        ):
            raise HTTPException(
                status_code=409,
                detail="This is the only active admin — make someone else an admin first.",
            )

        # An admin who can't sign in is not a working admin, so promoting also
        # lets them in. Otherwise the last-admin guard could be satisfied by
        # someone who could never actually approve anything.
        if new_role == UserRole.ADMIN and target.status != UserStatus.ACTIVE:
            user_service.set_status(
                session, user_id, UserStatus.ACTIVE, by_admin=admin.username
            )

        updated = user_service.set_role(session, user_id, new_role)
        return _to_summary(updated)


@auth_router.patch("/users/{user_id}/password-reset", response_model=UserSummary)
def review_password_reset(
    user_id: int, body: SetPasswordResetRequest, admin: User = Depends(require_admin)
) -> UserSummary:
    """Approve or deny a member's password-reset request.

    Approving does not change the password — it only lets the person holding
    the one-time code set a new one. Denying forgets the request entirely, so
    a stale code can never be redeemed later.
    """
    action = body.action.strip().lower()
    if action not in ("approve", "deny"):
        raise HTTPException(status_code=422, detail="action must be 'approve' or 'deny'")

    with Session(engine) as session:
        if session.get(User, user_id) is None:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found")

        handler = (
            user_service.approve_password_reset if action == "approve"
            else user_service.deny_password_reset
        )
        updated = handler(session, user_id, by_admin=admin.username)
        if updated is None:
            raise HTTPException(
                status_code=409, detail="That account has no pending reset request."
            )
        return _to_summary(updated)


def _reset_throttle_for_tests() -> None:
    """Test-only: the failed-attempt counter is process-global and would
    otherwise leak between test cases."""
    global _failed_attempts
    _failed_attempts = 0
