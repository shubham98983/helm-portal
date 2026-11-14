"""FastAPI application and API routes."""

from __future__ import annotations

import csv
import io
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from helm.auth import SESSION_SECONDS, current_user, login, role_at_least
from helm.config import ConfigError, load_config
from helm.github import GitHubClient, GitHubError
from helm.logic import (
    approval_summary,
    attention_items,
    audit_verdict,
    board_column,
    check_summary,
    detect_ai_pr,
    is_bot_author,
    issue_column,
    required_approvers,
)
from helm.store import Store
from helm.sync import SyncService

ROOT = Path(__file__).resolve().parent.parent


class SecurityMiddleware:
    """Pure ASGI authentication gate and security headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        state = request.app.state
        path = request.url.path
        public = path in {
            "/health",
            "/login",
            "/assets/style.css",
            "/assets/app.js",
            "/favicon.ico",
        }
        needs_auth = not public and path != "/api/login" and not current_user(request) and not state.anonymous
        if needs_auth:
            response = (
                JSONResponse({"error": "Authentication required"}, status_code=401)
                if path.startswith("/api/")
                else RedirectResponse("/login", status_code=302)
            )
            await response(scope, receive, send)
            return

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"strict-origin-when-cross-origin"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; "
                            b"img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                            b"base-uri 'self'; frame-ancestors 'none'",
                        ),
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


class Credentials(BaseModel):
    username: str
    password: str


class SyncRequest(BaseModel):
    repo: str


def create_app(
    config_dir: str | Path | None = None, store: Store | None = None, github: GitHubClient | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "startup_error", None):
            raise RuntimeError(app.state.startup_error)
        if not app.state.users and not app.state.anonymous:
            raise RuntimeError(
                "No configured users. Add a password hash to config/team.toml or set "
                "HELM_ALLOW_ANONYMOUS=1 for development."
            )
        app.state.sync_service.start()
        try:
            yield
        finally:
            app.state.sync_service.stop()

    app = FastAPI(title="Helm", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store or Store()
    try:
        app.state.project, app.state.team = load_config(config_dir or ROOT / "config")
    except ConfigError as exc:
        app.state.startup_error = str(exc)
        app.state.project, app.state.team = {}, {"member": [], "ai_agent": []}
    app.state.users = {
        m["github"].lower(): m for m in app.state.team.get("member", []) if m.get("password_hash")
    }
    app.state.github = github or GitHubClient(app.state.store)
    app.state.anonymous = os.getenv("HELM_ALLOW_ANONYMOUS") == "1"
    app.state.sync_service = SyncService(
        app.state.store,
        app.state.github,
        app.state.project,
        app.state.team,
        int(os.getenv("HELM_POLL_SECONDS", "120")),
    )

    app.add_middleware(SecurityMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/login")
    async def login_page():
        return FileResponse(ROOT / "frontend" / "index.html")

    @app.post("/api/login")
    async def do_login(payload: Credentials, request: Request, response: Response):
        if "application/json" not in request.headers.get("content-type", ""):
            raise HTTPException(415, "Content-Type must be application/json")
        sid = login(
            app.state.store,
            app.state.users,
            payload.username,
            payload.password,
            request.client.host if request.client else "unknown",
        )
        if not sid:
            raise HTTPException(401, "Invalid credentials or login temporarily locked")
        response.set_cookie(
            "helm_session",
            sid,
            httponly=True,
            samesite="lax",
            secure=os.getenv("HELM_HTTPS") == "1",
            max_age=SESSION_SECONDS,
            path="/",
        )
        return {"ok": True}

    @app.post("/api/logout")
    async def do_logout(request: Request, response: Response):
        if "application/json" not in request.headers.get("content-type", ""):
            raise HTTPException(415, "Content-Type must be application/json")
        sid = request.cookies.get("helm_session")
        if sid:
            with app.state.store.connect() as db:
                db.execute("DELETE FROM sessions WHERE id=?", (sid,))
        response.delete_cookie("helm_session", path="/")
        return {"ok": True}

    @app.get("/api/me")
    async def me(request: Request):
        user = current_user(request)
        if not user and app.state.anonymous:
            return {"username": "anonymous", "name": "Anonymous", "role": "viewer"}
        if not user:
            raise HTTPException(401, "Authentication required")
        return {"username": user["github"], "name": user["name"], "role": user["role"]}

    @app.get("/api/team")
    async def team(request: Request):
        members = app.state.team.get("member", [])
        agents = app.state.team.get("ai_agent", [])
        by_name = {
            m["github"]: {
                "github": m["github"],
                "name": m["name"],
                "role": m["role"],
                "title": m["title"],
                "responsibilities": m.get("responsibilities", []),
                "reports_to": m.get("reports_to", ""),
                "children": [],
                "ai_agents": [],
                "workload": _member_workload(m["github"]),
            }
            for m in members
        }
        for agent in agents:
            supervisor = by_name[agent["supervised_by"]]
            supervisor["ai_agents"].append({**agent, "supervisor_name": supervisor["name"]})
        roots = []
        for member in by_name.values():
            if member["reports_to"]:
                by_name[member["reports_to"]]["children"].append(member)
            else:
                roots.append(member)
        return {
            "tree": roots,
            "ai_agents": [{**a, "supervisor_name": by_name[a["supervised_by"]]["name"]} for a in agents],
        }

    @app.get("/api/project")
    async def project(request: Request):
        profile = app.state.project
        milestones = profile.get("milestone", [])
        done = sum(m.get("status") == "done" for m in milestones)
        repos = []
        for repo in profile.get("repo", []):
            item = dict(repo)
            if app.state.github.token:
                try:
                    item["metadata"] = app.state.github.repo_metadata(repo["full_name"])
                except GitHubError as exc:
                    item["metadata_error"] = str(exc)
            repos.append(item)
        return {
            "profile": {k: v for k, v in profile.items() if k not in {"repo", "milestone"}},
            "repos": repos,
            "milestones": milestones,
            "milestone_progress": round(done / len(milestones) * 100) if milestones else 0,
        }

    def _sync_status() -> dict:
        with app.state.store.connect() as db:
            rows = db.execute(
                "SELECT repo,last_sync,last_error,request_count FROM sync_meta ORDER BY repo"
            ).fetchall()
        notices = []
        if not app.state.github.token and app.state.sync_service.repos:
            notices.append("GitHub token is not configured; showing locally cached data only.")
        notices.extend(f"{row['repo']}: {row['last_error']}" for row in rows if row["last_error"])
        syncing = app.state.sync_service.is_running
        if syncing:
            notices.append("A GitHub sync is in progress.")
        return {
            "repos": [dict(row) for row in rows],
            "last_synced": max((row["last_sync"] for row in rows if row["last_sync"]), default=None),
            "syncing": syncing,
            "notices": notices,
        }

    @app.get("/api/status")
    async def status():
        return _sync_status()

    @app.get("/api/work/issues")
    async def work_issues(repo: str | None = None, assignee: str | None = None, label: str | None = None):
        repo_names = _repo_names(repo)
        if repo and not repo_names:
            raise HTTPException(404, "Repository is not configured")
        issues, errors = [], {}
        for repository in repo_names:
            try:
                rows = app.state.github.issues(repository)
            except GitHubError as exc:
                errors[repository] = str(exc)
                continue
            with app.state.store.connect() as db:
                pulls = db.execute("SELECT number,payload FROM prs WHERE repo=?", (repository,)).fetchall()
            references = []
            for row in pulls:
                pull = json.loads(row["payload"])
                references.append((int(row["number"]), str(pull.get("body") or "")))
            for issue in rows:
                if issue.get("state") == "closed":
                    try:
                        closed = datetime.fromisoformat(issue.get("closed_at", "").replace("Z", "+00:00"))
                        if closed.tzinfo is None:
                            closed = closed.replace(tzinfo=timezone.utc)
                        if datetime.now(timezone.utc) - closed > timedelta(days=14):
                            continue
                    except ValueError:
                        continue
                assigned = [item.get("login", "") for item in issue.get("assignees", [])]
                labels = [item.get("name", "") for item in issue.get("labels", [])]
                if assignee and assignee.casefold() not in {value.casefold() for value in assigned}:
                    continue
                if label and label.casefold() not in {value.casefold() for value in labels}:
                    continue
                linked = next(
                    (number for number, body in references if f"#{issue.get('number')}" in body), None
                )
                issues.append(
                    {
                        "number": issue.get("number"),
                        "repo": repository,
                        "title": issue.get("title", ""),
                        "state": issue.get("state", "open"),
                        "column": issue_column(issue),
                        "url": issue.get("html_url", ""),
                        "created_at": issue.get("created_at"),
                        "updated_at": issue.get("updated_at"),
                        "assignees": assigned,
                        "labels": labels,
                        "linked_pr": linked,
                    }
                )
        issues.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return {"issues": issues, "errors": errors, "last_synced": _sync_status()["last_synced"]}

    @app.get("/api/overview")
    async def overview():
        repos = app.state.sync_service.repos
        records = _records(repos)
        now = datetime.now(timezone.utc)
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        open_records = [record for record in records if record["pull"].get("state") == "open"]
        opened_this_week = merged_this_week = 0
        for record in records:
            pull = record["pull"]
            created = pull.get("created_at") or ""
            merged = pull.get("merged_at") or ""
            if record["is_ai"] and created and created >= monday.isoformat():
                opened_this_week += 1
            if record["is_ai"] and merged and merged >= monday.isoformat():
                merged_this_week += 1

        main_ci = None
        ci_errors = {}
        issues_open = 0
        latest_runs = []
        for repository in repos:
            try:
                metadata = app.state.github.repo_metadata(repository)
                branch = metadata.get("default_branch") or "main"
                runs = app.state.github.workflow_runs(repository)
                matches = [run for run in runs if run.get("head_branch") == branch]
                if matches:
                    latest_runs.append((matches[0], repository))
            except GitHubError as exc:
                ci_errors[repository] = str(exc)
            try:
                issues_open += sum(
                    item.get("state") == "open" for item in app.state.github.issues(repository)
                )
            except GitHubError as exc:
                ci_errors.setdefault(repository, str(exc))
        if latest_runs:
            primary_repo = repos[0] if repos else ""
            primary_runs = [pair for pair in latest_runs if pair[1] == primary_repo]
            run, repository = max(primary_runs or latest_runs, key=lambda pair: pair[0].get("updated_at", ""))
            main_ci = {
                "repo": repository,
                "name": run.get("name", "Workflow"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "updated_at": run.get("updated_at"),
                "html_url": run.get("html_url"),
                "age": _age(run.get("updated_at")),
            }

        copilot = None
        copilot_url = os.getenv("COPILOT_URL", "").rstrip("/")
        if copilot_url:
            try:
                if not repos:
                    raise ValueError("No repository is configured")
                response = httpx.get(
                    f"{copilot_url}/api/repos/{quote(repos[0], safe='')}/pipeline/summary", timeout=3.0
                )
                response.raise_for_status()
                raw = response.json()
                score = raw.get("quality_score", raw.get("score"))
                coverage = raw.get("coverage")
                findings = raw.get("findings", {})
                if isinstance(findings, list):
                    finding_counts = {}
                    for finding in findings:
                        severity = str(finding.get("severity", "unknown")).casefold()
                        finding_counts[severity] = finding_counts.get(severity, 0) + 1
                    findings = finding_counts
                copilot = {
                    "status": "available",
                    "score": score if score is not None else "No data",
                    "coverage": coverage if coverage is not None else "No data",
                    "findings": findings,
                    "score_trend": raw.get("score_trend", raw.get("trend", [])),
                }
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                copilot = {"status": "unavailable", "label": "Copilot unavailable"}

        workloads = [
            {"github": member["github"], "name": member["name"], **_member_workload(member["github"])}
            for member in app.state.team.get("member", [])
        ]
        attention = attention_items(records, main_ci, _audit_rows(None, "violation"), workloads, now)
        awaiting = sum(bool(record["approvals"].get("missing")) for record in open_records)
        return {
            "main_ci": main_ci,
            "open_prs": {
                "ai": sum(r["is_ai"] for r in open_records),
                "human": sum(not r["is_ai"] for r in open_records),
            },
            "awaiting_approval": awaiting,
            "ai_changes_this_week": {"opened": opened_this_week, "merged": merged_this_week},
            "open_issues": issues_open,
            "copilot": copilot,
            "needs_attention": attention,
            "errors": ci_errors,
            "last_synced": _sync_status()["last_synced"],
        }

    def _age(value: str | None) -> str | None:
        if not value:
            return None
        try:
            occurred = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=timezone.utc)
            seconds = max(0, int((datetime.now(timezone.utc) - occurred).total_seconds()))
            if seconds < 3600:
                return f"{max(1, seconds // 60)}m ago"
            if seconds < 86400:
                return f"{seconds // 3600}h ago"
            return f"{seconds // 86400}d ago"
        except ValueError:
            return None

    @app.post("/api/sync")
    async def sync_now(payload: SyncRequest, request: Request):
        if "application/json" not in request.headers.get("content-type", ""):
            raise HTTPException(415, "Content-Type must be application/json")
        if not role_at_least(current_user(request), "lead"):
            raise HTTPException(403, "Lead role required")
        try:
            return app.state.sync_service.sync_repo_manual(payload.repo)
        except ValueError as exc:
            raise HTTPException(429 if "synced less than" in str(exc) else 404, str(exc)) from exc
        except GitHubError as exc:
            raise HTTPException(502, str(exc)) from exc

    def _repo_names(repo: str | None) -> list[str]:
        configured = app.state.sync_service.repos
        if repo:
            return [repo] if repo in configured else []
        return configured

    def _records(repo_names: list[str]):
        result = []
        with app.state.store.connect() as db:
            for repository in repo_names:
                rows = db.execute("SELECT * FROM prs WHERE repo=?", (repository,)).fetchall()
                for row in rows:
                    pull = json.loads(row["payload"])
                    base_branch = pull.get("base", {}).get("ref")
                    approver_row = (
                        db.execute(
                            "SELECT content FROM repo_approvers WHERE repo=? AND branch=?",
                            (repository, base_branch),
                        ).fetchone()
                        if base_branch
                        else None
                    )
                    approvers_file = approver_row["content"] if approver_row else None
                    reviews = [
                        json.loads(x[0])
                        for x in db.execute(
                            "SELECT payload FROM reviews WHERE repo=? AND pr_number=?",
                            (repository, row["number"]),
                        )
                    ]
                    checks = [
                        json.loads(x[0])
                        for x in db.execute(
                            "SELECT payload FROM checks WHERE repo=? AND pr_number=?",
                            (repository, row["number"]),
                        )
                    ]
                    commits = json.loads(row["commits_payload"] or "[]")
                    reasons = detect_ai_pr(pull, commits)
                    user = pull.get("user", {}).get("login", "")
                    required = (
                        None
                        if approvers_file == "__ERROR__"
                        else required_approvers(approvers_file, app.state.team.get("member", []), user)
                    )
                    approvals = approval_summary(required, reviews, pull.get("head", {}).get("sha", ""))
                    result.append(
                        {
                            "repo": repository,
                            "pull": pull,
                            "reviews": reviews,
                            "checks_data": checks,
                            "checks": check_summary(checks),
                            "approvals": approvals,
                            "required": required,
                            "is_ai": bool(reasons),
                            "ai_reasons": reasons,
                            "is_bot": is_bot_author(pull),
                        }
                    )
        return result

    @app.get("/api/prs/board")
    async def prs_board(repo: str | None = None, ai: int = 0):
        columns = {name: [] for name in ("draft", "ci", "awaiting", "ready", "done")}
        for record in _records(_repo_names(repo)):
            if ai and not record["is_ai"]:
                continue
            pull = record["pull"]
            column = board_column(pull, record["checks"], record["approvals"])
            if not column:
                continue
            approvals = record["approvals"]
            columns[column].append(
                {
                    "number": pull.get("number"),
                    "repo": record["repo"],
                    "title": pull.get("title", ""),
                    "author": pull.get("user", {}).get("login", ""),
                    "branch": pull.get("head", {}).get("ref", ""),
                    "is_ai": record["is_ai"],
                    "ai_reasons": record["ai_reasons"],
                    "is_bot": record["is_bot"],
                    "checks": record["checks"],
                    "approvals": approvals,
                    "url": pull.get("html_url", ""),
                    "updated_at": pull.get("updated_at"),
                }
            )
        for cards in columns.values():
            cards.sort(key=lambda card: card.get("updated_at") or "", reverse=True)
        return {"columns": columns}

    @app.get("/api/activity")
    async def activity(
        repo: str | None = None,
        actor: str = Query("all", pattern="^(all|ai|human)$"),
        kind: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        before: str | None = None,
    ):
        repo_names = _repo_names(repo)
        if repo and not repo_names:
            raise HTTPException(404, "Repository is not configured")
        if not repo_names:
            return {"events": [], "next_before": None}
        placeholders = ",".join("?" for _ in repo_names)
        where = [f"repo IN ({placeholders})"]
        params: list = list(repo_names)
        if actor != "all":
            where.append("actor_type=?")
            params.append(actor)
        if kind:
            where.append("kind=?")
            params.append(kind)
        if before:
            where.append("created_at<?")
            params.append(before)
        params.append(limit + 1)
        with app.state.store.connect() as db:
            rows = db.execute(
                f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?", params
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [
            {
                "repo": row["repo"],
                "key": row["event_key"],
                "kind": row["kind"],
                "actor": row["actor"],
                "actor_type": row["actor_type"],
                "created_at": row["created_at"],
                **json.loads(row["payload"]),
            }
            for row in rows
        ]
        copilot_url = os.getenv("COPILOT_URL", "").rstrip("/")
        if copilot_url and (actor in {"all", "ai"}) and kind in {None, "copilot_failure"}:
            for repository in repo_names:
                try:
                    response = httpx.get(
                        f"{copilot_url}/api/repos/{quote(repository, safe='')}/runs", timeout=3.0
                    )
                    response.raise_for_status()
                    payload = response.json()
                    runs = payload if isinstance(payload, list) else payload.get("runs", [])
                    for run in runs:
                        if str(run.get("status", run.get("conclusion", ""))).casefold() not in {
                            "failure",
                            "failed",
                            "error",
                        } and str(run.get("conclusion", "")).casefold() not in {"failure", "failed", "error"}:
                            continue
                        occurred = run.get("created_at", run.get("updated_at", ""))
                        if before and occurred >= before:
                            continue
                        events.append(
                            {
                                "repo": repository,
                                "key": f"copilot:{run.get('id', '')}",
                                "kind": "copilot_failure",
                                "actor": "AI DevOps Copilot",
                                "actor_type": "ai",
                                "is_ai": True,
                                "created_at": run.get("created_at", run.get("updated_at", "")),
                                "name": run.get("name", "Failure analysis"),
                                "message": run.get("summary", run.get("analysis", "Failed run analysis")),
                                "url": run.get("html_url", ""),
                            }
                        )
                except (httpx.HTTPError, ValueError, TypeError):
                    continue
        events.sort(key=lambda event: event.get("created_at", ""), reverse=True)
        events = events[:limit]
        return {"events": events, "next_before": events[-1]["created_at"] if has_more and events else None}

    def _audit_rows(repo: str | None, verdict: str | None):
        records = _records(_repo_names(repo))
        rows = []
        for record in records:
            pull = record["pull"]
            if not pull.get("merged_at") or not record["is_ai"]:
                continue
            result = audit_verdict(
                pull,
                record["required"],
                record["reviews"],
                record["checks_data"],
                pull.get("head", {}).get("sha"),
            )
            if verdict and result["verdict"] != verdict:
                continue
            rows.append(
                {
                    "repo": record["repo"],
                    "number": pull.get("number"),
                    "title": pull.get("title", ""),
                    "author": pull.get("user", {}).get("login", ""),
                    "url": pull.get("html_url", ""),
                    "merged_at": pull.get("merged_at"),
                    "verdict": result["verdict"],
                    "reasons": result["reasons"],
                    "required": record["required"],
                    "approvals": record["approvals"],
                    "reviews": record["reviews"],
                    "checks": record["checks"],
                }
            )
        rows.sort(key=lambda row: row.get("merged_at") or "", reverse=True)
        return rows

    def _require_audit_role(request: Request) -> None:
        if not role_at_least(current_user(request), "lead", "reviewer"):
            raise HTTPException(403, "Lead or reviewer role required")

    @app.get("/api/audit")
    async def audit(request: Request, repo: str | None = None, verdict: str | None = None):
        _require_audit_role(request)
        return {"entries": _audit_rows(repo, verdict)}

    @app.get("/api/audit.csv")
    async def audit_csv(request: Request, repo: str | None = None, verdict: str | None = None):
        _require_audit_role(request)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "repo",
                "number",
                "title",
                "author",
                "merged_at",
                "verdict",
                "reasons",
                "required",
                "approved_by",
            ]
        )
        for row in _audit_rows(repo, verdict):
            values = [
                row["repo"],
                row["number"],
                row["title"],
                row["author"],
                row["merged_at"],
                row["verdict"],
                "; ".join(row["reasons"]),
                "; ".join(row["required"] or []),
                "; ".join(row["approvals"]["approved_by"]),
            ]
            writer.writerow(
                [f"'{value}" if str(value).startswith(("=", "+", "-", "@")) else value for value in values]
            )
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=helm-audit.csv"},
        )

    def _member_workload(github: str) -> dict[str, int]:
        done = pending = ai_opened = open_count = 0
        username = github.casefold()
        for record in _records(_repo_names(None)):
            pull = record["pull"]
            if pull.get("user", {}).get("login", "").casefold() == username and record["is_ai"]:
                ai_opened += 1
            reviews = record["reviews"]
            if any(
                review.get("user", {}).get("login", "").casefold() == username
                and review.get("state", "").upper() not in {"COMMENTED", "DISMISSED"}
                for review in reviews
            ):
                done += 1
            if pull.get("state") != "open":
                continue
            open_count += 1
            requested = {u.get("login", "").casefold() for u in pull.get("requested_reviewers", [])}
            reviewed = {r.get("user", {}).get("login", "").casefold() for r in reviews}
            if username in requested and username not in reviewed:
                pending += 1
        return {
            "reviews_done": done,
            "reviews_pending": pending,
            "ai_prs_opened": ai_opened,
            "open_prs": open_count,
        }

    @app.get("/api/members/{github}/workload")
    async def member_workload(github: str):
        if github.casefold() not in {m["github"].casefold() for m in app.state.team.get("member", [])}:
            raise HTTPException(404, "Member not found")
        return {"github": github, **_member_workload(github)}

    app.mount("/assets", StaticFiles(directory=ROOT / "frontend"), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        return FileResponse(ROOT / "frontend" / "index.html")

    return app


app = create_app()
