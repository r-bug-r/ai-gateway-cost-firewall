# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, a minor version may
change the API; every change to a documented contract is listed.

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
- Container image with an unprivileged user and a `HEALTHCHECK`.
- Documentation: architecture, API, operations, threat model, privacy, development, ADRs.

### Known limitations

- No streaming responses; `stream: true` is rejected with `400`.
- Rate and concurrency limits are process-local; budgets are not.
- Cost is an estimate from a configured price table and provider-reported token counts.
- Token counts for the reservation are estimated with a local heuristic, not a tokenizer.
- Redaction is pattern-based and can miss secrets.
- SQLite is the single source of truth; there is no multi-node coordination.
