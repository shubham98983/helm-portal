"""Read-only GitHub REST client with a persistent ETag cache."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from urllib.parse import urlparse

import httpx

from helm.store import Store


class GitHubError(RuntimeError):
    pass


class RequestBudgetExceeded(GitHubError):
    pass


class GitHubClient:
    base_url = "https://api.github.com"

    def __init__(self, store: Store, token: str | None = None, transport: httpx.BaseTransport | None = None):
        self.store = store
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN", "")
        self.client = httpx.Client(transport=transport, timeout=15)
        self._sync_local = threading.local()
        self.begin_sync()

    @property
    def requests_this_sync(self) -> int:
        return getattr(self._sync_local, "requests", 0)

    @property
    def sync_request_limit(self) -> int:
        return getattr(self._sync_local, "limit", 19)

    def begin_sync(self, request_limit: int = 19) -> None:
        self._sync_local.requests = 0
        self._sync_local.limit = max(1, request_limit)
        self._sync_local.active = True

    def end_sync(self) -> None:
        self._sync_local.active = False

    def _record_request(self) -> None:
        if getattr(self._sync_local, "active", False) and self.requests_this_sync >= self.sync_request_limit:
            raise RequestBudgetExceeded(
                f"Per-sync GitHub request budget reached ({self.sync_request_limit} requests)"
            )
        now = time.time()
        with self.store.connect() as db:
            db.execute("DELETE FROM github_request_log WHERE requested_at < ?", (now - 3600,))
            count = db.execute("SELECT COUNT(*) FROM github_request_log").fetchone()[0]
            if count >= 599:
                raise RequestBudgetExceeded("GitHub request budget reached (599 requests in the past hour)")
            db.execute("INSERT INTO github_request_log VALUES (?)", (now,))
        self._sync_local.requests += 1

    def get(self, path: str, fresh_for: int = 0):
        if not self.token:
            raise GitHubError("GitHub token is not configured")
        url = f"{self.base_url}/{path.lstrip('/')}"
        if urlparse(url).hostname != "api.github.com":
            raise GitHubError("GitHub credentials may only be sent to api.github.com")
        with self.store.connect() as db:
            cached = db.execute("SELECT etag, body, updated FROM http_cache WHERE url=?", (url,)).fetchone()
        if cached and fresh_for and time.time() - cached["updated"] < fresh_for:
            return json.loads(cached["body"])
        if cached and time.time() - cached["updated"] > 600:
            cached = None
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}"}
        if cached and cached["etag"]:
            headers["If-None-Match"] = cached["etag"]
        self._record_request()
        try:
            response = self.client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub request failed ({type(exc).__name__}).") from exc
        if response.status_code == 304 and cached:
            with self.store.connect() as db:
                db.execute("UPDATE http_cache SET updated=? WHERE url=?", (time.time(), url))
            return json.loads(cached["body"])
        if response.status_code == 401:
            raise GitHubError("GitHub rejected the token (401). Check GITHUB_TOKEN.")
        if response.status_code == 403:
            reset = response.headers.get("X-RateLimit-Reset")
            if response.headers.get("X-RateLimit-Remaining") == "0" and reset:
                reset_time = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(reset)))
                raise GitHubError(f"GitHub rate limit reached; reset at {reset_time}")
            raise GitHubError("GitHub denied access (403). Check token permissions.")
        if response.status_code == 404:
            raise GitHubError("GitHub repository not found or token lacks access (404).")
        if response.is_error:
            raise GitHubError(f"GitHub API returned HTTP {response.status_code}.")
        body = response.json()
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?, ?)",
                (url, response.headers.get("ETag"), json.dumps(body), time.time()),
            )
        return body

    def repo_metadata(self, full_name: str):
        parts = full_name.split("/", 1)
        if len(parts) != 2 or not all(parts):
            raise GitHubError(f"Invalid GitHub repository name: {full_name}")
        data = self.get(f"repos/{parts[0]}/{parts[1]}", fresh_for=600)
        return {
            key: data.get(key)
            for key in (
                "full_name",
                "description",
                "html_url",
                "default_branch",
                "private",
                "language",
                "stargazers_count",
            )
        }

    @staticmethod
    def _repo_path(repo: str) -> str:
        parts = repo.split("/", 1)
        if len(parts) != 2 or not all(parts):
            raise GitHubError(f"Invalid GitHub repository name: {repo}")
        return f"repos/{parts[0]}/{parts[1]}"

    def pulls(self, repo: str) -> list[dict]:
        return self.get(f"{self._repo_path(repo)}/pulls?state=all&sort=updated&direction=desc&per_page=100")

    def reviews(self, repo: str, number: int) -> list[dict]:
        return self.get(f"{self._repo_path(repo)}/pulls/{int(number)}/reviews?per_page=100")

    def pr_commits(self, repo: str, number: int) -> list[dict]:
        return self.get(f"{self._repo_path(repo)}/pulls/{int(number)}/commits?per_page=30")[:30]

    def check_runs(self, repo: str, sha: str) -> list[dict]:
        data = self.get(f"{self._repo_path(repo)}/commits/{sha}/check-runs?per_page=100")
        return data.get("check_runs", [])

    def workflow_runs(self, repo: str) -> list[dict]:
        data = self.get(f"{self._repo_path(repo)}/actions/runs?per_page=100")
        return data.get("workflow_runs", [])

    def issues(self, repo: str) -> list[dict]:
        """List recent open and closed issues, filtering pull requests from GitHub's shared endpoint."""
        data = self.get(
            f"{self._repo_path(repo)}/issues?state=all&sort=updated&direction=desc&per_page=100",
            fresh_for=600,
        )
        return [item for item in data if "pull_request" not in item]

    def branch_commits(self, repo: str, branch: str) -> list[dict]:
        from urllib.parse import quote

        data = self.get(f"{self._repo_path(repo)}/commits?sha={quote(branch, safe='')}&per_page=30")
        return data[:30]

    def approvers_file(self, repo: str, base_branch: str) -> str | None:
        from urllib.parse import quote

        path = f"{self._repo_path(repo)}/contents/.github/APPROVERS?ref={quote(base_branch, safe='')}"
        try:
            data = self.get(path, fresh_for=600)
        except GitHubError as exc:
            if "404" in str(exc):
                return None
            raise
        if data.get("encoding") == "base64":
            return base64.b64decode(data.get("content", "")).decode("utf-8")
        return data.get("content", "")
