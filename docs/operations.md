# Operations

Everything here assumes a single gateway process (or several processes sharing one SQLite
file) and a filesystem you control. There is no external service to operate.

## Configuration reference

Configuration is strict TOML: unknown keys are rejected, and a bad file is a startup failure
with exit code `2`. Run `cost-firewall check-config --config config.toml` after editing.

### `[server]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `host` | string | `127.0.0.1` | Bind address. |
| `port` | int | `8787` | Bind port. |
| `max_body_bytes` | int | `1048576` | Request body cap; larger bodies are `413`. |
| `request_timeout_seconds` | float | `30.0` | Per-attempt upstream timeout; also caps the provider's own setting. |
| `log_prompts` | bool | `false` | Log prompt text at DEBUG. Redacted first, but still: leave it off. |
| `log_level` | string | `INFO` | Python/uvicorn log level. |
| `fail_closed` | bool | `true` | When the ledger is unavailable, reject (`503`) instead of forwarding unaccounted. |

### `[database]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `path` | string | `gateway.db` | SQLite file. `:memory:` is supported for tests only. |

### `[limits]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `max_messages` | int | `64` | Maximum messages per request. |
| `max_input_chars` | int | `200000` | Sum of message content length. |
| `default_max_output_tokens` | int | `512` | Used when the client omits `max_tokens`. |
| `max_output_tokens` | int | `4096` | Hard ceiling on `max_tokens`. |
| `reservation_ttl_seconds` | int | `120` | Expiry for an unsettled reservation. |

### `[redaction]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `redact_before_forward` | bool | `false` | Rewrite prompts before sending upstream. Changes what the model sees. |
| `[[redaction.patterns]]` | table | built-in set | `name` + `regex`; replaces the built-in list when present. |

The built-in patterns cover common key shapes (OpenAI, Anthropic, GitHub, AWS), JWTs, bearer
tokens, PEM private keys, emails, grouped card numbers, and `password = …` assignments.
They are narrow on purpose: a false positive silently changes a prompt.

### `[[projects]]`

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | `[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}`; appears in every metric label. |
| `name` | string | no | Display name; defaults to `id`. |
| `api_keys` | array | yes | SHA-256 hex digests (optionally `sha256:` prefixed). Plaintext is rejected. |
| `monthly_budget_usd` | number | yes | Monthly cap, UTC month boundary. Minimum `$0.000001`. |
| `daily_budget_usd` | number | no | Daily cap, UTC day boundary. Omit for no daily cap. |
| `per_request_max_usd` | number | yes | Largest reservation a single request may take. |
| `requests_per_minute` | int | yes | Sliding-window limit, process-local. |
| `max_concurrency` | int | yes | In-flight cap, process-local; over-limit requests are rejected. |
| `allowed_models` | array | no | Route models this project may call. Empty means all routes. |
| `retention_days` | int | no | Default retention for `purge`; `30` if omitted. |

Generate digests with `cost-firewall hash-key <key>` (or `echo -n <key> | sha256sum`).

### `[[providers]]`

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | Referenced by route targets. |
| `type` | string | yes | `fake`, `openai_compatible`, or `anthropic`. |
| `base_url` | string | for real providers | e.g. `https://api.openai.com/v1`, `http://127.0.0.1:11434/v1`. |
| `api_key_env` | string | no | Environment variable read **once at startup** for the provider key. |
| `timeout_seconds` | float | no | Provider-specific timeout; the effective timeout is the smaller of this and `server.request_timeout_seconds`. |
| `latency_ms` | int | no | `fake` only: simulated latency. |
| `fail_first` | int | no | `fake` only: fail this many calls before succeeding. |
| `fail_status` | int | no | `fake` only: HTTP status used for simulated failures. |
| `fail_class` | string | no | `fake` only: `transient` or `permanent`. |
| `response_text` | string | no | `fake` only: fixed completion text. |

### `[[routes]]` and `[[routes.targets]]`

| Key | Type | Meaning |
| --- | --- | --- |
| `model` | string | Client-facing model name. |
| `targets[].provider` | string | Provider id. |
| `targets[].model` | string | Model name sent upstream. |
| `targets[].input_usd_per_1m` | number | Price per 1M input tokens. `0` is allowed. |
| `targets[].output_usd_per_1m` | number | Price per 1M output tokens. `0` is allowed. |

Targets are tried in order. The first one is the primary.

## Environment variables

| Variable | Used by | Meaning |
| --- | --- | --- |
| `COST_FIREWALL_API_KEY` | `dry-run`, `loadtest` | Project key for CLI calls; preferred over `--api-key`. |
| `COST_FIREWALL_CONFIG` | container entrypoint | Path to the config file. |
| *value of* `providers[].api_key_env` | gateway | Provider credential. Read once at startup; rotate by restarting. |

The server does not read the project API key from the environment; only the CLI does.

## Deploy

### Local process

```bash
cost-firewall check-config --config config.toml
cost-firewall serve --config config.toml
```

Run it as an unprivileged user whose only write access is the ledger directory. Put a TLS
terminator (Caddy, nginx, an ingress) in front if it is reachable from anywhere but loopback.

Minimal systemd unit:

