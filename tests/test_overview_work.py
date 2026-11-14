import asyncio

import httpx

from helm.auth import hash_password
from helm.github import GitHubClient
from helm.main import create_app
from helm.store import Store


def config_dir(path):
    path.mkdir()
    (path / "team.toml").write_text(
        f"""[[member]]\ngithub="lead"\nname="Lead"\nrole="lead"\ntitle="Lead"\nreports_to=""\n"""
        f'''responsibilities=[]\npassword_hash="{hash_password("secret")}"\n'''
    )
    (path / "project.toml").write_text(
        """name="Demo"\ntagline="Tag"\npurpose="Purpose"\nphase="Build"\ntech_stack=[]\n"""
        """[[repo]]\nfull_name="org/repo"\nrole="Primary"\nurl="https://github.com/org/repo"\n"""
    )


def github_transport(request):
    assert request.method == "GET"
    if request.url.path == "/repos/org/repo":
        return httpx.Response(200, json={"default_branch": "trunk"})
    if request.url.path.endswith("/actions/runs"):
        return httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "id": 2,
                        "name": "CI",
                        "head_branch": "trunk",
                        "conclusion": "success",
                        "updated_at": "2026-11-14T12:00:00Z",
                        "html_url": "https://github.com/org/repo/actions/runs/2",
                    }
                ]
            },
        )
    if request.url.path.endswith("/issues"):
        return httpx.Response(
            200,
            json=[
                {
                    "number": 3,
                    "title": "Open issue",
                    "state": "open",
                    "labels": [],
                    "assignees": [],
                    "html_url": "https://github.com/org/repo/issues/3",
                    "created_at": "2026-11-14T12:00:00Z",
                },
                {"number": 4, "title": "PR result", "state": "open", "pull_request": {"url": "x"}},
            ],
        )
    return httpx.Response(404)


def test_overview_copilot_up_down_and_missing_scores(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config_dir(config)
    store = Store(str(tmp_path / "data"))
    app = create_app(config, store, GitHubClient(store, "token", httpx.MockTransport(github_transport)))
    monkeypatch.setenv("COPILOT_URL", "http://copilot.test")
    responses = [
        httpx.Response(
            200,
            request=httpx.Request("GET", "http://copilot.test"),
            json={
                "score": 87,
                "coverage": 93,
                "findings": [{"severity": "high"}, {"severity": "low"}],
                "score_trend": [80, 87],
            },
        ),
        httpx.ConnectError("offline"),
        httpx.Response(
            200, request=httpx.Request("GET", "http://copilot.test"), json={"score": None, "coverage": None}
        ),
    ]

    def copilot_get(*_args, **_kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("helm.main.httpx.get", copilot_get)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://helm.test"
        ) as client:
            assert (
                await client.post("/api/login", json={"username": "lead", "password": "secret"})
            ).status_code == 200
            up = (await client.get("/api/overview")).json()
            assert up["main_ci"]["conclusion"] == "success"
            assert up["open_issues"] == 1
            assert up["copilot"]["score"] == 87
            assert up["copilot"]["findings"] == {"high": 1, "low": 1}
            down = (await client.get("/api/overview")).json()
            assert down["copilot"]["label"] == "Copilot unavailable"
            missing = (await client.get("/api/overview")).json()
            assert missing["copilot"]["score"] == "No data"
            assert missing["copilot"]["coverage"] == "No data"

    asyncio.run(exercise())


def test_work_issues_shape_filters_and_missing_token_error(tmp_path):
    config = tmp_path / "config"
    config_dir(config)
    store = Store(str(tmp_path / "data"))
    app = create_app(config, store, GitHubClient(store, "token", httpx.MockTransport(github_transport)))

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://helm.test"
        ) as client:
            await client.post("/api/login", json={"username": "lead", "password": "secret"})
            data = (await client.get("/api/work/issues?repo=org%2Frepo")).json()
            assert [issue["number"] for issue in data["issues"]] == [3]
            assert data["issues"][0]["column"] == "backlog"
            assert (await client.get("/api/work/issues?repo=missing/repo")).status_code == 404

    asyncio.run(exercise())


def test_overview_without_copilot_does_not_make_optional_call(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config_dir(config)
    store = Store(str(tmp_path / "data"))
    app = create_app(config, store, GitHubClient(store, "", httpx.MockTransport(github_transport)))
    monkeypatch.delenv("COPILOT_URL", raising=False)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://helm.test"
        ) as client:
            await client.post("/api/login", json={"username": "lead", "password": "secret"})
            data = (await client.get("/api/overview")).json()
            assert data["copilot"] is None
            assert data["main_ci"] is None
            assert data["open_issues"] == 0
            assert data["errors"]["org/repo"] == "GitHub token is not configured"

    asyncio.run(exercise())


def test_activity_includes_failed_copilot_analysis(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config_dir(config)
    store = Store(str(tmp_path / "data"))
    app = create_app(config, store, GitHubClient(store, "", httpx.MockTransport(github_transport)))
    monkeypatch.setenv("COPILOT_URL", "http://copilot.test")
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "http://copilot.test"),
        json={
            "runs": [
                {
                    "id": 12,
                    "status": "failed",
                    "name": "CI run",
                    "created_at": "2026-11-13T10:00:00Z",
                    "summary": "Database check failed",
                }
            ]
        },
    )
    monkeypatch.setattr("helm.main.httpx.get", lambda *args, **kwargs: response)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://helm.test"
        ) as client:
            await client.post("/api/login", json={"username": "lead", "password": "secret"})
            data = (await client.get("/api/activity?actor=ai")).json()
            assert data["events"][0]["actor"] == "AI DevOps Copilot"
            assert data["events"][0]["message"] == "Database check failed"
            assert data["events"][0]["is_ai"] is True

    asyncio.run(exercise())
