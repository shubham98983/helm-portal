import asyncio

import httpx

from helm.auth import hash_password
from helm.main import create_app
from helm.store import Store


def test_team_project_api_and_auth(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "team.toml").write_text(
        f'''[[member]]\ngithub="lead"\nname="<script>alert(1)</script>"\nrole="lead"\ntitle="Lead"\nreports_to=""\nresponsibilities=[]\npassword_hash="{hash_password("secret")}"\n'''
    )
    (config / "project.toml").write_text(
        """name="Project"\ntagline="Tag"\npurpose="Purpose"\nphase="Build"\ntech_stack=[]\n[[milestone]]\nname="Done"\ndue=""\nstatus="done"\ndeliverables=[]\n"""
    )
    app = create_app(config, Store(str(tmp_path / "data")))

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/team")).status_code == 401
            assert (
                await client.post("/api/login", json={"username": "lead", "password": "secret"})
            ).status_code == 200
            team_response = await client.get("/api/team")
            team = team_response.json()
            assert team["tree"][0]["name"] == "<script>alert(1)</script>"
            assert "Content-Security-Policy" in team_response.headers
            project = (await client.get("/api/project")).json()
            assert project["milestone_progress"] == 100
            assert (await client.get("/api/me")).json()["role"] == "lead"
            assert (await client.post("/api/logout", json={})).status_code == 200
            assert (await client.get("/api/me")).status_code == 401

    asyncio.run(exercise())
