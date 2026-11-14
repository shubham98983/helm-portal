"""TOML configuration loading and validation."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when project configuration is missing or invalid."""


def _load(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    return data


def _required(value: dict[str, Any], key: str, kind: type, source: str) -> None:
    if not isinstance(value.get(key), kind):
        raise ConfigError(f"{source}: '{key}' must be {kind.__name__}")


def validate_project(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("name", "tagline", "purpose", "phase"):
        _required(data, key, str, "project.toml")
    _required(data, "tech_stack", list, "project.toml")
    for section in ("repo", "environment", "milestone", "links", "runbook"):
        rows = data.get(section, [])
        if not isinstance(rows, list):
            raise ConfigError(f"project.toml: '{section}' must be an array of tables")
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise ConfigError(f"project.toml: {section} entry {index} must be a table")
    statuses = {"planned", "active", "done"}
    for item in data.get("milestone", []):
        if item.get("status") not in statuses:
            raise ConfigError("project.toml: milestone status must be planned, active, or done")
        if not isinstance(item.get("deliverables", []), list):
            raise ConfigError("project.toml: milestone deliverables must be an array")
    return data


def validate_team(data: dict[str, Any]) -> dict[str, Any]:
    members = data.get("member")
    agents = data.get("ai_agent", [])
    if not isinstance(members, list) or not members:
        raise ConfigError("team.toml: define at least one [[member]]")
    if not isinstance(agents, list):
        raise ConfigError("team.toml: [[ai_agent]] entries must be an array")
    by_github: dict[str, dict[str, Any]] = {}
    roles = {"lead", "reviewer", "member", "viewer"}
    for item in members:
        if not isinstance(item, dict):
            raise ConfigError("team.toml: each member must be a table")
        for key in ("github", "name", "role", "title"):
            _required(item, key, str, "team.toml member")
        github = item["github"].lower()
        if github in by_github:
            raise ConfigError(f"team.toml: duplicate member github '{github}'")
        if item["role"] not in roles:
            raise ConfigError(f"team.toml: member '{github}' role must be lead, reviewer, member, or viewer")
        if not isinstance(item.get("responsibilities", []), list):
            raise ConfigError(f"team.toml: member '{github}' responsibilities must be an array")
        item["github"] = github
        item.setdefault("reports_to", "")
        item.setdefault("password_hash", "")
        by_github[github] = item
    heads = [m for m in members if not m.get("reports_to")]
    if len(heads) != 1:
        raise ConfigError("team.toml: exactly one member must have no reports_to (the head)")
    for member in members:
        parent = member.get("reports_to")
        if parent and parent.lower() not in by_github:
            raise ConfigError(f"team.toml: member '{member['github']}' reports_to unknown member '{parent}'")
        member["reports_to"] = parent.lower() if parent else ""
    for github in by_github:
        seen: set[str] = set()
        cursor = github
        while cursor:
            if cursor in seen:
                raise ConfigError(f"team.toml: reporting cycle involving '{cursor}'")
            seen.add(cursor)
            cursor = by_github[cursor].get("reports_to", "")
    for agent in agents:
        if not isinstance(agent, dict):
            raise ConfigError("team.toml: each AI agent must be a table")
        for key in ("id", "name", "purpose", "supervised_by"):
            _required(agent, key, str, "team.toml AI agent")
        supervisor = agent["supervised_by"].lower()
        if supervisor not in by_github:
            raise ConfigError(f"team.toml: AI agent '{agent['id']}' has unknown supervisor '{supervisor}'")
        agent["supervised_by"] = supervisor
        for key in ("may", "may_not"):
            if not isinstance(agent.get(key, []), list):
                raise ConfigError(f"team.toml: AI agent '{agent['id']}' {key} must be an array")
            agent.setdefault(key, [])
    return data


def load_config(
    config_dir: str | Path = "config", allow_anonymous: bool | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    base = Path(config_dir)
    if allow_anonymous is None:
        allow_anonymous = os.getenv("HELM_ALLOW_ANONYMOUS") == "1"
    project_path, team_path = base / "project.toml", base / "team.toml"
    if allow_anonymous:
        if not project_path.exists():
            project_path = base / "project.example.toml"
        if not team_path.exists():
            team_path = base / "team.example.toml"
    project = validate_project(_load(project_path))
    team = validate_team(_load(team_path))
    return project, team
