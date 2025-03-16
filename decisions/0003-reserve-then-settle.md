# 0003 — Reserve then settle, with the reservation as a hard ceiling

- **Status:** accepted
- **Date:** 2026-10-20

## Context

A proxy learns the true cost of a request only after the provider responds. Charging after
the fact means the budget can be exceeded by any burst that starts while headroom still
looks sufficient. Charging before the fact means over-charging requests that produce fewer
tokens than requested.

Options:

1. **Post-hoc accounting.** Record cost after the call; enforce budgets on the running
   total. Simple, and wrong under concurrency: N in-flight requests each pass the check.
2. **Reserve the exact expected cost.** Tighter, but the input token count is an estimate
   and the output count is unknown until completion, so it can be exceeded.
3. **Reserve the worst case, settle at actual.** Worst case is bounded: output cannot exceed
   `max_tokens`, and input is bounded by `limits.max_input_chars`.

## Decision

Reserve `estimate_input_tokens × input_price + max_tokens × output_price` before forwarding,
inside the same transaction that checks the budget. On success, settle at the provider's
reported usage. On failure, release. On timeout with no usage evidence, release and record
`upstream_timeout`.

## Consequences

- **No overspend by construction**, as long as the estimator does not under-count input. It
  is a heuristic, so the reservation can be slightly low; settlement is clamped by the
  reservation and an overage is written to `audit_events` rather than silently exceeding the
  cap. The smoke test asserts settled spend never exceeds the budget.
- Requests may be rejected while headroom exists, because the reservation is the worst case.
  This is the intended direction: refuse before spending, not after.
- A crashed process leaves a reservation until `reservation_ttl_seconds` expires. Recovery
  is automatic; the cost is temporary false rejection.
- Timeouts are ambiguous — the provider may have billed. Releasing under-counts spend. The
  alternative (charging the reservation) would over-charge users for failures they did not
  receive. Under-counting is visible in reconciliation against provider invoices; over-charging
  is not.
- Settlement is write-once. A duplicate settlement attempt is refused, so retries cannot
  double-charge.
