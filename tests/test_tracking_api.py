import asyncio
import csv
import io
import json
from datetime import datetime, timedelta, timezone

import httpx

from helm.auth import hash_password
from helm.github import GitHubClient
from helm.main import create_app
from helm.store import Store


def make_config(folder):
    folder.mkdir()
    users = [
        ("lead", "Lead", "lead", ""),
        ("reviewer", "Reviewer", "reviewer", "lead"),
        ("viewer", "Viewer", "viewer", "lead"),
    ]
    lines = []
    for github, name, role, parent in users:
        lines.extend(
            [
                "[[member]]",
                f'github="{github}"',
                f'name="{name}"',
                f'role="{role}"',
                f'title="{name}"',
                f'reports_to="{parent}"',
                "responsibilities=[]",
                f'password_hash="{hash_password("secret")}"',
                "",
            ]
        )
    (folder / "team.toml").write_text("\n".join(lines))
    (folder / "project.toml").write_text(
        """name="Helm"\ntagline="Tag"\npurpose="P"\nphase="Build"\ntech_stack=[]
[[repo]]\nfull_name="org/repo"\nrole="Primary"\nurl="https://github.com/org/repo"\n"""
    )


def seed_tracking(store):
    merged_at = datetime.now(timezone.utc).isoformat()
    approved_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    payloads = [
        {
            "number": 10,
            "state": "open",
            "draft": False,
            "title": "Normal PR",
            "updated_at": "2025-01-01T00:00:00Z",
            "created_at": "2025-01-01T00:00:00Z",
            "merged_at": None,
            "html_url": "https://github.com/org/repo/pull/10",
            "user": {"login": "dev"},
            "labels": [],
            "head": {"ref": "feature/fix", "sha": "sha-open"},
            "base": {"ref": "main"},
            "requested_reviewers": [{"login": "reviewer"}],
        },
        {
            "number": 11,
            "state": "closed",
            "draft": False,
            "title": "=2+2",
            "updated_at": merged_at,
            "created_at": merged_at,
            "merged_at": merged_at,
            "closed_at": merged_at,
            "merge_commit_sha": "merge-sha",
            "html_url": "https://github.com/org/repo/pull/11",
            "user": {"login": "codex[bot]"},
            "labels": [{"name": "AI-GENERATED"}],
            "head": {"ref": "ai/fix", "sha": "merge-sha"},
            "base": {"ref": "main"},
            "requested_reviewers": [],
        },
    ]
    with store.connect() as db:
        for payload in payloads:
            db.execute(
                "INSERT INTO prs(repo,number,payload,updated_at,commits_payload) VALUES (?,?,?,?,?)",
                ("org/repo", payload["number"], json.dumps(payload), payload["updated_at"], "[]"),
            )
        db.execute("INSERT INTO repo_approvers VALUES (?,?,?,?)", ("org/repo", "main", "reviewer\n", 1))
        db.execute(
            "INSERT INTO reviews VALUES (?,?,?,?)",
            (
                "org/repo",
                11,
                "1",
                json.dumps(
                    {
                        "id": 1,
                        "state": "APPROVED",
                        "commit_id": "merge-sha",
                        "submitted_at": approved_at,
                        "user": {"login": "reviewer"},
                    }
                ),
            ),
        )
        for number, sha, conclusion in [(10, "sha-open", "failure"), (11, "merge-sha", "success")]:
            db.execute(
                "INSERT INTO checks VALUES (?,?,?,?,?)",
                (
                    "org/repo",
                    number,
                    f"{number}-check",
                    sha,
                    json.dumps(
                        {
                            "id": number,
                            "name": "CI",
                            "head_sha": sha,
                            "status": "completed",
                            "conclusion": conclusion,
                        }
                    ),
                ),
            )
        db.executemany(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
            [
                ("org/repo", "ai-review", "review", "codex[bot]", "ai", "2025-01-05T00:00:00Z", "{}"),
                ("org/repo", "human-commit", "commit", "dev", "human", "2025-01-04T00:00:00Z", "{}"),
            ],
        )


def test_board_audit_csv_and_role_restrictions(tmp_path):
    config = tmp_path / "config"
    make_config(config)
    store = Store(str(tmp_path / "data"))
    seed_tracking(store)
    app = create_app(config, store, GitHubClient(store, token=""))

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://helm.test"
        ) as client:
            assert (await client.get("/api/audit")).status_code == 401
            assert (
                await client.post("/api/login", json={"username": "lead", "password": "secret"})
            ).status_code == 200
            board = (await client.get("/api/prs/board?repo=org%2Frepo")).json()["columns"]
            card = board["ci"][0]
            assert card["number"] == 10 and card["checks"]["overall"] == "failing"
            assert card["approvals"]["missing"] == ["reviewer"]
            filtered = (await client.get("/api/prs/board?repo=org%2Frepo&ai=1")).json()["columns"]
            assert [item["number"] for item in filtered["done"]] == [11]
            activity = (await client.get("/api/activity?repo=org%2Frepo&actor=ai&kind=review")).json()
            assert [item["key"] for item in activity["events"]] == ["ai-review"]
            audit = (await client.get("/api/audit?repo=org%2Frepo")).json()["entries"]
            assert audit[0]["verdict"] == "compliant"
            csv_response = await client.get("/api/audit.csv?repo=org%2Frepo")
            rows = list(csv.reader(io.StringIO(csv_response.text)))
            assert rows[0][0] == "repo" and rows[1][2] == "'=2+2"
            assert (await client.get("/api/members/reviewer/workload")).json()["reviews_done"] == 1
            assert (await client.post("/api/sync", json={"repo": "org/repo"})).status_code != 403
            await client.post("/api/logout", json={})
            await client.post("/api/login", json={"username": "viewer", "password": "secret"})
            assert (await client.get("/api/audit")).status_code == 403
            assert (await client.get("/api/audit.csv")).status_code == 403
            assert (await client.post("/api/sync", json={"repo": "org/repo"})).status_code == 403
            team = (await client.get("/api/team")).json()
            assert team["tree"][0]["workload"]["open_prs"] == 1

    asyncio.run(exercise())
