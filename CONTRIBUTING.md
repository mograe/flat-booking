# Contributing to Flat Booking

Use a GitHub issue to report bugs or discuss larger changes. For confidential security reports, follow [SECURITY.md](SECURITY.md).

## Development setup

Install Python 3.12 or later and uv. From the repository root, run:

```bash
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest
```

Tests use temporary SQLite databases and Telegram test doubles. Use synthetic data in fixtures. If you change the schema, add an explicit Alembic revision and verify it on a temporary database using the [migration instructions](README.md#develop-and-test). Cover boundary cases when booking or access rules change. Use file-backed SQLite and separate connections for concurrency tests.

Keep Telegram-specific handling and Russian user-facing messages in the adapter. Keep the services independent of Aiogram and enforce authorization there as well as in protected Telegram flows. Use type annotations in new or changed code.

## Pull requests

Create a feature branch from `main` for substantial work. Review both `git diff` and `git diff --staged` before committing. Run pytest, Ruff lint, and the Ruff formatting check. Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/), for example `fix: preserve the original booking on failed reschedule`.

Open the pull request against `main`. Explain what changed, link the related issue, and list the checks you ran. Mention any migration or configuration changes. Keep credentials, Telegram user data, local databases, logs, and backup files out of commits and pull requests.
