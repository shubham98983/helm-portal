import base64
from datetime import datetime, timezone

import httpx

from helm.github import GitHubClient, GitHubError
from helm.store import Store
from helm.sync import MAX_CHECK_FETCHES_PER_SYNC, SyncService


def make_pull(number, updated="2025-01-01T00:00:00Z"):
    return {
        "number": number,
        "state": "open",
        "draft": False,
        "title": f"PR {number}",
        "created_at": updated,
        "updated_at": updated,
        "merged_at": None,
        "closed_at": None,
        "html_url": f"https://github.com/org/repo/pull/{number}",
        "user": {"login": "dev"},
        "labels": [],
        "head": {"ref": f"feature/{number}", "sha": f"sha-{number}"},
        "base": {"ref": "main"},
        "requested_reviewers": [],
    }


class FakeGitHub:
    def __init__(self, pulls):
        self.pull_rows = pulls
        self.requests_this_sync = 0
        self.calls = []

    def begin_sync(self, request_limit=19):
        self.requests_this_sync = 0

    def end_sync(self):
        pass

    def _call(self, name, value):
        self.calls.append(name)
        self.requests_this_sync += 1
        return value

    def pulls(self, repo):
        return self._call("pulls", self.pull_rows)

    def repo_metadata(self, repo):
        return self._call("repo", {"default_branch": "main"})

    def approvers_file(self, repo, branch):
        return self._call("approvers", "reviewer\n")

    def workflow_runs(self, repo):
        return self._call("workflow", [])

    def branch_commits(self, repo, branch):
        return self._call(
            "branch",
            [
                {
                    "sha": "base-sha",
                    "author": {"login": "dev"},
                    "commit": {"committer": {"date": "2025-01-01T00:00:00Z"}, "message": "base"},
                }
            ],
        )

    def reviews(self, repo, number):
        self._call(f"reviews:{number}", [])
        return []

    def pr_commits(self, repo, number):
        self._call(f"commits:{number}", [])
        return []

    def check_runs(self, repo, sha):
        return self._call(
            f"checks:{sha}",
            [
                {
                    "id": int(sha.split("-")[-1]),
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2025-01-01T00:00:00Z",
                }
            ],
        )


def service(tmp_path, gh, repos=None):
    repo_names = repos or ["org/repo"]
    project = {"repo": [{"full_name": repo} for repo in repo_names]}
    return SyncService(Store(str(tmp_path)), gh, project, {"member": []}, poll_seconds=120)


def test_sync_idempotence_review_and_commit_cache(tmp_path):
    github = FakeGitHub([make_pull(1)])
    sync = service(tmp_path, github)
    first = sync.sync_all()
    assert first["request_count"] == 8
    with sync.store.connect() as db:
        before = db.execute("SELECT COUNT(*) FROM events WHERE repo='org/repo'").fetchone()[0]
        meta = db.execute("SELECT request_count,last_error FROM sync_meta WHERE repo='org/repo'").fetchone()
    assert meta["request_count"] == 8 and meta["last_error"] is None
    github.calls.clear()
    second = sync.sync_all()
    assert "reviews:1" not in github.calls and "commits:1" not in github.calls
    with sync.store.connect() as db:
        after = db.execute("SELECT COUNT(*) FROM events WHERE repo='org/repo'").fetchone()[0]
    assert before == after
    assert second["request_count"] == 5


def test_reviews_and_commits_refresh_when_pull_updated_at_changes(tmp_path):
    github = FakeGitHub([make_pull(1)])
    sync = service(tmp_path, github)
    sync.sync_all()
    github.calls.clear()
    github.pull_rows = [make_pull(1, "2025-01-02T00:00:00Z")]
    sync.sync_all()
    assert "reviews:1" in github.calls
    assert "commits:1" in github.calls


def test_one_pull_failure_does_not_abort_other_pull(tmp_path):
    class IsolatedFailure(FakeGitHub):
        def reviews(self, repo, number):
            if number == 2:
                self.calls.append("reviews:2")
                self.requests_this_sync += 1
                raise GitHubError("review endpoint unavailable")
            return super().reviews(repo, number)

    github = IsolatedFailure([make_pull(1), make_pull(2), make_pull(3)])
    sync = service(tmp_path, github)
    result = sync.sync_all()
    with sync.store.connect() as db:
        rows = db.execute("SELECT number FROM prs WHERE repo='org/repo' ORDER BY number").fetchall()
    assert [row[0] for row in rows] == [1, 2, 3]
    assert "review endpoint unavailable" in result["errors"]["org/repo"]
    assert "commits:3" in github.calls


def test_checks_are_capped_across_repositories_and_events_trimmed(tmp_path):
    pulls = [make_pull(index) for index in range(1, 13)]
    github = FakeGitHub(pulls)
    sync = service(tmp_path, github, ["org/one", "org/two"])
    sync.sync_all()
    check_calls = [call for call in github.calls if call.startswith("checks:")]
    assert len(check_calls) == MAX_CHECK_FETCHES_PER_SYNC
    with sync.store.connect() as db:
        for index in range(550):
            db.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)",
                (
                    "org/repo",
                    f"extra-{index}",
                    "commit",
                    "dev",
                    "human",
                    datetime.now(timezone.utc).isoformat(),
                    "{}",
                ),
            )
    sync._trim_events("org/repo")
    with sync.store.connect() as db:
        count = db.execute("SELECT COUNT(*) FROM events WHERE repo='org/repo'").fetchone()[0]
    assert count <= 500


def test_manual_sync_cooldown_per_repository(tmp_path):
    github = FakeGitHub([])
    sync = service(tmp_path, github)
    sync.sync_repo_manual("org/repo")
    try:
        sync.sync_repo_manual("org/repo")
    except ValueError as exc:
        assert "less than 60 seconds" in str(exc)
    else:
        raise AssertionError("manual sync should be rate limited")


def test_real_client_reports_cold_and_warm_sync_request_counts(tmp_path):
    pulls = [make_pull(1)]
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        assert request.method == "GET" and request.url.host == "api.github.com"
        if path.endswith("/pulls"):
            return httpx.Response(200, json=pulls)
        if path.endswith("/contents/.github/APPROVERS"):
            return httpx.Response(
                200,
                json={"encoding": "base64", "content": base64.b64encode(b"reviewer\n").decode()},
            )
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": []})
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if path.endswith("/repos/org/repo"):
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(200, json=[])

    store = Store(str(tmp_path))
    client = GitHubClient(store, "test-token", httpx.MockTransport(handler))
    sync = service(tmp_path, client)
    cold = sync.sync_all()
    assert cold["request_count"] == 8
    calls.clear()
    warm = sync.sync_all()
    assert warm["request_count"] == 4
    assert all(request.method == "GET" for request in calls)
