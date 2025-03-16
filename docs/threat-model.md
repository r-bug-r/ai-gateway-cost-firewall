# Threat Model

## Assets

1. **Provider credentials** — money can be spent with them.
2. **Project API keys** — access to the gateway and therefore to provider spend.
3. **Prompt and completion content** — may contain personal or secret data.
4. **The ledger** — the only durable record of spend; corrupting it corrupts budgets.
5. **Availability** — a gateway outage stops the calling application.

## Actors and trust boundaries

| Actor | Trust | Boundary |
| --- | --- | --- |
| Calling application | Holds a project key; untrusted input | HTTP request body, headers |
| Gateway process | Trusted code, least privilege expected | Reads config, provider env vars, ledger |
| Provider | Semi-trusted third party | Upstream HTTP; may lie about usage or return malformed data |
| Operator | Trusted | Config file, CLI, ledger file |
| Local filesystem | Trusted for integrity, not for confidentiality | Ledger and logs at rest |

The gateway is the enforcement point for spend. If it is bypassed, the application talks to
the provider directly and no budget applies. That escape hatch is deliberate; the
enforcement claim only covers traffic routed through the gateway.

## Threats and mitigations

| # | Threat | Mitigation | Residual risk |
| --- | --- | --- | --- |
| T1 | Concurrent requests each see budget headroom and overspend | Reservation inside one `BEGIN IMMEDIATE` transaction; the check and insert are atomic across processes | A direct provider call bypasses it |
| T2 | Caller sets a huge `max_tokens` to force large spend | `limits.max_output_tokens`, `per_request_max_usd`, and a worst-case reservation before forwarding | Operator sets limits too high |
| T3 | Caller retries a non-idempotent operation and causes duplicate side effects | `idempotent: false` disables fallback; chat completions are treated as idempotent by default | The caller must set the flag correctly |
| T4 | Prompt contains an API key or private key and ends up in logs | Only metadata is logged by default; `log_prompts` is off; every logged value passes through the redactor | Pattern misses; redaction is not a DLP system |
| T5 | Prompt content is retained in the ledger | The ledger stores token counts, ids, and cost — not message bodies; the trace endpoint cannot return prompts | An operator could enable debug logging and capture them |
| T6 | Project key theft | Keys stored only as SHA-256 digests; constant-time comparison across all projects; no key echo in responses or logs | A stolen key works until rotated; there is no per-key expiry |
| T7 | Provider credential leak | Keys read from environment at startup, never written to config, logs, or responses | Process environment is visible to privileged local users |
| T8 | Malicious provider response (huge usage, bad JSON) | Strict parsing, bounded body read, usage clamped to non-negative integers, settlement never exceeds the reservation | A provider that under-reports usage causes under-charging |
| T9 | Replay or duplication of a charge | Settlement is write-once per request; reconciliation compares settled cost with attempt rows | Ambiguous timeouts are released, so they under-count |
| T10 | Denial of service via large or many requests | Body cap, message cap, character cap, rate limit, concurrency limit | Limits are process-local and in-memory |
| T11 | Ledger tampering or deletion | File permissions are the control; `purge --force` is audited | Anyone with write access to the file can rewrite spend |
| T12 | Crash mid-request | Reservations expire after the TTL, so headroom is recovered; attempts are recorded as they happen | A crash after the provider call but before settlement under-counts that request |
| T13 | Budget state unreadable | `fail_closed = true` refuses to forward | Availability loss; this is the intended trade |
| T14 | Sensitive data sent to a third-party provider | `redact_before_forward` rewrites prompts before forwarding | Changes model behavior; still sends the rest |
| T15 | Operator misconfiguration | Strict TOML: unknown keys, plaintext keys, bad prices, and inconsistent limits are startup failures | A valid but wrong price table produces wrong numbers |

## Explicit non-goals

- Multi-tenant isolation at the network layer. Projects are a policy boundary inside one
  deployment, not separate security domains.
- Protection against a malicious operator. The operator controls config and the ledger.
- Prompt injection defense. The gateway forwards prompts; it does not interpret them.
- Model output safety. Content returned by a provider is passed through.

## Abuse cases worth testing before release

1. Two processes, one database, a budget that fits exactly one request → exactly one succeeds.
2. A request whose reservation fits but whose provider usage would not → settlement never
   exceeds the reservation, and an overage is audited rather than hidden.
3. `stream: true`, unknown fields, 100k-character prompts, 10k messages → each rejected with
   the documented code, none forwarded.
4. Ledger replaced with a directory, made read-only, or deleted mid-flight → `503`, no
   unaccounted forwarding.
5. Provider returns `200` with a malformed body → `502`, no fallback, no charge.
6. Provider returns `429` → fallback happens; provider returns `400` → no fallback.

These map to tests in `tests/` and to `scripts/smoke.py`; if you change one of these paths,
change the corresponding test in the same commit.
