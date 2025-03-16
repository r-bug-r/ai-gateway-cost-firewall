# 0002 — Fake providers are the default, not a test double

- **Status:** accepted
- **Date:** 2026-10-20

## Context

A cost firewall is only trustworthy if its failure paths are exercised: fallback, permanent
error, timeout, overage, budget exhaustion. Those paths are hard to produce against a real
provider and impossible to produce deterministically without spending money.

The usual approach is to keep fakes in the test suite and ship only real adapters. That
leaves the shipped configuration unable to demonstrate the product without credentials, and
makes the first-run experience "add a key and hope".

## Decision

`type = "fake"` is a first-class provider in the production code path, with knobs for
latency, failure count, failure status, and failure class. The shipped
`config.example.toml` uses only fake providers and is the documented quickstart.

## Consequences

- The quickstart costs $0 and needs no network. A new user can see fallback, cost
  accounting, and a rejected over-budget request within a minute.
- `scripts/smoke.py` can assert transient-versus-permanent behavior over real HTTP without
  stubbing the transport.
- The fake is deterministic: token counts come from the same estimator the reservation uses,
  and the response text is derived from the prompt, so cost assertions are stable.
- Risk: someone might deploy the example config. It is loudly labeled as demo-only, uses
  demo key hashes, and has a $0.05 cap. `check-config` prints every route so a real
  deployment is visibly different.
- Risk: the fake's success path is simpler than a real provider's. Adapter tests with
  recorded bodies cover the real parsing paths, so the fake is not the only evidence.