```ini
[Unit]
Description=AI Gateway Cost Firewall
After=network-online.target

[Service]
User=costfirewall
WorkingDirectory=/var/lib/cost-firewall
ExecStart=/opt/cost-firewall/.venv/bin/cost-firewall serve --config /etc/cost-firewall/config.toml
# Provider credentials, readable only by this user:
EnvironmentFile=/etc/cost-firewall/provider.env
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/cost-firewall

[Install]
WantedBy=multi-user.target
```

The `EnvironmentFile` holds the variables named by `providers[].api_key_env`
(`OPENAI_API_KEY=…`), not the project keys.

### Container

```bash
docker build -t cost-firewall .
docker run -d --name cost-firewall -p 8787:8787 \
  -v cost-firewall-data:/data \
  -v "$PWD/config.toml:/config/config.toml:ro" \
  -e COST_FIREWALL_CONFIG=/config/config.toml \
  -e OPENAI_API_KEY \
  cost-firewall
```

The image stores the ledger at `/data/gateway.db`, runs as `app`, and healthchecks `/healthz`.

### Several processes, one database

It works: budget enforcement is transactional in SQLite. Two caveats:

- rate and concurrency limits are per process, so effective limits multiply;
- write throughput is bounded by one SQLite writer. Keep the number of processes small.

## Backup and restore

The ledger is the spend record. Back it up with SQLite's online backup, not a file copy of a
live WAL:

```bash
sqlite3 /data/gateway.db ".backup '/backup/gateway-$(date +%F).db'"
```

Restore by stopping the gateway, replacing the file (and removing stale `-wal`/`-shm`), then
starting again. A restored ledger restores the budget position with it — that is the point.

## Retention and purging

`cost-firewall purge` deletes requests, attempts, and audit events older than the retention
window, per project (`retention_days` or `--retention-days`).

Rows inside the **current UTC month** are protected unless `--force` is passed, because
deleting them would silently return budget to the project. A purge is recorded as an
`audit_events` row with the cutoff and the counts.

Schedule it daily, after the backup:

```cron
15 3 * * * cost-firewall purge --config /etc/cost-firewall/config.toml --project alpha >> /var/log/cost-firewall-purge.log 2>&1
```

`purge` requires `--project` when the config defines more than one project, so schedule one
line per project (or one line per project with its own retention override).

## Monitoring

Poll `/healthz` for liveness and `/metrics` for counters. Worth alerting on:

| Signal | Why |
| --- | --- |
| `gateway_rejections_total{reason="monthly_budget_exhausted"}` rising | A project is out of budget; the application is getting `429`s. |
| `gateway_rejections_total{reason="budget_state_unavailable"}` > 0 | The ledger is failing; requests are being refused. Investigate the disk. |
| `gateway_attempts_total{outcome=~"error_.*"}` ratio | Provider health, independent of client retries. |
| `gateway_cost_micros_total` slope | Burn rate versus budget. |
| `/healthz` non-200 | Process or database problem. |
| `usage` report `reconciled: false` | A settled charge disagrees with its attempts. Treat as a bug. |

`usage` and `loadtest` exit `4` when reconciliation fails, so they can be used as CI or cron
gates.

## Failure modes

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `503 budget_state_unavailable` | Ledger file unreadable, disk full, permissions, or `database is locked` beyond 30s | Check disk and permissions; the gateway deliberately refuses to spend unaccounted. |
| `429 budget_exhausted` with budget left in the report | Reservations from a crashed process not yet expired | Wait `reservation_ttl_seconds`, or inspect with `trace`. |
| `502 upstream_error` on every call | Provider key missing, wrong `base_url`, or a permanent `4xx` | Check the provider row in `trace`; the message includes the class, not the key. |
| `504 upstream_timeout` repeatedly | Timeout too low for the model, or the provider is slow | Raise `server.request_timeout_seconds` deliberately; the reservation is released meanwhile. |
| `401 invalid_api_key` for a working key | Key rotated but config still holds the old digest | `hash-key` the new key and restart. |
| Spend lower than the provider invoice | Timeouts without usage evidence are released, not charged | Reconcile against the provider's usage export; this is a known under-count. |

## Measured performance

Measured on the development machine (Windows, Python 3.12, one gateway process, SQLite on
local disk). Method: `cost-firewall loadtest` in-process for the ledger path, and an HTTP
load script against a running server for the transport path. Every run also asserts that
settled spend never exceeded the cap and that every settled request reconciled.

| Path | Requests | Concurrency | Result |
| --- | --- | --- | --- |
| In-process gateway + ledger | 400 | 16 | ~2.0 s, ~196 req/s, all reconciled, no overspend |
| HTTP, `POST /v1/chat/completions` | 500 | 32 | all `200`, p50 ~0.7 s, p95 ~2.3 s |
| HTTP baseline, trivial FastAPI app, same box | 200 | 32 | ~24 req/s |

The last row is the control: on this machine the HTTP harness itself saturates around
24–32 req/s at concurrency 32, so the gateway's HTTP numbers measure the harness, not the
gateway. The in-process number is the meaningful throughput figure for the budget path.
Re-measure on your own hardware before publishing capacity claims; the repository does not
claim a production throughput target.

Correctness under concurrency is the property the tests assert: 400 concurrent requests
against a budget that only fits a subset, with zero overspend and zero unreconciled rows.

## Upgrades

The ledger records `schema_version`. A database from a newer schema is refused at startup
with an explicit error rather than being written with a mismatched layout. Take a backup
before upgrading; this project does not ship automated migrations yet.
