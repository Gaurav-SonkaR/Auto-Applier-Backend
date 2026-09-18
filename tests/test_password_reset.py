"""Password reset, and the promote/demote path that makes the last-admin guard
actionable.

The claims being pinned down:
  * a reset needs *both* the one-time code and an admin's approval — neither
    half is enough on its own
  * redeeming it spends the approval, so the code can't be replayed
  * the request endpoint answers identically for real and made-up usernames,
    so it can't be used to enumerate accounts
  * an admin can be created over the network only by another admin, and the
    last one can't be demoted or disabled away
"""
from __future__ import annotations

import pytest
from sqlmodel import delete

from backend.app.models.user import User, UserRole, UserStatus
from backend.app.services import users as user_service

MEMBER_PASSWORD = "member-password-1"
NEW_PASSWORD = "brand-new-password-9"


@pytest.fixture(autouse=True)
def _no_throttle():
    """The failed-attempt damper is process-global and sleeps for seconds;
    these tests fail on purpose a lot."""
    from backend.app.routers import auth as auth_router_module

    auth_router_module._reset_throttle_for_tests()
    yield
    auth_router_module._reset_throttle_for_tests()


@pytest.fixture
def member(anon_client, client, session):
    """An ACTIVE, non-admin account — the one that forgets its password."""
    username = "reset-member"
    session.exec(delete(User).where(User.username == username))
    session.commit()

    anon_client.post("/api/auth/signup", json={
        "username": username, "password": MEMBER_PASSWORD, "email": "m@example.com",
    })
    user = user_service.get_by_username(session, username)
    client.patch(f"/api/auth/users/{user.user_id}/status", json={"status": "ACTIVE"})
    session.refresh(user)

    yield {"username": username, "user_id": user.user_id, "password": MEMBER_PASSWORD}

    session.exec(delete(User).where(User.username == username))
    session.commit()


def _request_code(anon_client, username: str) -> str:
    r = anon_client.post("/api/auth/password-reset/request", json={"username": username})
    assert r.status_code == 202, r.text
    return r.json()["code"]


def _complete(anon_client, username: str, code: str, password: str = NEW_PASSWORD):
    return anon_client.post("/api/auth/password-reset/complete", json={
        "username": username, "code": code, "new_password": password,
    })


def _approve(client, user_id: int, action: str = "approve"):
    return client.patch(f"/api/auth/users/{user_id}/password-reset", json={"action": action})


# ── The happy path ──────────────────────────────────────────────────────────

class TestApprovedReset:
    def test_full_flow_request_approve_reset_login(self, anon_client, client, member):
        code = _request_code(anon_client, member["username"])
        assert _approve(client, member["user_id"]).status_code == 200

        assert _complete(anon_client, member["username"], code).status_code == 200

        # The new password works…
        ok = anon_client.post("/api/auth/login", json={
            "username": member["username"], "password": NEW_PASSWORD,
        })
        assert ok.status_code == 200, ok.text

        # …and the old one does not.
        old = anon_client.post("/api/auth/login", json={
            "username": member["username"], "password": member["password"],
        })
        assert old.status_code == 401

    def test_approval_is_spent_once_used(self, anon_client, client, member, session):
        """The whole point of clearing the request on success: otherwise the
        same code could be replayed to change the password again later without
        anybody approving it."""
        code = _request_code(anon_client, member["username"])
        _approve(client, member["user_id"])
        assert _complete(anon_client, member["username"], code).status_code == 200

        again = _complete(anon_client, member["username"], code, "another-password-2")
        assert again.status_code == 400

        user = user_service.get_by_username(session, member["username"])
        assert user.reset_state() == "NONE"

    def test_admin_list_shows_the_request_then_clears_it(self, anon_client, client, member):
        _request_code(anon_client, member["username"])
        body = client.get("/api/auth/users").json()
        row = next(u for u in body["items"] if u["username"] == member["username"])
        assert row["reset_state"] == "REQUESTED"
        assert body["reset_request_count"] >= 1

        _approve(client, member["user_id"])
        row = next(
            u for u in client.get("/api/auth/users").json()["items"]
            if u["username"] == member["username"]
        )
        assert row["reset_state"] == "APPROVED"


# ── Both halves are required ────────────────────────────────────────────────

