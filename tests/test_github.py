import time

import httpx
import pytest

from helm.github import GitHubClient, GitHubError, RequestBudgetExceeded
from helm.store import Store


def test_get_only_and_etag_reuse(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.github.com"
        assert request.method == "GET"
        if len(calls) == 1:
            return httpx.Response(200, json={"full_name": "a/b"}, headers={"ETag": '"abc"'})
        assert request.headers["If-None-Match"] == '"abc"'
        return httpx.Response(304)

    client = GitHubClient(Store(str(tmp_path)), "private-token", httpx.MockTransport(handler))
    assert client.get("repos/a/b")["full_name"] == "a/b"
    assert client.get("repos/a/b")["full_name"] == "a/b"
    assert len(calls) == 2


def test_missing_token_and_status_errors(tmp_path):
    with pytest.raises(GitHubError, match="not configured"):
        GitHubClient(Store(str(tmp_path)), "").repo_metadata("a/b")
    client = GitHubClient(Store(str(tmp_path)), "token", httpx.MockTransport(lambda req: httpx.Response(401)))
    with pytest.raises(GitHubError, match="401"):
        client.repo_metadata("a/b")


def test_extended_github_reads_and_approvers_404(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/contents/.github/APPROVERS"):
            return httpx.Response(200, json={"encoding": "base64", "content": "QGxlYWQK"})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [{"id": 5}]})
        if request.url.path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": [{"id": 6}]})
        return httpx.Response(200, json=[])

    client = GitHubClient(Store(str(tmp_path)), "secret-token", httpx.MockTransport(handler))
    assert client.pulls("org/repo") == []
    assert client.reviews("org/repo", 4) == []
    assert client.pr_commits("org/repo", 4) == []
    assert client.check_runs("org/repo", "sha") == [{"id": 5}]
    assert client.workflow_runs("org/repo") == [{"id": 6}]
    assert client.branch_commits("org/repo", "main") == []
    assert client.approvers_file("org/repo", "main") == "@lead\n"
    assert len(requests) == 7
    assert all(request.headers["Authorization"] == "Bearer secret-token" for request in requests)

    missing = GitHubClient(
        Store(str(tmp_path / "missing")), "token", httpx.MockTransport(lambda _: httpx.Response(404))
    )
    assert missing.approvers_file("org/repo", "main") is None


def test_request_budget_stops_before_six_hundredth_request(tmp_path):
    store = Store(str(tmp_path))
    with store.connect() as db:
        db.executemany("INSERT INTO github_request_log VALUES (?)", [(time.time(),)] * 599)
    called = []
    client = GitHubClient(store, "token", httpx.MockTransport(lambda request: called.append(request)))
    with pytest.raises(RequestBudgetExceeded):
        client.get("repos/org/repo")
    assert called == []


def test_request_budget_is_capped_per_sync(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    client = GitHubClient(Store(str(tmp_path)), "token", httpx.MockTransport(handler))
    client.begin_sync(1)
    client.get("repos/org/one")
    with pytest.raises(RequestBudgetExceeded, match="Per-sync"):
        client.get("repos/org/two")
    assert len(calls) == 1


def test_issues_excludes_pull_requests_and_caches_response(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path.endswith("/issues")
        return httpx.Response(
            200,
            headers={"ETag": '"issues"'},
            json=[
                {"number": 1, "title": "Issue"},
                {"number": 2, "title": "Pull", "pull_request": {"url": "x"}},
            ],
        )

    client = GitHubClient(Store(str(tmp_path)), "token", httpx.MockTransport(handler))
    assert [issue["number"] for issue in client.issues("org/repo")] == [1]
    assert [issue["number"] for issue in client.issues("org/repo")] == [1]
    assert len(calls) == 1
