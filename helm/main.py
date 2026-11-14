"""FastAPI application and API routes."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from helm.auth import SESSION_SECONDS, current_user, login
from helm.config import ConfigError, load_config
from helm.github import GitHubClient, GitHubError
from helm.store import Store

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
        yield

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

    app.mount("/assets", StaticFiles(directory=ROOT / "frontend"), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        return FileResponse(ROOT / "frontend" / "index.html")

    return app


app = create_app()
