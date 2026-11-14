import httpx
import pytest

from helm.github import GitHubClient, GitHubError
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
    assert client.repo_metadata("a/b")["full_name"] == "a/b"
    assert client.repo_metadata("a/b")["full_name"] == "a/b"
    assert len(calls) == 2


def test_missing_token_and_status_errors(tmp_path):
    with pytest.raises(GitHubError, match="not configured"):
        GitHubClient(Store(str(tmp_path)), "").repo_metadata("a/b")
    client = GitHubClient(Store(str(tmp_path)), "token", httpx.MockTransport(lambda req: httpx.Response(401)))
    with pytest.raises(GitHubError, match="401"):
        client.repo_metadata("a/b")
