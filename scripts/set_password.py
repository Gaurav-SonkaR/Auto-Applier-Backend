#!/usr/bin/env python3
"""Create or reset the ADMIN account, and the token signing key.

Other people sign up through the app; their accounts land in PENDING and can't
log in until an admin approves them. This script is how the *first* admin comes
into existence — it has to be run on the machine hosting the app, so admin
access can never be provisioned over the network.

Re-running it resets that account's password (useful if you lock yourself
out). Resetting an *existing* account leaves its role alone: use --make-admin
if you actually mean to promote someone, so recovering a member's forgotten
password can't hand them the admin panel by accident.

    python scripts/set_password.py                 # prompts for both
    python scripts/set_password.py --username me   # prompts for password only
    python scripts/set_password.py --username bob --make-admin   # promote bob
    python scripts/set_password.py --rotate-secret # new signing key, logs out
                                                   # every existing session

    # Non-interactive (Docker/provisioning). Prefer piping over an argument so
    # the password never lands in shell history or the process table:
    echo "$PASSWORD" | python scripts/set_password.py --username me --password-stdin

Writes AUTH_SECRET_KEY into .env (the token signing key) and the admin
account into the database. The password itself is never written anywhere —
only its scrypt hash.
"""
from __future__ import annotations

import argparse
import getpass
import re
import secrets
import sys
from pathlib import Path

# Import the same hashing the server verifies against, rather than a second
# copy that could drift out of sync with it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.database import engine, init  # noqa: E402
from backend.app.services import users as user_service  # noqa: E402
from sqlmodel import Session  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

MIN_PASSWORD_LENGTH = 8


def upsert_env(path: Path, values: dict[str, str]) -> None:
    """Set each key in .env, replacing any existing line for it.

    Rewrites in place rather than appending, so running this twice doesn't
    leave two AUTH_SECRET_KEY lines where the loser silently wins.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)

    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# Authentication (written by scripts/set_password.py)")
        out.extend(f"{k}={v}" for k, v in remaining.items())

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def prompt_password() -> str:
    while True:
        password = getpass.getpass("Password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"  Too short - use at least {MIN_PASSWORD_LENGTH} characters.\n")
            continue
        if password != getpass.getpass("Confirm password: "):
            print("  Passwords didn't match. Try again.\n")
            continue
        return password


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or reset the AutoApply AI admin account.")
    parser.add_argument("--username", help="Login username (prompted if omitted)")
    parser.add_argument(
        "--rotate-secret", action="store_true",
        help="Also generate a new signing key, invalidating all existing sessions",
    )
    parser.add_argument(
        "--password-stdin", action="store_true",
        help="Read the password from stdin instead of prompting (for scripted setup)",
    )
    parser.add_argument(
        "--make-admin", action="store_true",
        help="Also promote this account to ADMIN (implied when creating the first one)",
    )
    args = parser.parse_args()

    print(f"Writing credentials to {ENV_PATH}\n")

    username = args.username or input("Username: ").strip()
    if not username:
        print("Username must not be empty.", file=sys.stderr)
        return 1
    if not re.fullmatch(r"[A-Za-z0-9._@-]{1,150}", username):
        print("Username may only contain letters, digits and . _ @ -", file=sys.stderr)
        return 1

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
            return 1
    else:
        password = prompt_password()

    # The signing key stays in .env — it isn't a credential, and the app needs
    # it before it touches the database. The account itself lives in the DB,
    # since accounts are now created at runtime by signup.
    values: dict[str, str] = {}
    existing = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    has_secret = re.search(r"^AUTH_SECRET_KEY=.+$", existing, re.M) is not None
    if args.rotate_secret or not has_secret:
        values["AUTH_SECRET_KEY"] = secrets.token_urlsafe(48)
    if values:
        upsert_env(ENV_PATH, values)

    init()
    with Session(engine) as session:
        existing = user_service.get_by_username(session, username)
        # Promote only when asked, when creating the account, or when there is
        # no admin at all - otherwise resetting a member's forgotten password
        # would quietly make them an admin.
        promote = (
            args.make_admin
            or existing is None
            or user_service.count_admins(session) == 0
        )
        try:
            if promote:
                user = user_service.ensure_admin(session, username, password)
            else:
                user = user_service.set_user_password(session, username, password)
        except user_service.SignupError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if promote:
        print(f"\nDone. Admin account '{user.username}' is ACTIVE.")
    else:
        print(f"\nDone. Password reset for '{user.username}' "
              f"(role {user.role.value}, status {user.status.value} - unchanged).")
        print("  Pass --make-admin if you meant to promote this account.")
    if "AUTH_SECRET_KEY" in values:
        print("  A new signing key was written to .env - existing sessions are signed out.")
    print("  Restart the backend for this to take effect.")
    print("  The password itself was not saved anywhere; only its scrypt hash.")
    print("\n  Others can sign up in the app; approve them under Settings > Accounts.")
    print("  Forgotten passwords are handled in the app too - the member asks,")
    print("  an admin approves, and only then can they set a new one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
