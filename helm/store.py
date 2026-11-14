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
                CREATE TABLE IF NOT EXISTS prs (
                    repo TEXT NOT NULL, number INTEGER NOT NULL, payload TEXT NOT NULL,
                    commits_payload TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL,
                    reviews_updated_at TEXT, commits_updated_at TEXT,
                    checks_updated_at TEXT,
                    PRIMARY KEY (repo, number)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    repo TEXT NOT NULL, pr_number INTEGER NOT NULL, review_id TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY (repo, pr_number, review_id)
                );
                CREATE TABLE IF NOT EXISTS checks (
                    repo TEXT NOT NULL, pr_number INTEGER NOT NULL, check_id TEXT NOT NULL,
                    sha TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (repo, pr_number, check_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    repo TEXT NOT NULL, event_key TEXT NOT NULL, kind TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT '', actor_type TEXT NOT NULL DEFAULT 'human',
                    created_at TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (repo, event_key)
                );
                CREATE INDEX IF NOT EXISTS events_repo_created ON events(repo, created_at DESC);
                CREATE TABLE IF NOT EXISTS sync_meta (
                    repo TEXT PRIMARY KEY, last_sync REAL, last_error TEXT,
                    request_count INTEGER NOT NULL DEFAULT 0, approvers_text TEXT,
                    default_branch TEXT
                );
                CREATE TABLE IF NOT EXISTS repo_approvers (
                    repo TEXT NOT NULL, branch TEXT NOT NULL, content TEXT,
                    fetched_at REAL NOT NULL, PRIMARY KEY(repo, branch)
                );
                CREATE TABLE IF NOT EXISTS github_request_log (requested_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS github_request_time ON github_request_log(requested_at);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(sync_meta)")}
            if "approvers_text" not in columns:
                db.execute("ALTER TABLE sync_meta ADD COLUMN approvers_text TEXT")
            if "default_branch" not in columns:
                db.execute("ALTER TABLE sync_meta ADD COLUMN default_branch TEXT")
            pr_columns = {row[1] for row in db.execute("PRAGMA table_info(prs)")}
            if "checks_updated_at" not in pr_columns:
                db.execute("ALTER TABLE prs ADD COLUMN checks_updated_at TEXT")

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
