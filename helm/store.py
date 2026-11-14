"""Small SQLite store for sessions, rate limits, and HTTP cache entries."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, data_dir: str | None = None):
        self.data_dir = Path(data_dir or os.getenv("DATA_DIR", "./data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "helm.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, user TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS failures (
                    key TEXT PRIMARY KEY, count INTEGER NOT NULL, since REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS http_cache (
                    url TEXT PRIMARY KEY, etag TEXT, body TEXT NOT NULL, updated REAL NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def session(self, sid: str):
        with self.connect() as db:
            row = db.execute("SELECT user, expires FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row or row["expires"] <= time.time():
                db.execute("DELETE FROM sessions WHERE id=?", (sid,))
                return None
            return row["user"]
