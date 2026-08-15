# Development

## Layout

```
src/cost_firewall/   library and server (no I/O outside ledger/providers/app)
tests/unit/          pure logic, in-memory ledger, mock HTTP transports
tests/integration/   real FastAPI app and real CLI subprocesses
tests/conftest.py    shared config builder; the only place fixtures are defined
scripts/smoke.py     end-to-end check against a live server on a free port
scripts/backdate-history.sh / .ps1   rebuild synthetic commit history (see help)
docs/                architecture, API, dashboard, design tokens, operations, threat model, privacy
decisions/           ADRs for choices that are expensive to reverse
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"    # Windows
# .venv/bin/python -m pip install -e ".[dev]"      # macOS / Linux
```

Python 3.12. Dependencies are pinned in `pyproject.toml`; there is no lockfile because the
runtime dependency set is three packages and the pins are exact.

## The five checks

```bash
.venv/Scripts/python -m pytest tests -q
.venv/Scripts/python -m ruff check src tests scripts
.venv/Scripts/python -m ruff format --check src tests scripts
.venv/Scripts/python -m mypy
.venv/Scripts/python scripts/smoke.py
```

`mypy` runs in strict mode over `src`, `tests`, and `scripts`. Tests are type-checked too:
the mocks in `tests/` are the ones most likely to drift from the real interface.

`scripts/smoke.py` starts a real uvicorn server on an OS-assigned free port, drives it over
HTTP, and asserts the documented behaviors. It is the check that catches wiring mistakes that
in-process tests cannot see (middleware, error envelope rendering, log redaction).

## Conventions

### Money

All money is an integer number of micro-USD (`money.py`). Config accepts USD strings or
numbers and converts with `Decimal`, never `float`. Display strings are produced by
`format_usd` and are always derived from the integer. Do not introduce a float into a
budget comparison.

### Errors

Client-visible failures are `GatewayError` subclasses with a stable `code`, `status_code`,
and `type` (`errors.py`). Adding a code means updating `docs/api.md` in the same commit.
Provider failures are `ProviderError` with an `error_class`; the router — not the adapter —
decides whether fallback is allowed.

### Ledger

`Ledger` is the only module that opens SQLite. Every write goes through
`_write_transaction`, which starts with `BEGIN IMMEDIATE` so a budget check cannot be based
on an invalidated snapshot. If you add a write path, it must either be inside an existing
transaction or be a new `_write_transaction` block; never use the connection's implicit
transaction mode (`isolation_level=None` is set deliberately).

### Tests

- Prefer behavior over structure: assert the ledger total, the error code, the number of
  attempt rows — not that a function called another function.
- Use the fake provider for routing tests and `httpx.MockTransport` for adapter tests.
- Do not sleep. `FakeProvider.latency_ms` exists for manual checks only.
- A concurrency test must assert the invariant (no overspend, full reconciliation), not a
  timing.

### Logging

Library modules use `logging.getLogger(__name__)`. Anything that could contain user text
goes through `LogRedactor.redact_value` before it is formatted. The CLI may `print`; the
library may not.

## Adding a provider

1. Implement `complete()` and `count_tokens()` to satisfy `ProviderAdapter`.
2. Raise `ProviderError` with a classification for every failure path — never let an
   `httpx` exception escape.
3. Require usage evidence. A response without token counts must be a permanent failure; the
   gateway refuses to bill an unverifiable request.
4. Read credentials from the environment variable named by `api_key_env`; never log them.
5. Register the type in `PROVIDER_TYPES` (`config.py`) and in `build_adapter()`.
6. Add adapter tests with recorded response bodies (happy path, `4xx`, `5xx`, malformed,
   missing usage) and one routing test through `Gateway`.

## Adding a config key

1. Parse it in `config.py` with an explicit type check and a default.
2. Reject unknown keys: `_table` helpers already do this per section.
3. Document it in `docs/operations.md`.
4. Add a validation test for the bad value, not only the good one.

## Debugging

- `cost-firewall trace <request_id> --config config.toml` shows attempts and cost.
- `cost-firewall dry-run --model <m> --prompt <p>` shows the routing and budget decision
  without spending anything.
- `cost-firewall loadtest --concurrency 16 --requests 400` prints wall time, throughput, and
  whether the ledger overspent or left unreconciled rows. It uses a throwaway ledger unless
  `--database` names one.
- `python scripts/http_bench.py --requests 400 --concurrency 16` does the same over real
  HTTP and reports p50/p95 latency.
- Set `log_level = "DEBUG"` for metadata logging; `log_prompts = true` only for a bounded
  session, and remember the prompts land in your log sink.

## Dashboard

`src/cost_firewall/dashboard/` is four static files served by the gateway: `index.html`,
`app.js` (ES module, no framework), `app.css`, and `tokens.css`. There is no build step, so
there is nothing to compile and no generated artefact to keep in sync.

- Style values come from `tokens.css` only; `docs/design-tokens.md` is the register of what
  each token means and its measured contrast. A new colour is a change to the register.
- Data reaches the DOM through `textContent` only. Prompt text never arrives, because the
  ledger does not store it.
- Every asynchronous outcome goes through `setStatus`, which writes to the reserved status
  region. Do not add a second live region.
- The page is served under `script-src 'self'` with no `unsafe-inline`, so no inline
  `<script>` and no `style=` attribute. `tests/integration/test_dashboard_api.py` enforces
  this; a CSP violation is a silent failure in the browser, so the test is the guard.
- Accessibility and reflow gates are listed in `docs/dashboard.md`. They were run in a real
  browser against a live server; they are not part of `pytest`.

## Offline development

Everything except the `openai_compatible` and `anthropic` adapters works with no network:
fake providers, SQLite, the CLI, the smoke script, and all tests. Adapter tests use
`httpx.MockTransport`, so they need no network either.
