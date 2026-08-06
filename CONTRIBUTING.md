# Contributing to llmproxy

First off, thank you for considering contributing to `llmproxy`! It's people like you that make open source such a great community.

## How can I contribute?

### Reporting Bugs
- Make sure you are on the latest version.
- Use the GitLab Issues tab to search if the bug has already been reported.
- If not, open a new issue. Include a clear description of the problem, steps to reproduce it, and any relevant logs (`journalctl -u llmproxy`).

### Suggesting Enhancements
- Open a new issue with the label `enhancement`.
- Describe the current behavior and the new behavior you want to see.
- Explain why this enhancement would be useful to most users.

### Submitting Pull Requests
1. Fork the repo and create your branch from `main`.
2. If you've added code that should be tested, add tests.
3. Update the documentation (in `docs/` or `README.md`) if you change features or architecture.
4. Ensure your code follows the existing style and conventions.
5. Issue the pull request!

## Development Setup
The dashboard is built with FastAPI and runs in a Docker container, while the core proxy is a standalone Python application.
- To work on the core proxy, check `proxy/llmproxy.py`.
- To work on the dashboard, check the `dashboard/` directory.

### Running locally
You can run the proxy locally using:
```bash
python3 proxy/llmproxy.py
```
And the dashboard:
```bash
cd dashboard
uvicorn app:app --reload --port 8000
```

### Running the test suite
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/ -q
```
`tests/` covers `proxy/` guardrail rules, RBAC, and chargeback currency
math, plus `dashboard/` auth gating and API passthroughs — see
`requirements-dev.txt` and `tests/proxy/conftest.py` for why it doesn't
need the heavier DLP dependencies (presidio-analyzer/spacy) installed.
`mcp_server/` has its own separate test suite; see `mcp_server/README.md`.

## Project Philosophy

This codebase is built agentically (with Claude Code) and run as a hobby
project in the maintainer's spare time — there's no roadmap, SLA, or
guarantee that a given issue or pull request gets reviewed. Contributions
and reports are genuinely welcome, but they get acted on when they
happen to interest the maintainer, not on any particular schedule.
