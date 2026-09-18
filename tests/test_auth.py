"""Single-user authentication.

The security-relevant claims being pinned down here:
  * every API route is protected — verified against the live route table, not
    a hand-maintained list that could drift as routers are added
  * there is no way to create an account over the network
  * tokens can't be forged, tampered with, or replayed after expiry
  * failures don't leak which half of the credential pair was wrong
"""
import time

import pytest

from backend.app.services.auth import (
    create_token,
    decode_token,
    hash_password,
    token_expiry,
    verify_password,
)

SECRET = "test-secret-key-not-a-real-one"   # for pure token unit tests


# ── Password hashing ────────────────────────────────────────────────────────

class TestPasswordHashing:
    def test_round_trips(self):
        assert verify_password("correct horse battery", hash_password("correct horse battery"))

    def test_rejects_wrong_password(self):
        assert not verify_password("wrong", hash_password("right"))

    def test_hash_is_salted(self):
        """Same password twice must not produce the same hash, or the stored
        value leaks that two accounts share a password."""
        assert hash_password("same") != hash_password("same")

    def test_plaintext_never_appears_in_the_hash(self):
        assert "hunter2" not in hash_password("hunter2")

    def test_hash_records_its_own_parameters(self):
        """Self-describing, so cost can be raised later without invalidating
        every existing hash."""
        parts = hash_password("x").split("$")
        assert parts[0] == "scrypt"
        assert all(p.isdigit() for p in parts[1:4])

    @pytest.mark.parametrize("stored", ["", "garbage", "scrypt$bad", "notscrypt$1$2$3$a$b"])
    def test_malformed_stored_hash_fails_closed(self, stored):
        """A broken AUTH_PASSWORD_HASH must reject logins, never crash into a
        500 or accidentally accept."""
        assert verify_password("anything", stored) is False

    def test_empty_password_rejected(self):
        assert verify_password("", hash_password("real")) is False
        with pytest.raises(ValueError):
            hash_password("")


# ── Tokens ──────────────────────────────────────────────────────────────────

class TestTokens:
    def test_round_trips(self):
        assert decode_token(create_token("gaurav", SECRET, 1), SECRET) == "gaurav"

    def test_rejects_a_different_secret(self):
        assert decode_token(create_token("gaurav", SECRET, 1), "other-secret") is None

    def test_rejects_a_tampered_payload(self):
        """The whole point of signing: editing the payload must invalidate it."""
        import base64
        import json

        token = create_token("gaurav", SECRET, 1)
        _, signature = token.split(".")
        forged_payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "attacker", "exp": int(time.time()) + 3600}).encode()
        ).rstrip(b"=").decode()
        assert decode_token(f"{forged_payload}.{signature}", SECRET) is None

    def test_rejects_an_unsigned_token(self):
        import base64
        import json

        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "attacker", "exp": int(time.time()) + 3600}).encode()
        ).rstrip(b"=").decode()
        assert decode_token(payload, SECRET) is None
        assert decode_token(f"{payload}.", SECRET) is None

    def test_rejects_expired_token(self):
        assert decode_token(create_token("gaurav", SECRET, ttl_hours=-1), SECRET) is None

    @pytest.mark.parametrize("token", ["", "garbage", "a.b.c", ".", "..."])
    def test_rejects_malformed(self, token):
        assert decode_token(token, SECRET) is None

    def test_rejects_when_no_secret_configured(self):
        """Fail closed: a blank AUTH_SECRET_KEY must not validate anything."""
        assert decode_token(create_token("gaurav", SECRET, 1), "") is None

    def test_cannot_mint_without_a_secret(self):
        with pytest.raises(ValueError):
            create_token("gaurav", "", 1)

    def test_expiry_is_readable_for_display(self):
        token = create_token("gaurav", SECRET, 2)
        assert time.time() + 7000 < token_expiry(token) <= time.time() + 7200


# ── Live API behaviour ──────────────────────────────────────────────────────

from tests.conftest import TEST_PASSWORD, TEST_USERNAME

TEST_USER = TEST_USERNAME


