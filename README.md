# Helm

Helm is a read-only project command center for team ownership, project context, and delivery activity. It reads GitHub through GET requests only and never writes to GitHub.

## Quick start

```sh
cp .env.example .env
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
cp config/project.example.toml config/project.toml
cp config/team.example.toml config/team.toml
```

Create the first password hash with `python -m helm.cli hash-password`, then put the printed value in the head member's `password_hash` field in `config/team.toml`. Set `GITHUB_TOKEN` in your environment or `.env` (Helm does not load `.env` automatically; export values in your shell). Configure `DATA_DIR` for the SQLite database, then run:

```sh
python -m helm
```

Open <http://127.0.0.1:8100>. Set `HELM_ALLOW_ANONYMOUS=1` only for local development without sign-in; it enables example configuration fallback when real config files are absent. You can also run `uvicorn helm.main:app --host 127.0.0.1 --port 8100`.

Run checks with `pytest` and `ruff check .`.