class TestResetNeedsBothHalves:
    def test_code_alone_is_not_enough(self, anon_client, member):
        """No admin approval yet — this is the guarantee the user asked for."""
        code = _request_code(anon_client, member["username"])
        r = _complete(anon_client, member["username"], code)
        assert r.status_code == 403
        assert "approval" in r.json()["detail"].lower()

        # And the old password still works, i.e. nothing was changed.
        assert anon_client.post("/api/auth/login", json={
            "username": member["username"], "password": member["password"],
        }).status_code == 200

    def test_approval_alone_is_not_enough(self, anon_client, client, member):
        """Without the code, an approved request can't be redeemed by whoever
        happens to notice it — which is the hole the code exists to close."""
        _request_code(anon_client, member["username"])
        _approve(client, member["user_id"])

        r = _complete(anon_client, member["username"], "guessed-code")
        assert r.status_code == 400
        assert anon_client.post("/api/auth/login", json={
            "username": member["username"], "password": member["password"],
        }).status_code == 200

    def test_reset_with_no_request_at_all_fails(self, anon_client, member):
        assert _complete(anon_client, member["username"], "anything").status_code == 400

    def test_a_second_request_invalidates_the_first_code(self, anon_client, client, member):
        first = _request_code(anon_client, member["username"])
        second = _request_code(anon_client, member["username"])
        _approve(client, member["user_id"])

        assert _complete(anon_client, member["username"], first).status_code == 400
        assert _complete(anon_client, member["username"], second).status_code == 200

    def test_re_requesting_drops_an_existing_approval(self, anon_client, client, member, session):
        """The admin approved the *old* request; a new one starts over."""
        _request_code(anon_client, member["username"])
        _approve(client, member["user_id"])
        fresh = _request_code(anon_client, member["username"])

        user = user_service.get_by_username(session, member["username"])
        session.refresh(user)
        assert user.reset_state() == "REQUESTED"
        assert _complete(anon_client, member["username"], fresh).status_code == 403

    def test_expired_request_is_refused(self, anon_client, client, member, session):
        from datetime import datetime, timedelta, timezone

        code = _request_code(anon_client, member["username"])
        _approve(client, member["user_id"])

        user = user_service.get_by_username(session, member["username"])
        user.reset_requested_at = datetime.now(timezone.utc) - timedelta(
            hours=user_service.PASSWORD_RESET_TTL_HOURS + 1
        )
        session.add(user)
        session.commit()

        r = _complete(anon_client, member["username"], code)
        assert r.status_code == 400
        assert "expired" in r.json()["detail"].lower()

    def test_new_password_must_meet_the_length_rule(self, anon_client, client, member):
        code = _request_code(anon_client, member["username"])
        _approve(client, member["user_id"])
        assert _complete(anon_client, member["username"], code, "short").status_code == 422


# ── Leaks ───────────────────────────────────────────────────────────────────

class TestResetDoesNotLeak:
    def test_unknown_username_looks_identical(self, anon_client, member):
        real = anon_client.post("/api/auth/password-reset/request",
                                json={"username": member["username"]})
        fake = anon_client.post("/api/auth/password-reset/request",
                                json={"username": "no-such-person-at-all"})
        assert real.status_code == fake.status_code == 202
        assert real.json()["message"] == fake.json()["message"]
        assert real.json()["code"] != fake.json()["code"]

    def test_a_code_for_an_unknown_username_never_works(self, anon_client):
        code = _request_code(anon_client, "no-such-person-at-all")
        assert _complete(anon_client, "no-such-person-at-all", code).status_code == 400

    def test_wrong_code_does_not_reveal_that_a_reset_is_pending(self, anon_client, member):
        """'Waiting for approval' is only shown to someone holding the code —
        otherwise it would tell a stranger that this account exists and has a
        reset in flight."""
        _request_code(anon_client, member["username"])
        r = _complete(anon_client, member["username"], "not-the-code")
        assert r.status_code == 400
        assert "approval" not in r.json()["detail"].lower()

    def test_the_stored_code_is_hashed(self, anon_client, member, session):
        code = _request_code(anon_client, member["username"])
        user = user_service.get_by_username(session, member["username"])
        session.refresh(user)
        assert user.reset_code_hash and code not in user.reset_code_hash
        assert user.reset_code_hash.startswith("scrypt$")


