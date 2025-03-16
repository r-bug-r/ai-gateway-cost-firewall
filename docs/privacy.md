# Privacy

## What the gateway stores

| Data | Where | Default retention |
| --- | --- | --- |
| Project id, route model, request id | `requests` | `projects[].retention_days` (default 30) |
| Reservation, settled cost, status, timestamps | `requests` | same |
| Provider, upstream model, status, error class, HTTP status | `attempts` | same |
| Input/output token counts, latency | `attempts` | same |
| Operator events (fail-open, overage, purge) | `audit_events` | same |
| Metrics counters (aggregated, no ids) | process memory | until restart |

## What it does not store

- **Prompt or completion text.** Not in SQLite, not in the trace endpoint, not in metrics.
- **API keys.** Project keys exist only as SHA-256 digests in config. Provider keys exist
  only in the process environment.
- **Client IP addresses or user agents.** The access log records method, path, status, and
  latency only.
- **Request headers.** Except `Authorization`, which is hashed and discarded.

## Logs

At `log_level = INFO` the gateway logs one line per HTTP request:

```
POST /v1/chat/completions -> 200 in 12ms
```

With `log_prompts = false` (the default), DEBUG logs contain message count and total input
characters — not text. With `log_prompts = true`, message contents are logged at DEBUG after
passing through the redactor. Enabling it is a deliberate choice to retain prompt text in
whatever collects stdout; treat those logs as sensitive.

Redaction patterns cover common credential shapes and also scrub email addresses, grouped
card numbers, and `password = …`-style assignments. Redaction is applied to every value the
logger receives, including upstream error messages.

## Forwarding to providers

Prompts are sent to the provider selected by the route, exactly as received, unless
`redaction.redact_before_forward = true`. In that case matching patterns are replaced with
`[REDACTED:<name>]` before the request leaves the process. This changes what the model sees;
it is off by default.

What a provider does with the prompt is governed by that provider's terms, not by this
gateway. The gateway cannot make a third-party provider forget a request.

## Deleting data

`cost-firewall purge --config config.toml --project <id>` removes requests, attempts, and
audit events older than the retention window. Rows inside the current UTC month are kept
unless `--force` is passed, because they carry the budget position.

To delete everything, stop the gateway and delete the ledger file. That also resets budgets;
do it only when you intend to.

## Data subject requests

Because prompts are not stored, a data-subject request usually has nothing to return from the
gateway. What the gateway can provide is the metadata trail for a known `request_id`
(`cost-firewall trace <id>`) or an aggregate for a project (`usage`). There is no mapping
from a person to a request id, and the gateway does not create one.

## Backups

Backups contain the same metadata as the live ledger. Protect them at the same level, and
apply the same retention: a backup older than your retention window still contains old
request ids and timestamps.

## Children and special categories

The gateway does not know what a prompt contains. If your application sends special-category
data, the control is on your side: do not send it, or enable forwarding redaction for the
patterns that matter to you. Pattern-based redaction is not a compliance mechanism.
