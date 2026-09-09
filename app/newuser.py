"""Make an account from the command line:  python -m app.newuser

A public copy starts with an empty roster on purpose -- the demo logins in
the README would be three published passwords on the open internet. That
leaves a hosted install with nobody who can sign in, and no screen to fix
it from, because every screen is behind the sign-in. This is the way in.

After the first account exists, use Settings -> People instead; it is the
same job with a roster in front of you.
"""
import getpass
import sys

from .auth import hash_password
from .db import connect, init_db, one, x
# Imported rather than restated so the rule cannot drift from the one the
# Account screen enforces.
from .main import _password_fault


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)


def _ask_secret(prompt: str) -> str:
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)


def main() -> int:
    init_db()
    db = connect()
    try:
        live = one(db, "SELECT COUNT(*) AS n FROM users"
                       " WHERE COALESCE(disabled, 0) = 0")["n"]
        if live:
            print(f"{live} account{'s' if live != 1 else ''} already "
                  f"{'exist' if live != 1 else 'exists'}.")
            print("This makes another super admin. To add an ordinary "
                  "reviewer, use Settings -> People in the browser.")
        else:
            print("No accounts yet. This one will be the super admin.")
        print()

        username = _ask("Username (what you type to sign in): ")
        if not username:
            print("A username is needed.", file=sys.stderr)
            return 1
        if one(db, "SELECT id FROM users WHERE username = ?", (username,)):
            print(f"'{username}' is taken. Pick another.", file=sys.stderr)
            return 1

        display = _ask("Full name (shown on the reviews you record): ")
        if not display:
            display = username

        password = _ask_secret("Password: ")
        again = _ask_secret("Password again: ")
        fault = _password_fault(password, again)
        if fault:
            print(fault, file=sys.stderr)
            return 1

        # Always a super admin: the first account has to be one or nobody
        # can add the second, and the only reason to reach for a terminal
        # afterwards is that no super admin is left to let you in.
        # Ordinary reviewers are made from Settings -> People.
        x(db, "INSERT INTO users (username, password_hash, display_name,"
              " role, entity_id) VALUES (?,?,?,'superadmin',NULL)",
          (username, hash_password(password), display))
        print()
        print(f"Created '{username}' as super admin. Sign in with it now.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
