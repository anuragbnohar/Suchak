"""Password hashing and session lookup. Deliberately simple for the
prototype; swap for SSO/Keycloak before production."""
import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request

from .db import one

_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS)
        return hmac.compare_digest(dk.hex(), expected)
    except (ValueError, TypeError):
        return False


def get_user(db, request: Request):
    """The signed-in user, or None.

    A disabled account reads as signed out, so removing someone ends the
    session they already hold rather than waiting for them to sign out.
    """
    uid = request.session.get("uid")
    if not uid:
        return None
    return one(db, "SELECT * FROM users WHERE id = ? AND COALESCE(disabled, 0) = 0",
               (uid,))


# What a read-only account may still do to itself. Somebody who can never
# change their own password can never be given one that is theirs alone.
SELF_SERVICE = {"/account/password"}
READS = {"GET", "HEAD", "OPTIONS"}


def is_read_only(user) -> bool:
    """Tolerates a row from a database that predates the column."""
    try:
        return bool(user["read_only"])
    except (IndexError, KeyError, TypeError):
        return False


def require_login(db, request: Request):
    """The signed-in user, and the one place a read-only account is stopped.

    Every route that changes anything calls this -- /login is the single
    exception, and it changes nothing but the session. So the refusal
    belongs here rather than in thirty-two separate handlers, where the
    thirty-third would be the one somebody forgot.
    """
    user = get_user(db, request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    if (is_read_only(user)
            and request.method not in READS
            and request.url.path not in SELF_SERVICE):
        raise HTTPException(
            status_code=403,
            detail="This account can look at Drishti but not change it, "
                   "and cannot fetch. Ask a super admin if you need more.")
    return user


def require_write(user):
    """For the few screens that are reached by GET but exist only to change
    something -- an add form, a delete confirmation. Every other GET is a
    read and a view-only account is welcome to it."""
    if is_read_only(user):
        raise HTTPException(
            status_code=403,
            detail="This account can look at Drishti but not change it.")


def require_role(user, *roles: str):
    if user["role"] not in roles:
        raise HTTPException(status_code=403, detail="Not permitted for your role")
