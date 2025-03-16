# 0004 — Rate and concurrency limits are process-local

- **Status:** accepted
- **Date:** 2026-10-20

## Context

The gateway enforces two kinds of limits:

- **Spend limits** (monthly, daily, per request) — correctness-critical, must hold across
  processes and restarts.
- **Traffic limits** (requests per minute, in-flight requests) — protective, meant to keep a
  misbehaving client from saturating the gateway.

Traffic limits could also be made durable: count request starts in SQLite, or use a shared
counter. Both cost a write on the hot path for every request, including ones that are about
to be rejected.

## Decision

Traffic limits live in process memory (`limits.py`): a sliding window of timestamps per
project, and a bounded in-flight counter. Spend limits stay transactional in SQLite.

## Consequences

- Rejection is cheap: no disk write, no lock contention, no extra fsync.
- The effective rate limit multiplies with the number of processes sharing a database. Two
  processes at 60 rpm allow 120 rpm. This is documented in `docs/operations.md`,
  `docs/threat-model.md`, and `SECURITY.md` — it is a known property, not a bug report.
- Restarting the gateway resets the windows. A client that was being throttled gets a clean
  slate. Acceptable because the budget, not the rate limit, is the safety control.
- The concurrency cap is per process, so total in-flight requests across processes is
  `max_concurrency × processes`. Operators sizing a deployment must account for that.
- If durable traffic limits are ever needed, the ledger already has the request rows to
  derive them; the change would be a query in `reserve`, and this ADR would be superseded.
