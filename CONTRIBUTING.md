# Contributing to llmproxy

First off, thank you for considering contributing to `llmproxy`! It's people like you that make open source such a great community.

## How can I contribute?

### Reporting Bugs
- Make sure you are on the latest version.
- Use the GitHub Issues tab to search if the bug has already been reported.
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
