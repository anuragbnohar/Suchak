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


READS = {"GET", "HEAD", "OPTIONS"}

# What a guest account cannot reach. Everything else it does exactly as
# its role allows -- reviewing, setting aside, marking done, raising an
# alert. The four kept back are the ones that spend money or reshape the
# installation itself.
#
# Prefix-matched, so /people covers every /people/... route and /settings
# covers /settings/risk and its kin. Listed as paths rather than checked
# inside each handler, because the handler somebody adds next year will
# not remember to check.
GUEST_BLOCKED = (
    "/fetch",        # spends money on the paid sources
    "/settings",     # every knob, and the People roster lives on that page
    "/policy",       # the definitions the classifier judges by, and re-scoring
    "/people",       # adding, renaming, removing, resetting a password
    "/rd/users",     # the same, by another door
)

# Single buttons kept back on pages a guest is otherwise welcome to read.
# Both set the model running over everything already stored, so both cost
# money -- which is the same objection as Fetch, by a quieter door.
GUEST_BLOCKED_ACTIONS = (
    "/insights/generate",
    "/alerts/recheck",
)


def is_guest(user) -> bool:
    """Tolerates a row from a database that predates the column."""
    try:
        return bool(user["guest"])
    except (IndexError, KeyError, TypeError):
        return False


def guest_blocked(method: str, path: str) -> bool:
    """Whether a guest account may do this.

    Entities are the awkward one: the list is worth reading and everything
    under it changes something, so /entities is a read and /entities/...
    is not.
    """
    if path == "/account/password":
        # Kept, always. A password nobody can change is not theirs.
        return False
    if path in GUEST_BLOCKED_ACTIONS:
        return True
    for prefix in GUEST_BLOCKED:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    if path == "/entities":
        return method not in READS
    return path.startswith("/entities/")


def require_login(db, request: Request):
    """The signed-in user, and the one place a guest account is stopped.

    Every route calls this -- /login is the single exception, and it
    changes nothing but the session. So the refusal belongs here rather
    than in thirty-two separate handlers, where the thirty-third would be
    the one somebody forgot.
    """
    user = get_user(db, request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    if is_guest(user) and guest_blocked(request.method, request.url.path):
        raise HTTPException(
            status_code=403,
            detail="This account can review and read, but cannot fetch or "
                   "re-run the classifier, change settings or policy, manage "
                   "people, or edit entities. Ask a super admin if you need "
                   "more.")
    return user


def require_role(user, *roles: str):
    if user["role"] not in roles:
        raise HTTPException(status_code=403, detail="Not permitted for your role")
