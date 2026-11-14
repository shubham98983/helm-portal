"""Incremental GitHub synchronization with repository-level failure isolation."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from helm.github import GitHubClient, RequestBudgetExceeded
from helm.logic import detect_ai_pr, event_key, is_bot_author
from helm.store import Store

logger = logging.getLogger(__name__)
MAX_CHECK_FETCHES_PER_SYNC = 9


class SyncService:
    def __init__(
        self,
        store: Store,
        github: GitHubClient,
        project: dict[str, Any],
        team: dict[str, Any],
        poll_seconds: int = 120,
    ):
        self.store = store
        self.github = github
        self.project = project
        self.team = team
        self.poll_seconds = max(30, int(poll_seconds))
        self.request_limit = min(19, max(1, int(599 * self.poll_seconds / 3600)))
        self._stop = threading.Event()
        self._sync_local = threading.local()
        self._repo_lock_guard = threading.Lock()
        self._repo_locks: dict[str, threading.Lock] = {}
        self._thread: threading.Thread | None = None

    @property
    def repos(self) -> list[str]:
        return [item["full_name"] for item in self.project.get("repo", []) if item.get("full_name")]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="helm-github-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self.sync_all()

    def _repo_lock(self, repo: str) -> threading.Lock:
        with self._repo_lock_guard:
            return self._repo_locks.setdefault(repo, threading.Lock())

    def sync_all(self) -> dict[str, Any]:
        self.github.begin_sync(self.request_limit)
        self._sync_local.budget_exhausted = False
        errors = {}
        check_budget = MAX_CHECK_FETCHES_PER_SYNC
        results = {}
        for repo in self.repos:
            try:
                with self._repo_lock(repo):
                    result, check_budget = self._sync_repo(repo, check_budget)
                results[repo] = result
                if result.get("error"):
                    errors[repo] = result["error"]
            except Exception as exc:  # one repository must not abort other repositories
                logger.exception("GitHub sync failed for repository %s", repo)
                errors[repo] = str(exc)
                self._record_meta(repo, last_error=str(exc), last_sync=time.time())
        requests = self.github.requests_this_sync
        for repo in self.repos:
            with self.store.connect() as db:
                db.execute("UPDATE sync_meta SET request_count=? WHERE repo=?", (requests, repo))
        logger.info("GitHub sync completed: %s requests across %s repositories", requests, len(self.repos))
        self.github.end_sync()
        return {"repos": results, "request_count": requests, "errors": errors}

    def sync_repo_manual(self, repo: str, minimum_interval: int = 60) -> dict[str, Any]:
        if repo not in self.repos:
            raise ValueError(f"Repository is not configured: {repo}")
        with self._repo_lock(repo):
            with self.store.connect() as db:
                row = db.execute("SELECT last_sync FROM sync_meta WHERE repo=?", (repo,)).fetchone()
            if row and row["last_sync"] and time.time() - row["last_sync"] < minimum_interval:
                raise ValueError(f"Repository was synced less than {minimum_interval} seconds ago")
            self.github.begin_sync(min(19, max(1, int(599 * 60 / 3600))))
            self._sync_local.budget_exhausted = False
            result, _ = self._sync_repo(repo, MAX_CHECK_FETCHES_PER_SYNC)
            result["request_count"] = self.github.requests_this_sync
            with self.store.connect() as db:
                db.execute(
                    "UPDATE sync_meta SET request_count=? WHERE repo=?",
                    (result["request_count"], repo),
                )
            logger.info("Manual GitHub sync completed for %s: %s requests", repo, result["request_count"])
            self.github.end_sync()
            return result

    def _record_meta(self, repo: str, last_error: str | None, last_sync: float | None = None) -> None:
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO sync_meta(repo,last_sync,last_error) VALUES (?,?,?) "
                "ON CONFLICT(repo) DO UPDATE SET last_sync=excluded.last_sync, "
                "last_error=excluded.last_error",
                (repo, last_sync or time.time(), last_error),
            )

    def _safe(self, fn, errors: list[str], label: str, fallback):
        if getattr(self._sync_local, "budget_exhausted", False):
            return fallback
        try:
            return fn()
        except RequestBudgetExceeded as exc:
            self._sync_local.budget_exhausted = True
            errors.append(str(exc))
            logger.warning("GitHub sync request budget reached: %s", exc)
            return fallback
        except Exception as exc:
            logger.warning("GitHub sync operation failed (%s): %s", label, exc)
            errors.append(f"{label}: {exc}")
            return fallback

    def _sync_repo(self, repo: str, checks_left: int) -> tuple[dict[str, Any], int]:
        errors: list[str] = []
        try:
            pulls = self.github.pulls(repo)
        except Exception as exc:
            self._record_meta(repo, str(exc))
            return {"error": str(exc), "pulls": 0}, checks_left

        metadata = self._safe(lambda: self.github.repo_metadata(repo), errors, "repository metadata", {})
        default_branch = metadata.get("default_branch") or "main"
        workflow_runs = self._safe(lambda: self.github.workflow_runs(repo), errors, "workflow runs", [])
        branch_commits = self._safe(
            lambda: self.github.branch_commits(repo, default_branch), errors, "branch commits", []
        )
        previous: dict[int, dict[str, Any]] = {}
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM prs WHERE repo=?", (repo,)).fetchall()
            previous = {row["number"]: dict(row) for row in rows}

        pulls = [pull for pull in pulls if isinstance(pull, dict) and pull.get("number") is not None]
        pulls.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
        base_branches = {pull.get("base", {}).get("ref") or default_branch for pull in pulls} or {
            default_branch
        }
        now = time.time()
        for branch in base_branches:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT content,fetched_at FROM repo_approvers WHERE repo=? AND branch=?", (repo, branch)
                ).fetchone()
            cache_for = 60 if row and row["content"] == "__ERROR__" else 600
            if row and now - row["fetched_at"] < cache_for:
                continue
            else:
                content = self._safe(
                    lambda b=branch: self.github.approvers_file(repo, b),
                    errors,
                    f"{branch} approvers file",
                    "__unavailable__",
                )
                stored_content = "__ERROR__" if content == "__unavailable__" else content
                with self.store.connect() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO repo_approvers VALUES (?,?,?,?)",
                        (repo, branch, stored_content, now),
                    )
        changed: list[dict[str, Any]] = []
        for pull in pulls:
            number = int(pull["number"])
            updated_at = pull.get("updated_at") or ""
            old = previous.get(number)
            old_payload = json.loads(old["payload"]) if old else None
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO prs(repo,number,payload,updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(repo,number) DO UPDATE SET payload=excluded.payload, "
                    "updated_at=excluded.updated_at",
                    (repo, number, json.dumps(pull), updated_at),
                )
            if old is None or old["updated_at"] != updated_at:
                changed.append(pull)
            if old is None or old.get("reviews_updated_at") != updated_at:
                reviews = self._safe(
                    lambda n=number: self.github.reviews(repo, n), errors, f"PR {number} reviews", None
                )
                if reviews is not None:
                    self._save_reviews(repo, number, reviews)
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE prs SET reviews_updated_at=? WHERE repo=? AND number=?",
                            (updated_at, repo, number),
                        )
                    for review in reviews:
                        self._review_event(repo, number, review)
            stored_commits = None
            if old is None or old.get("commits_updated_at") != updated_at:
                commits = self._safe(
                    lambda n=number: self.github.pr_commits(repo, n), errors, f"PR {number} commits", None
                )
                if commits is not None:
                    stored_commits = commits[:30]
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE prs SET commits_payload=?, commits_updated_at=? "
                            "WHERE repo=? AND number=?",
                            (json.dumps(stored_commits), updated_at, repo, number),
                        )
            if stored_commits is None:
                with self.store.connect() as db:
                    stored_commits = json.loads(
                        db.execute(
                            "SELECT commits_payload FROM prs WHERE repo=? AND number=?", (repo, number)
                        ).fetchone()[0]
                        or "[]"
                    )
            if old is None or old["updated_at"] != updated_at:
                self._pr_events(repo, {**pull, "__commits": stored_commits}, old_payload)
            self._refresh_pr_event_ai(repo, pull, stored_commits)

        checks_to_fetch = [
            pull
            for pull in pulls
            if pull.get("state") == "open"
            or (pull.get("merged_at") and self._within_days(pull.get("merged_at"), 10))
        ]
        checks_to_fetch.sort(
            key=lambda pull: (
                previous.get(int(pull["number"]), {}).get("checks_updated_at") or "",
                pull.get("updated_at", ""),
            )
        )
        for pull in checks_to_fetch[:checks_left]:
            number = int(pull["number"])
            sha = pull.get("head", {}).get("sha")
            if not sha:
                continue
            checks = self._safe(
                lambda s=sha: self.github.check_runs(repo, s), errors, f"PR {number} checks", None
            )
            if checks is not None:
                self._save_checks(repo, number, sha, checks)
                checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE prs SET checks_updated_at=? WHERE repo=? AND number=?",
                        (checked_at, repo, number),
                    )
                for check in checks:
                    self._check_event(repo, number, check)
        checks_left = max(0, checks_left - min(len(checks_to_fetch), checks_left))

        for workflow in workflow_runs:
            self._workflow_event(repo, workflow)
        for commit in branch_commits:
            self._commit_event(repo, commit)
        self._trim_events(repo)
        now = time.time()
        last_error = "; ".join(errors) if errors else None
        self._record_meta(repo, last_error, now)
        with self.store.connect() as db:
            db.execute("UPDATE sync_meta SET default_branch=? WHERE repo=?", (default_branch, repo))
        return {"pulls": len(pulls), "changed": len(changed), "error": last_error}, checks_left

    @staticmethod
    def _within_days(value: str | None, days: int) -> bool:
        try:
            occurred = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return False
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - occurred <= timedelta(days=days)

    def _save_reviews(self, repo: str, number: int, reviews: list[dict]) -> None:
        with self.store.connect() as db:
            db.execute("DELETE FROM reviews WHERE repo=? AND pr_number=?", (repo, number))
            db.executemany(
                "INSERT OR REPLACE INTO reviews VALUES (?,?,?,?)",
                [
                    (repo, number, str(review.get("id", i)), json.dumps(review))
                    for i, review in enumerate(reviews)
                ],
            )

    def _save_checks(self, repo: str, number: int, sha: str, checks: list[dict]) -> None:
        with self.store.connect() as db:
            db.execute("DELETE FROM checks WHERE repo=? AND pr_number=?", (repo, number))
            db.executemany(
                "INSERT OR REPLACE INTO checks VALUES (?,?,?,?,?)",
                [
                    (repo, number, str(check.get("id", i)), sha, json.dumps(check))
                    for i, check in enumerate(checks)
                ],
            )

    def _add_event(
        self,
        repo: str,
        kind: str,
        subject: str,
        created_at: str | None,
        actor: str,
        actor_type: str,
        payload: dict,
    ) -> None:
        if not created_at:
            return
        key = event_key(repo, kind, subject, created_at, actor)
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)",
                (repo, key, kind, actor, actor_type, created_at, json.dumps(payload)),
            )

    def _pr_events(self, repo: str, pull: dict, old: dict | None) -> None:
        user = pull.get("user", {}).get("login", "")
        reasons = detect_ai_pr(pull, pull.get("__commits", []))
        bot = is_bot_author(pull)
        merged_at = pull.get("merged_at")
        if old is None:
            kind, timestamp = "opened", pull.get("created_at")
            self._add_pull_event(repo, pull, kind, timestamp, user, reasons, bot)
            if merged_at:
                kind, timestamp = "merged", merged_at
            elif pull.get("state") == "closed":
                kind, timestamp = "closed", pull.get("closed_at")
            else:
                return
        elif merged_at and not old.get("merged_at"):
            kind, timestamp = "merged", merged_at
        elif pull.get("state") == "closed" and old.get("state") != "closed":
            kind, timestamp = "closed", pull.get("closed_at")
        else:
            kind, timestamp = "updated", pull.get("updated_at")
        self._add_pull_event(repo, pull, kind, timestamp, user, reasons, bot)

    def _add_pull_event(
        self,
        repo: str,
        pull: dict,
        kind: str,
        timestamp: str | None,
        user: str,
        reasons: list[str],
        bot: bool,
    ) -> None:
        self._add_event(
            repo,
            "pull_request",
            f"{pull.get('number')}:{kind}",
            timestamp,
            user,
            "ai" if reasons or bot else "human",
            {
                "number": pull.get("number"),
                "title": pull.get("title"),
                "url": pull.get("html_url"),
                "action": kind,
                "is_ai": bool(reasons),
                "ai_reasons": reasons,
                "is_bot": bot,
                "branch": pull.get("head", {}).get("ref", ""),
            },
        )

    def _refresh_pr_event_ai(self, repo: str, pull: dict, commits: list[dict]) -> None:
        reasons = detect_ai_pr(pull, commits)
        bot = is_bot_author(pull)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT event_key,payload FROM events WHERE repo=? AND kind='pull_request'", (repo,)
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                if payload.get("number") != pull.get("number"):
                    continue
                payload.update({"is_ai": bool(reasons), "ai_reasons": reasons, "is_bot": bot})
                db.execute(
                    "UPDATE events SET actor_type=?,payload=? WHERE repo=? AND event_key=?",
                    ("ai" if reasons or bot else "human", json.dumps(payload), repo, row["event_key"]),
                )

    def _review_event(self, repo: str, number: int, review: dict) -> None:
        actor = review.get("user", {}).get("login", "")
        self._add_event(
            repo,
            "review",
            str(review.get("id", f"{number}:{review.get('submitted_at')}")),
            review.get("submitted_at"),
            actor,
            "ai" if str(actor).casefold().endswith("[bot]") else "human",
            {"number": number, "state": review.get("state"), "body": review.get("body", "")},
        )

    def _check_event(self, repo: str, number: int, check: dict) -> None:
        actor = check.get("app", {}).get("slug", "GitHub Actions")
        actor_type = (
            "ai" if str(actor).casefold().endswith("[bot]") or "copilot" in str(actor).casefold() else "human"
        )
        self._add_event(
            repo,
            "check",
            str(check.get("id", number)),
            check.get("updated_at") or check.get("completed_at") or check.get("started_at"),
            actor,
            actor_type,
            {
                "number": number,
                "name": check.get("name"),
                "status": check.get("status"),
                "conclusion": check.get("conclusion"),
            },
        )

    def _workflow_event(self, repo: str, run: dict) -> None:
        actor = run.get("actor", {}).get("login", "")
        self._add_event(
            repo,
            "workflow",
            str(run.get("id", "")),
            run.get("updated_at") or run.get("created_at"),
            actor,
            "ai" if str(actor).casefold().endswith("[bot]") else "human",
            {
                "name": run.get("name"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "url": run.get("html_url"),
                "branch": run.get("head_branch"),
            },
        )

    def _commit_event(self, repo: str, commit: dict) -> None:
        actor = commit.get("author", {}).get("login", "") or commit.get("committer", {}).get("login", "")
        data = commit.get("commit", {})
        self._add_event(
            repo,
            "commit",
            commit.get("sha", ""),
            data.get("committer", {}).get("date"),
            actor,
            "ai" if str(actor).casefold().endswith("[bot]") else "human",
            {"message": data.get("message", ""), "url": commit.get("html_url")},
        )

    def _trim_events(self, repo: str) -> None:
        with self.store.connect() as db:
            db.execute(
                """DELETE FROM events WHERE repo=? AND event_key IN (
                       SELECT event_key FROM events WHERE repo=? ORDER BY created_at DESC LIMIT -1 OFFSET 500
                   )""",
                (repo, repo),
            )
