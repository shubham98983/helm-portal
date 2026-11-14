import time

from helm.auth import hash_password, login, role_at_least, verify_password
from helm.store import Store


def test_hash_verify_and_roles():
    hashed = hash_password("correct horse")
    user = {"role": "lead"}
    assert verify_password("correct horse", hashed)
    assert not verify_password("wrong", hashed)
    assert role_at_least(user, "lead", "reviewer")
    assert not role_at_least(user, "viewer")


def test_rate_limit_and_session_expiry(tmp_path):
    store = Store(str(tmp_path))
    users = {"lead": {"github": "lead", "password_hash": hash_password("secret")}}
    for _ in range(5):
        assert login(store, users, "lead", "bad", "127.0.0.1") is None
    assert login(store, users, "lead", "secret", "127.0.0.1") is None
    with store.connect() as db:
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)", ("old", "lead", time.time() - 1))
    assert store.session("old") is None
