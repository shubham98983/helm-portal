"""Password hashing, session management, and role helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

from fastapi import Request

from helm.store import Store

SESSION_SECONDS = 8 * 60 * 60
FAILURE_WINDOW = 10 * 60


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = encoded.split("$", 2)
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1)
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def current_user(request: Request) -> dict[str, Any] | None:
    sid = request.cookies.get("helm_session")
    username = request.app.state.store.session(sid) if sid else None
    return request.app.state.users.get(username) if username else None


def role_at_least(user: dict[str, Any] | None, *roles: str) -> bool:
    return bool(user and user.get("role") in roles)


def login(store: Store, users: dict[str, dict[str, Any]], username: str, password: str, ip: str):
    username = username.strip().lower()
    now = time.time()
    keys = (f"user:{username}", f"ip:{ip}")
    with store.connect() as db:
        locked = any(
            (lambda row: row and row["since"] + FAILURE_WINDOW > now and row["count"] >= 5)(
                db.execute("SELECT count, since FROM failures WHERE key=?", (key,)).fetchone()
            )
            for key in keys
        )
    user = users.get(username)
    if locked or not user or not verify_password(password, user.get("password_hash", "")):
        with store.connect() as db:
            for key in keys:
                row = db.execute("SELECT count, since FROM failures WHERE key=?", (key,)).fetchone()
                count = row["count"] + 1 if row and row["since"] + FAILURE_WINDOW > now else 1
                since = row["since"] if row and row["since"] + FAILURE_WINDOW > now else now
                db.execute("INSERT OR REPLACE INTO failures VALUES (?, ?, ?)", (key, count, since))
        return None
    with store.connect() as db:
        db.executemany("DELETE FROM failures WHERE key=?", ((key,) for key in keys))
    sid = secrets.token_urlsafe(32)
    with store.connect() as db:
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (sid, username, now + SESSION_SECONDS))
    return sid
