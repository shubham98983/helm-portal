# Setup

## Requirements and configuration

Use Python 3.12. Copy `config/project.example.toml` to `config/project.toml` and `config/team.example.toml` to `config/team.toml`; replace the example repository and team details. Configure exactly one head member with no `reports_to`. Create a password hash with `python -m helm.cli hash-password`, then place the complete hash in that member's `password_hash`. Helm has no default account.

Set `GITHUB_TOKEN` in the service environment. Grant only the repository metadata, pull request, issues, actions/checks, and contents read permissions needed for configured repositories. Helm only issues GET requests and does not need write, approval, or merge permissions. Set `COPILOT_URL` only when using the optional Copilot service. `HELM_POLL_SECONDS` controls background polling (default 120); `DATA_DIR` holds the SQLite database. Set `HELM_HTTPS=1` behind HTTPS to enable secure session cookies. Use `HELM_ALLOW_ANONYMOUS=1` only for a local development instance; it permits example config fallback and viewer access.

## Run locally

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m helm
```

Helm listens on `127.0.0.1:8100`. `uvicorn helm.main:app --host 127.0.0.1 --port 8100` is also supported.

## Run with Docker Compose

Create the real config files and environment values first, then run `docker compose up --build`. Compose binds to `127.0.0.1:8100` and persists its database in a named volume. Keep `.env` and the real config files out of source control.
