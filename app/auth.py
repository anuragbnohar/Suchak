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
    uid = request.session.get("uid")
    if not uid:
        return None
    return one(db, "SELECT * FROM users WHERE id = ?", (uid,))


def require_login(db, request: Request):
    user = get_user(db, request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_role(user, *roles: str):
    if user["role"] not in roles:
        raise HTTPException(status_code=403, detail="Not permitted for your role")
