# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, a minor version may
change the API; every change to a documented contract is listed.

## [Unreleased]

### Added

- Operator dashboard at `GET /dashboard/`: a budget meter with committed, reserved, and
  remaining amounts; usage KPIs; observed provider outcomes; recent refusals; a searchable
  and filterable request ledger with a per-request attempt trace; a routing simulator that
  calls `POST /v1/gateway/dry-run`; a read-only policy view; and CSV/JSON export.
- Read endpoints the dashboard consumes: `GET /v1/gateway/requests` (list),
  `GET /v1/gateway/whoami`, `GET /v1/gateway/policy`, `GET /v1/gateway/providers`, and
  `GET /v1/gateway/usage.csv`.
- The dashboard is served with a strict content security policy
  (`default-src 'none'; script-src 'self'`, no inline script or style), `nosniff`, and
  `referrer-policy: no-referrer`. API responses are unchanged.
- The control room renders the daily cap as its own compact meter next to the monthly one,
  and states which cap is refusing traffic. Previously a project at 0.26% of its monthly
  budget could refuse every request with `daily_budget_exhausted` while the page showed no
  reason.
- `docs/dashboard.md` (views, states, accessibility evidence, limitations) and
  `docs/design-tokens.md` (token register with measured contrast for every pair).

### Fixed

- A rejected or unreachable key now clears every project panel. Previously the last
  project's numbers stayed on screen under the new key's error message, which attributed
  one tenant's spend to another credential.
- An error panel's Retry button now works after a failed load; the message was previously
  cloned per panel, which copied the markup but not the click listener.
- Simulator validation no longer references an out-of-scope variable, so an empty model or
  a zero token count is reported on the field instead of failing silently.
- Controls are 44px high, matching the touch-target minimum; the skip link, text fields, and
  selects were 40-41px.
- Wide tables scroll in a focusable, named region, so keyboard users can reach columns that
  overflow at narrow widths; the tab stop exists only while the table actually overflows.
- Arrow-key navigation on the tab strip scrolls the selected tab into view on narrow screens.
- Budget meter fills no longer render at zero width. The dashboard ships `style-src 'self'`,
  which discards inline style *attributes*; dynamically sized bars are now applied through
  the CSSOM, and the decision path legend uses a class instead of an inline declaration.

## [0.1.0] - 2026-10-20

First working release. Scope: local-first gateway, deterministic fake providers, SQLite
ledger, budgets, routing, redaction, metrics, CLI, container image.

### Added

- OpenAI-compatible `POST /v1/chat/completions` (non-streaming) and `GET /v1/models`.
- Gateway extension endpoints: `POST /v1/gateway/dry-run`, `GET /v1/gateway/usage`,
  `GET /v1/gateway/budget`, `GET /v1/gateway/requests/{request_id}`.
- Reserve-then-settle budget accounting in integer micro-USD, enforced inside one
  `BEGIN IMMEDIATE` SQLite transaction per request, with per-request, daily, and monthly caps.
- Ordered multi-target routing with classified fallback: transient errors fall back for
  idempotent requests, permanent errors never do.
- Provider adapters: `fake` (deterministic, offline), `openai_compatible` (OpenAI, Ollama,
  vLLM, LM Studio), and `anthropic`.
- Per-project API keys stored as SHA-256 digests and compared in constant time.
- Per-project sliding-window rate limit and in-flight concurrency cap.
- Redaction of secrets in logs, with opt-in redaction of forwarded prompts.
- SQLite ledger with attempts, audit events, reservation TTL, retention purge, and a
  reconciliation check between settled cost and attempt rows.
- Prometheus text metrics at `/metrics`; `GET /healthz` reports ledger liveness.
- CLI: `serve`, `dry-run`, `usage`, `budget`, `trace`, `purge`, `hash-key`, `check-config`,
  `loadtest`.
- `scripts/smoke.py`: end-to-end HTTP check of fallback, permanent failure, budget
  exhaustion, reconciliation, trace privacy, redaction, metrics, auth, and allowlists.
- `scripts/http_bench.py`: concurrent HTTP probe that reports wall time, throughput, and
  p50/p95 latency, and asserts reconciliation and no overspend.
- `loadtest` reports wall time and throughput, and uses a throwaway ledger by default
  (`--database` to keep one) so a benchmark never consumes the deployment's budget.
- Upstream response bodies are bounded at 4 MiB; an oversized body is a permanent
  provider failure rather than unbounded memory growth.
- Cross-process budget test: fourteen separate Python processes race for a budget that
  fits ten reservations; exactly ten are granted and the ledger reconciles.
- Container image with an unprivileged user and a `HEALTHCHECK`.
- Documentation: architecture, API, operations, threat model, privacy, development, ADRs.

### Known limitations

- No streaming responses; `stream: true` is rejected with `400`.
- Rate and concurrency limits are process-local; budgets are not.
- Cost is an estimate from a configured price table and provider-reported token counts.
- Token counts for the reservation are estimated with a local heuristic, not a tokenizer.
- Redaction is pattern-based and can miss secrets.
- SQLite is the single source of truth; there is no multi-node coordination.
