"""Read-only GitHub REST client with a persistent ETag cache."""

from __future__ import annotations

import json
import os
import time
from urllib.parse import urlparse

import httpx

from helm.store import Store


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    base_url = "https://api.github.com"

    def __init__(self, store: Store, token: str | None = None, transport: httpx.BaseTransport | None = None):
        self.store = store
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN", "")
        self.client = httpx.Client(transport=transport, timeout=15)

    def get(self, path: str):
        if not self.token:
            raise GitHubError("GitHub token is not configured")
        url = f"{self.base_url}/{path.lstrip('/')}"
        if urlparse(url).hostname != "api.github.com":
            raise GitHubError("GitHub credentials may only be sent to api.github.com")
        with self.store.connect() as db:
            cached = db.execute("SELECT etag, body, updated FROM http_cache WHERE url=?", (url,)).fetchone()
        if cached and time.time() - cached["updated"] > 600:
            cached = None
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}"}
        if cached and cached["etag"]:
            headers["If-None-Match"] = cached["etag"]
        response = self.client.get(url, headers=headers)
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
        data = self.get(f"repos/{parts[0]}/{parts[1]}")
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
