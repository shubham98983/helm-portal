# Helm: project command center (internal portal)

Helm is a READ-ONLY internal portal that shows how a software project is run: team hierarchy, who or what changed what (humans and AI), pull request
approvals, CI results, work items, and an audit trail of AI-made changes. It reads GitHub's REST API (GET only) and optionally the AI DevOps Copilot's
API. It never writes to GitHub, never approves or merges, and never calls an LLM.

## Separation
Related projects (context only; NEVER edit, import or modify them):
- ~/ai-devops-copilot: FastAPI + RAG Q&A + CI failure analyzer + security explainer + quality dashboard, port 8000, no login.
- ~/inventory-management-demo: Flask target app with a GitHub Actions CI. Codex fixes planted bugs through PRs (branches ai/..., label ai-generated).
Helm talks to the copilot ONLY over HTTP when COPILOT_URL is set (GET /api/repos/{id}/pipeline/summary and /runs). If it is unset or down, Helm works without it.
Helm runs on port 8100 with its own SQLite file in DATA_DIR (default ./data) and its own GITHUB_TOKEN.

## Stack and layout
Python 3.12, FastAPI, sqlite3 (stdlib), httpx, tomllib (stdlib) for config, plain HTML/CSS/JS frontend with hash routing (#/home), NO build step, NO JS
dependencies. Package helm/ (config.py, github.py, store.py, logic.py, sync.py, auth.py, api/, cli.py, main.py), frontend/, config/, tests/, docs/.
Tests: pytest, httpx.MockTransport with realistic GitHub JSON; frontend checks with jsdom installed in a temp dir (not in the project).
Config as code (TOML): config/project.toml and config/team.toml. Commit only *.example.toml; the real files are gitignored (they contain password hashes).

## Domain rules (single source of truth, implement once in helm/logic.py)
- AI-made PR if ANY: label "ai-generated" (case-insensitive) -> reason "label"; head branch starts with "ai/" or "ai-fix/" -> "branch"; any commit message has a
  line matching ^AI-Assisted:\s*\S+ (case-insensitive) -> "commit trailer". is_bot is separate: author login ends with "[bot]".
- Review state per reviewer: latest review whose state is not COMMENTED; APPROVED is "current" only if its commit_id equals the PR head SHA, otherwise "stale";
  CHANGES_REQUESTED blocks; DISMISSED clears that reviewer.
- Required approvers: .github/APPROVERS in the repo (base branch), one GitHub username per line, # comments, leading @ stripped, lowercase, minus the PR author.
  If the file is missing, fall back to members with role lead or reviewer in team.toml (minus author); if neither exists required is null.
- Check summary: failing = failure, cancelled, timed_out, action_required; pending = queued, in_progress; passing = success, neutral, skipped.
- Board column: closed or merged -> done (last 14 days); draft -> draft; checks failing or pending -> ci; green but approvals incomplete or changes requested ->
  awaiting; else ready.
- Audit verdict for a MERGED AI PR: compliant if every required approver has a current APPROVED review (commit_id == head SHA at merge, submitted before
  merged_at), no blocking CHANGES_REQUESTED, and checks on that SHA were passing; otherwise "violation" with the reasons (which approvers missing, which checks
  failed); "unknown" if required is null.

## Security rules
- Everything from GitHub (titles, branches, labels, names) is untrusted: render with textContent only, never innerHTML; links only to https://github.com/.
- GitHub requests are GET only (a test asserts it); the token is sent only to api.github.com, never logged or returned by any endpoint.
- Login required for every page and API except /health and the login page. Passwords hashed with hashlib.scrypt and a per-user salt; session ids random, stored
  server-side in SQLite, cookie HttpOnly + SameSite=Lax (+Secure when HELM_HTTPS=1), 8 h expiry; login rate limit 5 failures per 10 minutes per user and IP;
  POST endpoints require Content-Type application/json. No default accounts. Refuse to start with no users unless HELM_ALLOW_ANONYMOUS=1 (development only).
  Bind to 127.0.0.1 by default. Add security headers including a Content-Security-Policy that allows only same-origin scripts and styles.
- sqlite3 execute() runs ONE statement; use executescript for multi-statement schemas, and test the store layer directly.
- Do not pin anything to "latest".

## Working rules
- Branch ai/<topic> from an up-to-date main; small commits; tests first; never push to main; never merge.
- Run the FULL pytest suite and "ruff check ." (line length 110, rules E, F, I) before every commit and show me the results.
- Draft PR with label ai-generated and an "AI disclosure" section (gh pr create --draft --label ai-generated). If a label does not exist, create it.
- No new Python dependency beyond fastapi, uvicorn, httpx (plus pytest and ruff for development) without asking me.
- Ask me instead of guessing when a requirement is unclear. Never touch .env or print secrets.
