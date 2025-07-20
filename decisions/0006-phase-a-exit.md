# 0006 — Phase A exit: implementation complete, entering UI/UX

- **Status:** accepted
- **Date:** 2026-10-20
- **Scope:** Phase A (implementation) for `ai-gateway-cost-firewall`

## Decision

`start` and `narrow`, then Phase A complete. Entering Phase B.

- **start** — the target was eligible. The repository had plans and no code; the plan's
  $0 path (fake providers, SQLite on localhost, no credentials, no network) needs no
  external prerequisite, so there was nothing to defer on cost or consent grounds.
- **narrow** — the "concrete, evidenced routing/budget use case" entry condition is met by
  the plan's own artifacts rather than by a third-party pilot: the cost-control example
  (`examples/cost-control/`) is a working hard-cap deployment, and the portfolio plan's
  evaluation-runner integration is the named downstream use case. No live provider was
  called; all evidence below is offline and deterministic.

## What was built

| Plan item | Where | Verified by |
| --- | --- | --- |
| Documented OpenAI-compatible subset | `models.py`, `docs/api.md` | `tests/unit/test_models.py`, smoke scenario |
| Adapters: fake, OpenAI-compatible, Anthropic, local | `providers.py` | `tests/unit/test_providers.py` (15 tests) |
| Per-request / daily / monthly budgets | `ledger.py`, `gateway.py` | `tests/unit/test_ledger.py`, `test_gateway.py`, `tests/integration/test_multiprocess_budget.py` |
| Rate and concurrency limits | `limits.py` | `tests/unit/test_limits.py` |
| Redaction of logs, opt-in forwarding | `redaction.py` | `tests/unit/test_redaction.py`, smoke |
| Provider/model/tokens/latency/cost/outcome record | `ledger.py` (`requests`, `attempts`) | `test_ledger.py`, smoke trace check |
| Fallback only for idempotent + transient | `router.py`, `providers._status_class` | `test_gateway.py`, smoke |
| Dry-run decision and usage report | `gateway.plan`, `gateway.usage` | `test_gateway.py`, `tests/integration/test_http.py` |
| SQLite and container deployment | `ledger.py`, `Dockerfile`, `.github/workflows/ci.yml` | SQLite exercised everywhere; container **not** run locally |
| Fail closed on unreadable budget state | `gateway._reserve`, `ledger` errors | `test_gateway.py::test_fail_closed` |
| CLI report (dashboard is Phase B) | `cli.py` | `tests/integration/test_cli.py` |
| Load test + documented result | `loadtest.py`, `scripts/http_bench.py`, `docs/operations.md` | runs recorded below |

## Commands run and observed outcomes

All on the development machine (Windows, Python 3.12), 2026-10-20.

```text
python -m pytest tests -q                     124 passed, 1 warning
python -m ruff check src tests scripts examples  All checks passed
python -m ruff format --check ...              27 files already formatted
python -m mypy                                 Success: no issues found in 26 source files
python scripts/smoke.py                        SMOKE PASSED (22 checks)
cost-firewall check-config --config config.example.toml
                                               config ok: 2 projects, 3 providers, 4 routes
cost-firewall check-config --config config.live.example.toml
                                               config ok: 1 projects, 4 providers, 3 routes
```

The same five checks were re-run in a **fresh clone** (`git clone` into a temp directory,
new venv, `pip install -e ".[dev]"`): 123 passed, lint clean, mypy clean, `SMOKE PASSED`,
`config ok`.

Load and HTTP measurements (each run uses a throwaway ledger):

```text
cost-firewall loadtest --concurrency 16 --requests 400
  duration_ms 1251 / 6007 / 1152  ->  320 / 67 / 347 req/s, reconciled, within_budget
python scripts/http_bench.py --requests 400 --concurrency 16
  5.6s / 6.9s / 5.7s  ->  71 / 58 / 70 req/s, p50 42-70ms, p95 0.7-0.8s, 400x200
```

Cross-process budget enforcement, observed (fourteen separate Python processes, one
database file, room for ten reservations):

```text
tests/integration/test_multiprocess_budget.py   1 passed
  grants 10, denials 4 x monthly_budget_exhausted,
  month_committed_micros 1000 <= 1000, reconciled True
```

`examples/cost-control/demo.py` output, observed:

```text
allowed 89, settled $0.014952, remaining $0.235048, reconciled True,
attempt rows 89 for 89 requests, CAP HELD
```

The refusal was `budget_exhausted (daily_budget_exhausted)` — the $0.02 daily cap, not the
monthly cap, which is the intended precedence.

## Known limitations at the gate

1. **Container not executed locally.** No Docker daemon on this machine. The image is
   authored and CI has a job that builds it and polls `/healthz`; that job has not run yet
   because there is no remote. Stated as unverified, not as passing.
2. **Streaming unsupported** by design (ADR 0005), rejected with `400`.
3. **Traffic limits are process-local** (ADR 0004), so they multiply with process count.
4. **Cost is an estimate** from a configured price table; no invoice reconciliation has been
   performed against a real provider, because no live provider was called.
5. **No live provider call was made.** Every number above comes from fake providers.
6. **Windows-only measurement.** Linux numbers are unknown; CI runs the tests there but
   nothing records performance.

## Evidence required before claiming more

- A CI run on a remote (container build + health probe + Windows/Linux matrix).
- One reconciled billing period against a real provider before any claim about estimate
  accuracy.
- A pilot user's before/after numbers before any adoption claim.

## Phase B entry

The Phase A gate is satisfied for every in-scope item with the six limitations above
recorded. Phase B (UI/UX) may begin: read `ui-ux-plan.md`, implement the dashboard against
the real gateway API, and verify it in a browser.