class TestResetIsAdminGated:
    def test_member_cannot_approve_their_own_reset(self, anon_client, member):
        _request_code(anon_client, member["username"])
        login = anon_client.post("/api/auth/login", json={
            "username": member["username"], "password": member["password"],
        })
        token = login.json()["access_token"]

        r = anon_client.patch(
            f"/api/auth/users/{member['user_id']}/password-reset",
            json={"action": "approve"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    def test_anonymous_cannot_approve(self, anon_client, member):
        _request_code(anon_client, member["username"])
        r = anon_client.patch(f"/api/auth/users/{member['user_id']}/password-reset",
                              json={"action": "approve"})
        assert r.status_code == 401

    def test_deny_forgets_the_request(self, anon_client, client, member, session):
        code = _request_code(anon_client, member["username"])
        assert _approve(client, member["user_id"], "deny").status_code == 200

        user = user_service.get_by_username(session, member["username"])
        session.refresh(user)
        assert user.reset_state() == "NONE"
        assert _complete(anon_client, member["username"], code).status_code == 400

    def test_approving_a_nonexistent_request_is_409(self, client, member):
        assert _approve(client, member["user_id"]).status_code == 409

    def test_unknown_action_is_422(self, client, member):
        assert _approve(client, member["user_id"], "maybe").status_code == 422

    def test_disabled_account_cannot_request_a_reset(
        self, anon_client, client, member, session
    ):
        """A reset must not be a way back in for access that was revoked."""
        client.patch(f"/api/auth/users/{member['user_id']}/status",
                     json={"status": "DISABLED"})
        code = _request_code(anon_client, member["username"])

        user = user_service.get_by_username(session, member["username"])
        session.refresh(user)
        assert user.reset_state() == "NONE"
        assert _complete(anon_client, member["username"], code).status_code == 400


# ── Promote / demote ────────────────────────────────────────────────────────

class TestRoleChanges:
    def test_admin_can_promote_a_member(self, client, member, session):
        r = client.patch(f"/api/auth/users/{member['user_id']}/role", json={"role": "ADMIN"})
        assert r.status_code == 200
        assert r.json()["role"] == "ADMIN"

        user = user_service.get_by_username(session, member["username"])
        session.refresh(user)
        assert user.is_admin()

    def test_promoted_member_can_then_approve_others(self, anon_client, client, member):
        client.patch(f"/api/auth/users/{member['user_id']}/role", json={"role": "ADMIN"})
        token = anon_client.post("/api/auth/login", json={
            "username": member["username"], "password": member["password"],
        }).json()["access_token"]

        r = anon_client.get("/api/auth/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    def test_promoting_a_pending_account_also_activates_it(
        self, anon_client, client, session
    ):
        """An admin who can't sign in isn't a working admin, and would satisfy
        the last-admin guard without being able to approve anything."""
        username = "pending-promotee"
        session.exec(delete(User).where(User.username == username))
        session.commit()
        anon_client.post("/api/auth/signup",
                         json={"username": username, "password": "pending-pass-1"})
        user = user_service.get_by_username(session, username)

        r = client.patch(f"/api/auth/users/{user.user_id}/role", json={"role": "ADMIN"})
        assert r.status_code == 200
        assert r.json()["status"] == "ACTIVE"

        session.exec(delete(User).where(User.username == username))
        session.commit()

    def test_member_cannot_promote_themselves(self, anon_client, member):
        token = anon_client.post("/api/auth/login", json={
            "username": member["username"], "password": member["password"],
        }).json()["access_token"]

        r = anon_client.patch(
            f"/api/auth/users/{member['user_id']}/role", json={"role": "ADMIN"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    def test_last_admin_cannot_be_demoted(self, client, admin_user):
        r = client.patch(f"/api/auth/users/{admin_user.user_id}/role", json={"role": "USER"})
        assert r.status_code == 409

    def test_demotion_works_once_a_second_admin_exists(self, client, member, admin_user, session):
        """And this is what the last-admin 409 message now actually tells you
        to do — before the role endpoint existed it was unfollowable advice."""
        client.patch(f"/api/auth/users/{member['user_id']}/role", json={"role": "ADMIN"})
        r = client.patch(f"/api/auth/users/{member['user_id']}/role", json={"role": "USER"})
        assert r.status_code == 200
        assert r.json()["role"] == "USER"

        # The original admin is still the only one, so still undemotable.
        assert client.patch(
            f"/api/auth/users/{admin_user.user_id}/role", json={"role": "USER"}
        ).status_code == 409

    def test_unknown_role_is_422(self, client, member):
        assert client.patch(
            f"/api/auth/users/{member['user_id']}/role", json={"role": "SUPERUSER"}
        ).status_code == 422

    def test_unknown_user_is_404(self, client):
        assert client.patch("/api/auth/users/999999/role",
                            json={"role": "ADMIN"}).status_code == 404


class TestScriptDoesNotSilentlyPromote:
    """Resetting a member's forgotten password used to make them an admin,
    because the script had only one code path and it was `ensure_admin`."""

    def test_set_user_password_leaves_role_and_status_alone(self, member, session):
        user = user_service.set_user_password(session, member["username"], NEW_PASSWORD)
        assert user.role == UserRole.USER
        assert user.status == UserStatus.ACTIVE

    def test_set_user_password_verifies_against_the_new_password(self, member, session):
        from backend.app.services.auth import verify_password

        user = user_service.set_user_password(session, member["username"], NEW_PASSWORD)
        assert verify_password(NEW_PASSWORD, user.password_hash)
        assert not verify_password(member["password"], user.password_hash)

    def test_set_user_password_on_an_unknown_account_returns_none(self, session):
        assert user_service.set_user_password(session, "nobody-here", NEW_PASSWORD) is None

    def test_ensure_admin_still_promotes_when_asked(self, member, session):
        """--make-admin has to keep working, or the bootstrap breaks."""
        user = user_service.ensure_admin(session, member["username"], NEW_PASSWORD)
        assert user.role == UserRole.ADMIN
        assert user.status == UserStatus.ACTIVE

    def test_a_pending_reset_is_dropped_by_a_password_reset(
        self, anon_client, member, session
    ):
        """Otherwise an admin-side reset would leave a live request behind."""
        _request_code(anon_client, member["username"])
        # The request above wrote through the app's own session; this one still
        # holds the pre-request copy. The script runs on a fresh session, so
        # drop the stale identity map rather than testing a staleness artefact.
        session.expire_all()
        user = user_service.set_user_password(session, member["username"], NEW_PASSWORD)
        assert user.reset_state() == "NONE"
