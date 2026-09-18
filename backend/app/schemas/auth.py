from __future__ import annotations

from typing import Optional

from backend.app.schemas.types import UtcDatetime
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=150)
    password: str = Field(..., min_length=1, max_length=1024)


class SignupRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=150)
    password: str = Field(..., min_length=8, max_length=1024)
    email: Optional[str] = Field(default=None, max_length=254)


class SignupResponse(BaseModel):
    """No token here on purpose — a new account is PENDING and cannot log in
    until an admin approves it."""

    username: str
    status: str
    message: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: Optional[UtcDatetime] = None
    username: str
    role: str


class CurrentUser(BaseModel):
    user_id: int
    username: str
    email: Optional[str] = None
    role: str
    status: str
    authenticated: bool = True
    is_admin: bool = False


class UserSummary(BaseModel):
    """One row in the admin's account list."""

    user_id: int
    username: str
    email: Optional[str] = None
    status: str
    role: str
    created_at: UtcDatetime
    approved_at: Optional[UtcDatetime] = None
    approved_by: Optional[str] = None
    last_login_at: Optional[UtcDatetime] = None
    reset_state: str = Field(
        default="NONE", description="NONE / REQUESTED / APPROVED - password-reset request"
    )
    reset_requested_at: Optional[UtcDatetime] = None


class UserListResponse(BaseModel):
    items: list[UserSummary]
    pending_count: int
    reset_request_count: int = 0


class SetUserStatusRequest(BaseModel):
    status: str = Field(..., description="ACTIVE / REJECTED / DISABLED")


class SetUserRoleRequest(BaseModel):
    role: str = Field(..., description="ADMIN / USER")


class PasswordResetRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=150)


class PasswordResetRequestResponse(BaseModel):
    """The code is returned to whoever asked, once, and never stored in the
    clear. It is useless on its own - an admin still has to approve the
    request before it can be redeemed."""

    code: str
    message: str


class PasswordResetCompleteRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=150)
    code: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=1024)


class SetPasswordResetRequest(BaseModel):
    action: str = Field(..., description="approve / deny")
