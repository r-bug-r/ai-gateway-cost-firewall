# API

Base URL is whatever `server.host` / `server.port` resolves to. All endpoints except
`/healthz` and `/metrics` require `Authorization: Bearer <project key>`.

Errors always use the OpenAI envelope:

```json
{
  "error": {
    "message": "Monthly budget exhausted for project 'alpha'.",
    "type": "insufficient_quota",
    "param": null,
    "code": "budget_exhausted"
  }
}
```

## `POST /v1/chat/completions`

### Request

```json
{
  "model": "gpt-4o-mini",
  "messages": [
    {"role": "system", "content": "You are terse."},
    {"role": "user", "content": "Explain WAL in one sentence."}
  ],
  "max_tokens": 64,
  "temperature": 0.2,
  "idempotent": true
}
```

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `model` | string | yes | Must be a configured route and allowed for the project. |
| `messages` | array | yes | 1..`limits.max_messages`; roles `system`, `user`, `assistant`; string content. |
| `max_tokens` | integer | no | Default `limits.default_max_output_tokens`, max `limits.max_output_tokens`. |
| `temperature` | number | no | `0..2`. |
| `stream` | boolean | no | Must be absent or `false`. `true` → `400` with `param: "stream"`. |
| `idempotent` | boolean | no | Default `true`. `false` forbids fallback for this request. |

Unknown fields are rejected with `400 invalid_request`. They are not silently ignored,
because a dropped field would change what the caller is billed for.

Total input characters are capped by `limits.max_input_chars`.

### Response

Standard chat completion fields plus a `gateway` evidence block:

```json
{
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "created": 1792497600,
  "model": "gpt-4o-mini",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "…"}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 18, "completion_tokens": 12, "total_tokens": 30},
  "gateway": {
    "request_id": "req_…",
    "provider": "fake-backup",
    "provider_model": "gpt-4o-mini",
    "provider_request_id": "…",
    "estimated_cost_micros": 8,
    "estimated_cost_usd": "$0.000008",
    "cost_basis": "configured_price_table",
    "attempts": [
      {"attempt": 1, "provider": "fake-primary", "model": "gpt-4o-mini",
       "status": "transient", "error_class": "transient", "cost_micros": 0, "latency_ms": 3},
      {"attempt": 2, "provider": "fake-backup", "model": "gpt-4o-mini",
       "status": "success", "error_class": null, "cost_micros": 8, "latency_ms": 5}
    ]
  }
}
```

`estimated_cost_micros` is an integer number of micro-USD (1e-6 USD). `cost_basis` is always
`configured_price_table`: the number is derived from configuration, not from an invoice.

## `GET /v1/models`

Lists route models visible to the project after `allowed_models` filtering:

```json
{"object": "list", "data": [{"id": "gpt-4o-mini", "object": "model"}]}
```

## `POST /v1/gateway/dry-run`

Same request body as chat completions. Returns the routing and budget decision without
reserving budget or calling a provider:

```json
{
  "object": "gateway.routing_decision",
  "project": "alpha",
  "model": "gpt-4o-mini",
  "targets": [
    {"provider": "fake-primary", "model": "gpt-4o-mini",
     "estimated_cost_micros": 20, "estimated_cost_usd": "$0.000020"},
    {"provider": "fake-backup", "model": "gpt-4o-mini",
     "estimated_cost_micros": 20, "estimated_cost_usd": "$0.000020"}
  ],
  "reservation_micros": 20,
  "reservation_usd": "$0.000020",
  "estimated_input_tokens": 5,
  "max_output_tokens": 32,
  "fallback_allowed": true,
  "fallback_reason": "fallback permitted for classified transient failures only",
  "allowed": true,
  "blocking_reason": null,
  "budget": {"monthly_budget_micros": 50000, "day_remaining_micros": 20000, "…": "…"},
  "per_request_max_usd": "$0.005000",
  "note": "Cost figures are estimates derived from configured prices, not invoices."
}
```

`allowed: false` sets `blocking_reason` to the same `code` the live endpoint would return.
`fallback_allowed` is `false` when the request is non-idempotent or the route has one target.

## `GET /v1/gateway/usage?project=<id>&days=<n>`

Aggregates the ledger. `reconciled` is `true` only when every settled request's charge equals
the sum of its successful attempt costs; `mismatched_request_ids` names the offenders.

```json
{
  "project_id": "alpha",
  "requests": 4,
  "settled_requests": 3,
  "failed_requests": 1,
  "released_requests": 0,
  "attempts": 7,
  "successful_attempts": 3,
  "input_tokens": 72,
  "output_tokens": 32,
  "settled_micros": 32,
  "settled_usd": "$0.000032",
  "avg_latency_ms": 5,
  "reconciled": true,
  "mismatched_request_ids": [],
  "since_ms": null
}
```

## `GET /v1/gateway/budget?project=<id>`

Returns the exact numbers the gate uses. `*_remaining_micros` subtract settled **and**
reserved amounts, so it is the amount a new request may reserve right now.

