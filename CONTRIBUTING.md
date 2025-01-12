# Contributing

Small, verifiable changes. This project is a security- and money-adjacent proxy, so a
change that "works" is not enough: it must be shown to fail closed and to keep the ledger
reconciled.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"    # Windows
# .venv/bin/python -m pip install -e ".[dev]"      # macOS / Linux
```

Python 3.12 is the pinned baseline (`pyproject.toml`).

## Before you open a pull request

```bash
.venv/Scripts/python -m pytest tests -q
.venv/Scripts/python -m ruff check src tests
.venv/Scripts/python -m ruff format --check src tests
.venv/Scripts/python -m mypy
.venv/Scripts/python scripts/smoke.py
```

All five must pass. CI runs the same set on Linux and Windows. If a check cannot run in
your environment, say so in the pull request instead of deleting the check.

## What reviewers look for

- **Correctness under failure.** New code paths must state what happens when the ledger is
  unavailable, the provider times out, or the response body is malformed.
- **No silent spend.** Any change to reservation, settlement, or routing needs a test that
  asserts the ledger totals still reconcile with the attempts.
- **No prompt retention by accident.** Prompt text may reach logs only under
  `server.log_prompts = true` and must pass through `redact_value` first.
- **Stable contracts.** Error codes, response shapes, and CLI exit codes are documented;
  changing one requires updating the docs in the same pull request.
- **No new mandatory dependency.** Optional services must stay optional, with a local
  fallback that works offline.

## Tests

- `tests/unit/` — pure logic: money, tokens, redaction, config, ledger, gateway, providers.
- `tests/integration/` — the real HTTP app and the real CLI, including failure injection.
- `scripts/smoke.py` — end-to-end against a live server; also the documented manual check.

Prefer a test that would catch a plausible consumer-visible bug (boundary, precedence,
error mapping, transition) over one that restates the implementation. Do not add tests that
only assert a value was copied from a config file.

## Style

- `ruff` for lint and formatting, `mypy --strict` on `src/`.
- No `print` in library code; use the module logger. The CLI is the exception.
- Comments explain *why* an invariant holds, not what the line does.
- Keep public functions small enough to test without a fixture maze.

## Reporting bugs

Include: version or commit, config shape with secrets removed, the exact request (redacted),
the response, and the relevant ledger rows (`cost-firewall trace <id>`). Security issues go
through [SECURITY.md](SECURITY.md), not the public issue tracker.

## Commit messages

`area: imperative summary` — e.g. `ledger: reject settlement without a reservation`.
One logical change per commit. Do not mix formatting-only churn with behavior changes.

## License

By contributing you agree your work is licensed under the MIT license in [LICENSE](LICENSE).
