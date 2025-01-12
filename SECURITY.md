# Security Policy

## Reporting a vulnerability

Do **not** open a public issue for a vulnerability. Report it privately through GitHub's
"Report a vulnerability" advisory form on this repository, or by email to the maintainers
listed in `pyproject.toml`.

Include: affected version or commit, a minimal reproduction, the configuration shape with
secrets removed, and the impact you believe it has (for example: "a prompt containing an
API key is written to the log", or "two concurrent requests both pass the budget gate").

You can expect an acknowledgement within 5 business days and an assessment within 10. If
the report is valid we will coordinate a fix and a disclosure date with you.

## Supported versions

The latest tagged release and `main` receive security fixes. This project is pre-1.0;
there are no long-term support branches.

## Security model in one page

The gateway is a proxy that holds provider credentials and decides whether money may be
spent. The properties it tries to guarantee:

| Property | Mechanism | Failure behavior |
| --- | --- | --- |
| Budget cannot be exceeded by concurrency | reservation inside one `BEGIN IMMEDIATE` transaction | reject with `503 budget_state_unavailable` when `server.fail_closed = true` |
| Provider keys never leave the server | `api_key_env` is read at startup; responses never echo it | provider errors are mapped to stable client errors |
| Project keys are not stored in plaintext | config holds SHA-256 digests, compared in constant time across all projects | invalid key → `401`, no project detail leaked |
| Prompts are not retained by default | only metadata is logged; prompt text requires `log_prompts = true` and is redacted first | ledger stores token counts, not message bodies |
| Secrets are not logged | `redaction.py` patterns apply to every logged value and, optionally, to forwarded text | pattern misses are possible; see limitations |
| A request is never charged twice | settlement is write-once per attempt, and reconciliation compares settled cost to attempt rows | `usage` exits `4` when totals disagree |

## Known limitations (read these before trusting the control)

- **Redaction is pattern-based.** It reduces accidental leakage; it does not detect
  arbitrary secrets, encoded payloads, or secrets split across messages.
- **Rate and concurrency limits are process-local.** Running two gateway processes against
  one database doubles the effective request rate. Budget enforcement is not affected.
- **Cost is estimated.** Prices come from configuration and token counts come from the
  provider. A wrong price table produces wrong numbers, not a security bug, but it does
  mean a budget can be set too low to reflect real spend.
- **The ledger is the only spend record.** `purge --force` deletes rows inside the current
  budget window and therefore resets in-period accounting. It is logged as an audit event.
- **No TLS termination.** Run behind a reverse proxy or bind to loopback. The server does
  not implement TLS.
- **No streaming.** Streaming responses are rejected, which keeps settlement exact.

## Hardening checklist for deployments

1. Replace the demo key hashes (`cost-firewall hash-key`).
2. Bind to `127.0.0.1` or put a TLS-terminating proxy in front.
3. Keep `server.fail_closed = true`.
4. Keep `server.log_prompts = false`; enable it only for a bounded debugging window.
5. Run the gateway as an unprivileged user with write access only to the ledger directory.
6. Back up the ledger file with SQLite's backup mechanism, not by copying a live WAL.
7. Rotate provider keys by restarting with a new environment value; keys are read once at
   startup.