```json
{
  "project": "alpha",
  "monthly_budget_micros": 50000,
  "daily_budget_micros": 20000,
  "month_settled_micros": 32,
  "month_reserved_micros": 0,
  "month_remaining_micros": 49968,
  "day_settled_micros": 32,
  "day_reserved_micros": 0,
  "day_remaining_micros": 19968,
  "month_committed_micros": 32,
  "monthly_budget_usd": "$0.050000",
  "daily_budget_usd": "$0.020000",
  "month_settled_usd": "$0.000032",
  "month_reserved_usd": "$0.000000",
  "month_remaining_usd": "$0.049968",
  "day_settled_usd": "$0.000032",
  "day_reserved_usd": "$0.000000",
  "day_remaining_usd": "$0.019968"
}
```

## `GET /v1/gateway/requests?project=<id>&limit=<n>&days=<n>`

Newest-first request metadata for the dashboard ledger table: `request_id`, `route_model`,
`status`, `reserved_usd`, `settled_usd`, `attempts`, timestamps, `error_code`, `decision`, and
`last_provider`. `limit` is 1–500. Prompt and completion text are absent by construction,
because the ledger never stores them.

## `GET /v1/gateway/requests/{request_id}`

Returns the request row plus every attempt, with `settled_usd` and per-attempt `cost_usd`
added, plus the audit trail. Unknown ids are `404 request_not_found`. Prompt text is never
stored, so it cannot be returned.

`outcome` is `settled`, `failed`, `released`, or `blocked`. `blocked` means the firewall
refused the request before forwarding it; such a row has no attempts and always carries an
`error_code` plus a `request_rejected` audit event, so the reason is visible in one place.

## `GET /v1/gateway/whoami`

No query parameters. Returns the project the presented key resolves to and the projects that
share that credential:

```json
{"project": "alpha", "name": "Demo alpha", "readable_projects": ["alpha", "beta"]}
```

`readable_projects` is computed from the configured key digests, so a dashboard can only
offer projects the key can actually read.

## `GET /v1/gateway/policy?project=<id>`

The effective policy for one project, with no credential material: budgets, model allowlist,
rate and concurrency limits, retention, redaction mode, and the route table filtered to the
models this project may use.

## `GET /v1/gateway/providers?project=<id>&days=<n>`

Observed provider outcomes in the window, one row per provider that saw traffic:
`attempts`, `successes`, `failures`, `last_error_class`, and `last_attempt_ms`. A provider
with no traffic has no row; these are observations, not synthetic probes.

## `GET /v1/gateway/usage.csv?project=<id>&days=<n>`

The usage report as one CSV row plus a header, served with
`content-disposition: attachment; filename="usage-<project>.csv"`. Same authorization and
window rules as the JSON report.

## `GET /healthz`

No auth. `200 {"status": "ok", "ledger": "ok"}` or
`503 {"status": "degraded", "ledger": "unavailable"}` when a trivial ledger read fails.

## `GET /metrics`

Prometheus text. Counters are process-local and reset on restart; the ledger is the durable
record.

| Metric | Labels |
| --- | --- |
| `gateway_requests_total` | `project`, `model`, `outcome` |
| `gateway_attempts_total` | `provider`, `model`, `outcome` |
| `gateway_tokens_total` | `project`, `direction` |
| `gateway_cost_micros_total` | `project`, `provider` |
| `gateway_rejections_total` | `reason` |
| `gateway_reservations_total` | `project`, `outcome` |

## Error codes

| HTTP | `code` | `type` | Meaning |
| --- | --- | --- | --- |
| 400 | `invalid_request` | `invalid_request_error` | Body shape, role, bounds, unknown field, `stream: true`. |
| 400 | `per_request_limit_exceeded` | `invalid_request_error` | Reservation exceeds `per_request_max_usd`. |
| 401 | `invalid_api_key` | `authentication_error` | Missing, malformed, or unknown key. |
| 404 | `model_not_found` | `invalid_request_error` | No route, or not allowed for the project. |
| 404 | `request_not_found` | `invalid_request_error` | Trace endpoint only. |
| 413 | `payload_too_large` | `invalid_request_error` | Body exceeds `server.max_body_bytes`. |
| 429 | `budget_exhausted` | `insufficient_quota` | Monthly or daily cap would be exceeded. |
| 429 | `rate_limited` | `rate_limit_error` | Sliding-window request limit; `Retry-After` is set. |
| 429 | `concurrency_limit` | `rate_limit_error` | In-flight cap; `Retry-After` is set. |
| 502 | `upstream_error` | `api_error` | All attempts failed permanently, or fallback was not allowed. |
| 503 | `budget_state_unavailable` | `api_error` | Ledger unreadable and `fail_closed = true`. |
| 504 | `upstream_timeout` | `timeout_error` | Provider timed out with no usage evidence; reservation released. |
| 500 | `internal_error` | `api_error` | Unhandled server fault. |

`429 rate_limited` sets `Retry-After` in whole seconds; `budget_exhausted` and
`concurrency_limit` do not, because retrying immediately is not the remedy.
