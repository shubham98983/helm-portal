import asyncio
import shutil

import pytest

from helm.config import ConfigError, load_config, validate_team
from helm.store import Store


def team():
    return {
        "member": [
            {"github": "lead", "name": "Lead", "role": "lead", "title": "Lead", "reports_to": ""},
            {"github": "dev", "name": "Dev", "role": "member", "title": "Dev", "reports_to": "lead"},
        ],
        "ai_agent": [],
    }


def test_example_configs_load():
    project, members = load_config("config", allow_anonymous=True)
    assert project["name"] == "Helm Portal"
    assert len(members["member"]) == 2


def test_reporting_cycle_rejected():
    data = team()
    data["member"][0]["reports_to"] = "dev"
    data["member"].append(
        {"github": "other", "name": "Other", "role": "member", "title": "Other", "reports_to": ""}
    )
    with pytest.raises(ConfigError, match="cycle"):
        validate_team(data)


def test_missing_head_rejected():
    data = team()
    data["member"][0]["reports_to"] = "dev"
    with pytest.raises(ConfigError, match="exactly one"):
        validate_team(data)


def test_unknown_supervisor_rejected():
    data = team()
    data["ai_agent"] = [{"id": "a", "name": "A", "purpose": "P", "supervised_by": "nobody"}]
    with pytest.raises(ConfigError, match="unknown supervisor"):
        validate_team(data)


def test_anonymous_examples_are_refused_without_flag(tmp_path):
    from helm.main import create_app

    config = tmp_path / "config"
    config.mkdir()
    shutil.copy("config/project.example.toml", config / "project.toml")
    shutil.copy("config/team.example.toml", config / "team.toml")
    app = create_app(config, store=Store(str(tmp_path / "data")))
    assert app.state.users == {}
    assert not app.state.anonymous
    with pytest.raises(RuntimeError, match="No configured users"):
        asyncio.run(enter_lifespan(app))


async def enter_lifespan(app):
    async with app.router.lifespan_context(app):
        pass
