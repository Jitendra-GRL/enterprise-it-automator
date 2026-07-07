# Contributing

## Dev setup

```bash
git clone https://github.com/JitendraPrabhu-l/enterprise-it-automator.git
cd enterprise-it-automator
python -m venv .venv && source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements.txt
pip install ruff pytest pytest-asyncio               # or: pip install -e ".[dev]"

cp .env.example .env
# set at least GROQ_API_KEY (free: https://console.groq.com/keys)
```

## Before opening a PR

```bash
ruff check app/ tests/
pytest -v
```

Both run in CI (`.github/workflows/ci.yml`) on every push/PR to `main` — a PR won't
merge cleanly if either fails locally.

## Project layout

```
app/
  agent/        LangGraph graph, LLM adapter, prompts, AG-UI streaming bridge
  api/           FastAPI routes, auth, RBAC, request/response schemas
  db/            SQLAlchemy models, session/engine setup, seed script
  mcp_server/    MCP tool server (identity/access/ticketing domains + gateway)
  static/        Vanilla HTML/CSS/JS dashboard (no build step)
tests/           One test file per module under test, mirroring app/'s layout
```

## Conventions

- **Dependencies**: add to both `requirements.txt` and `pyproject.toml`'s
  `dependencies` list, then regenerate `requirements.lock.txt`:
  `uv pip compile requirements.txt -o requirements.lock.txt --python-platform linux --python-version 3.13`
  (Linux-targeted deliberately — that's what CI and the Docker image run on;
  compiling without `--python-platform linux` on a Windows/macOS dev machine
  will pull in the wrong platform-specific wheels).
- **Tests**: prefer a real dependency (real subprocess, real SQLite file, a
  compiled LangGraph graph) over a mock wherever the test's whole point is
  proving real integration behavior — several bugs in this codebase's history
  were only caught because a test exercised the real thing instead of a stub
  standing in for it (see inline comments in `tests/test_mcp_transport.py`,
  `tests/test_retry_policy.py` for examples).
- **Comments**: only where the *why* isn't obvious from the code itself — a
  hidden constraint, a workaround for a specific bug, a non-obvious tradeoff.
  Not restating what the code already says.
- **Config**: every setting lives in `app/config.py`'s `Settings` class with a
  safe default; document it in `.env.example` with a comment explaining what
  it does and when you'd change it.

## Reporting issues

Open a GitHub issue with repro steps. If it's security-sensitive, please don't
open a public issue — see below.

## Security

This project handles simulated sensitive actions (account disable, access
revocation) behind a human-approval gate — see the README's **Identity &
approval authorization** section for the current security model and its
explicitly scoped-down pieces. If you find a real vulnerability, please report
it privately rather than filing a public issue.