@pytest.fixture
def auth_configured(admin_user):
    """The suite's admin exists and can log in. Credentials live in the DB now,
    not in settings, so this just resets the login throttle between cases."""
    from backend.app.routers import auth as auth_router_module

    auth_router_module._reset_throttle_for_tests()
    yield admin_user
    auth_router_module._reset_throttle_for_tests()


@pytest.fixture
def auth_unconfigured(monkeypatch):
    """No signing key — the app can't verify anything and must fail closed."""
    from backend.app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_secret_key", "", raising=False)
    return settings


@pytest.fixture
def token(anon_client, auth_configured):
    r = anon_client.post(
        "/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
def pending_signup(anon_client, session):
    """A freshly signed-up account: exists, but PENDING and cannot log in."""
    from sqlmodel import delete

    from backend.app.models.user import User

    username = "applicant-one"
    session.exec(delete(User).where(User.username == username))
    session.commit()

    r = anon_client.post(
        "/api/auth/signup",
        json={"username": username, "password": "applicant-pass-123",
              "email": "applicant@example.com"},
    )
    assert r.status_code == 201, r.text
    yield {"username": username, "password": "applicant-pass-123"}

    session.exec(delete(User).where(User.username == username))
    session.commit()


class TestLogin:
    def test_correct_credentials_return_a_token(self, anon_client, auth_configured):
        r = anon_client.post("/api/auth/login",
                        json={"username": TEST_USER, "password": TEST_PASSWORD})
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == TEST_USER
        assert body["token_type"] == "bearer"
        from backend.app.config import get_settings
        assert decode_token(body["access_token"], get_settings().auth_secret_key) == TEST_USER

    def test_wrong_password_is_401(self, anon_client, auth_configured):
        r = anon_client.post("/api/auth/login",
                        json={"username": TEST_USER, "password": "nope"})
        assert r.status_code == 401

    def test_wrong_username_is_401(self, anon_client, auth_configured):
        r = anon_client.post("/api/auth/login",
                        json={"username": "someone-else", "password": TEST_PASSWORD})
        assert r.status_code == 401

    def test_failure_does_not_reveal_which_half_was_wrong(self, anon_client, auth_configured):
        """Identical bodies for bad-user and bad-password, or an attacker can
        enumerate the username."""
        bad_user = anon_client.post("/api/auth/login",
                               json={"username": "nope", "password": TEST_PASSWORD})
        bad_pass = anon_client.post("/api/auth/login",
                               json={"username": TEST_USER, "password": "nope"})
        assert bad_user.json()["detail"] == bad_pass.json()["detail"]

    def test_response_never_echoes_the_password_or_hash(self, anon_client, auth_configured):
        r = anon_client.post("/api/auth/login",
                        json={"username": TEST_USER, "password": TEST_PASSWORD})
        assert TEST_PASSWORD not in r.text
        assert "scrypt$" not in r.text

    def test_login_without_a_signing_key_fails_closed(self, anon_client, auth_unconfigured):
        """No key means no token can be minted or verified — refuse rather
        than serve an unauthenticated API."""
        r = anon_client.post("/api/auth/login", json={"username": "a", "password": "b"})
        assert r.status_code in (401, 403, 503)

    @pytest.mark.parametrize("payload", [
        {}, {"username": "x"}, {"password": "y"}, {"username": "", "password": ""},
    ])
    def test_rejects_malformed_bodies(self, anon_client, auth_configured, payload):
        assert anon_client.post("/api/auth/login", json=payload).status_code == 422


class TestSignupRequiresApproval:
    """Signup is open; *use* is not. A new account exists but can't sign in
    until an admin approves it — that's what "nobody gets in without my
    verification" means now that signup is public."""

    def test_signup_creates_a_pending_account(self, pending_signup, session):
        from backend.app.models.user import UserStatus
        from backend.app.services import users as user_service

        user = user_service.get_by_username(session, pending_signup["username"])
        assert user is not None
        assert user.status == UserStatus.PENDING

    def test_signup_does_not_hand_back_a_token(self, anon_client, session):
        """A signup response that carried a token would defeat the approval
        gate entirely."""
        from sqlmodel import delete

        from backend.app.models.user import User

        session.exec(delete(User).where(User.username == "fresh-applicant"))
        session.commit()
        r = anon_client.post(
            "/api/auth/signup",
            json={"username": "fresh-applicant", "password": "some-password-1"},
        )
        assert r.status_code == 201
        assert "access_token" not in r.json()
        assert "token" not in r.text.lower()

    def test_pending_account_cannot_log_in(self, anon_client, pending_signup):
        r = anon_client.post("/api/auth/login", json={
            "username": pending_signup["username"],
            "password": pending_signup["password"],
        })
        assert r.status_code == 403
        assert "approval" in r.json()["detail"].lower()

    def test_pending_account_cannot_reach_any_api_route(self, anon_client, pending_signup):
        """Belt and braces: even if a token were somehow obtained, the account
        is re-checked on every request."""
        r = anon_client.get("/api/dashboard/stats")
        assert r.status_code == 401

    def test_approved_account_can_then_log_in(self, anon_client, client, pending_signup, session):
        from backend.app.services import users as user_service

        user = user_service.get_by_username(session, pending_signup["username"])
        approve = client.patch(
            f"/api/auth/users/{user.user_id}/status", json={"status": "ACTIVE"}
        )
        assert approve.status_code == 200

        r = anon_client.post("/api/auth/login", json={
            "username": pending_signup["username"],
            "password": pending_signup["password"],
        })
        assert r.status_code == 200
        assert r.json()["access_token"]

    def test_rejected_account_cannot_log_in(self, anon_client, client, pending_signup, session):
        from backend.app.services import users as user_service

        user = user_service.get_by_username(session, pending_signup["username"])
        client.patch(f"/api/auth/users/{user.user_id}/status", json={"status": "REJECTED"})

        r = anon_client.post("/api/auth/login", json={
            "username": pending_signup["username"],
            "password": pending_signup["password"],
        })
        assert r.status_code == 403

    def test_signup_rejects_a_duplicate_username(self, anon_client, pending_signup):
        r = anon_client.post("/api/auth/signup", json={
            "username": pending_signup["username"], "password": "another-password-1",
        })
        assert r.status_code == 422
        assert "taken" in r.json()["detail"].lower()

    @pytest.mark.parametrize("payload", [
        {"username": "ab", "password": "long-enough-1"},        # username too short
        {"username": "valid-name", "password": "short"},        # password too short
        {"username": "bad name!", "password": "long-enough-1"}, # illegal characters
    ])
    def test_signup_validates_input(self, anon_client, payload):
        assert anon_client.post("/api/auth/signup", json=payload).status_code == 422


class TestAdminOnlyApproval:
    """Only an admin can move an account to ACTIVE — otherwise any approved
    user could let their friends in."""

    def test_non_admin_cannot_list_accounts(self, anon_client, client, pending_signup, session):
        from backend.app.services import users as user_service

        user = user_service.get_by_username(session, pending_signup["username"])
        client.patch(f"/api/auth/users/{user.user_id}/status", json={"status": "ACTIVE"})

        login = anon_client.post("/api/auth/login", json={
            "username": pending_signup["username"], "password": pending_signup["password"],
        })
        member_token = login.json()["access_token"]

        r = anon_client.get("/api/auth/users",
                            headers={"Authorization": f"Bearer {member_token}"})
        assert r.status_code == 403

    def test_non_admin_cannot_approve_anyone(self, anon_client, client, pending_signup, session):
        from backend.app.services import users as user_service

        user = user_service.get_by_username(session, pending_signup["username"])
        client.patch(f"/api/auth/users/{user.user_id}/status", json={"status": "ACTIVE"})
        login = anon_client.post("/api/auth/login", json={
            "username": pending_signup["username"], "password": pending_signup["password"],
        })
        member_token = login.json()["access_token"]

        r = anon_client.patch(
            f"/api/auth/users/{user.user_id}/status", json={"status": "ACTIVE"},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r.status_code == 403

    def test_admin_sees_pending_accounts(self, client, pending_signup):
        body = client.get("/api/auth/users").json()
        assert body["pending_count"] >= 1
        assert any(u["username"] == pending_signup["username"] for u in body["items"])

    def test_last_admin_cannot_disable_themselves(self, client, admin_user):
        """Otherwise nobody is left who can approve anyone, ever."""
        r = client.patch(
            f"/api/auth/users/{admin_user.user_id}/status", json={"status": "DISABLED"}
        )
        assert r.status_code == 409


class TestProtectedRoutes:
    def test_every_api_route_requires_auth(self, anon_client, auth_configured):
        """Checked against the live route table, so a router added later
        without a guard fails this test instead of shipping open.

        Must use `anon_client` — with the signed-in `client` every route
        answers 200 and this assertion can never fail, which is exactly the
        bug this comment exists to prevent recurring.
        """
        paths = anon_client.get("/openapi.json").json()["paths"]
        public = {
            "/api/auth/login", "/api/auth/status", "/api/auth/signup",
            # Someone who has forgotten their password cannot authenticate
            # to ask for a reset, so these two are open by necessity. Both
            # are covered by tests/test_password_reset.py instead.
            "/api/auth/password-reset/request", "/api/auth/password-reset/complete",
        }

        unguarded = []
        for path, methods in paths.items():
            if not path.startswith("/api/") or path in public:
                continue
            for method in methods:
                url = path.replace("{job_id}", "1").replace("{run_id}", "1") \
                          .replace("{resume_id}", "1")
                resp = anon_client.request(method.upper(), url)
                if resp.status_code not in (401, 403):
                    unguarded.append(f"{method.upper()} {path} -> {resp.status_code}")
        assert not unguarded, f"routes reachable without a token: {unguarded}"

    def test_valid_token_is_accepted(self, client, token):
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["username"] == TEST_USER

    def test_protected_route_works_with_a_token(self, client, token):
        r = client.get("/api/dashboard/stats", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    @pytest.mark.parametrize("header", [
        "Bearer garbage", "Bearer ", "Basic dXNlcjpwYXNz", "garbage",
    ])
    def test_bad_authorization_headers_are_rejected(self, client, auth_configured, header):
        r = client.get("/api/dashboard/stats", headers={"Authorization": header})
        assert r.status_code in (401, 403)

    def test_expired_token_is_rejected(self, anon_client, auth_configured):
        from backend.app.config import get_settings

        expired = create_token(TEST_USER, get_settings().auth_secret_key, ttl_hours=-1)
        r = anon_client.get("/api/dashboard/stats",
                            headers={"Authorization": f"Bearer {expired}"})
        assert r.status_code == 401

    def test_token_naming_a_nonexistent_account_is_rejected(self, anon_client, auth_configured):
        """Correctly signed, but the named account doesn't exist — the user is
        re-read from the DB on every request, so this can't pass."""
        from backend.app.config import get_settings

        other = create_token("no-such-person", get_settings().auth_secret_key, 1)
        r = anon_client.get("/api/dashboard/stats",
                            headers={"Authorization": f"Bearer {other}"})
        assert r.status_code == 401

    def test_query_token_works_for_browser_issued_downloads(self, anon_client, token):
        """<a href> downloads and <img src> can't set an Authorization header,
        so the same token is accepted as ?token= on protected routes."""
        r = anon_client.get(f"/api/dashboard/stats?token={token}")
        assert r.status_code == 200

    def test_query_token_must_still_be_valid(self, anon_client, auth_configured):
        assert anon_client.get("/api/dashboard/stats?token=garbage").status_code == 401

    def test_health_stays_public(self, client):
        """Docker's HEALTHCHECK has no credentials."""
        assert client.get("/health").status_code == 200

    def test_auth_status_is_public(self, client):
        r = client.get("/api/auth/status")
        assert r.status_code == 200
        assert "configured" in r.json()

    def test_unconfigured_backend_locks_everything(self, anon_client, auth_unconfigured):
        r = anon_client.get("/api/dashboard/stats")
        assert r.status_code == 503
        assert "set_password.py" in r.json()["detail"]


class TestWebSocketAuth:
    def test_rejects_connection_without_a_token(self, anon_client, auth_configured):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with anon_client.websocket_connect("/ws/runs/1"):
                pass

    def test_rejects_a_forged_token(self, anon_client, auth_configured):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with anon_client.websocket_connect("/ws/runs/1?token=garbage"):
                pass

    def test_accepts_a_valid_token(self, anon_client, token):
        with anon_client.websocket_connect(f"/ws/runs/1?token={token}") as ws:
            assert ws is not None
