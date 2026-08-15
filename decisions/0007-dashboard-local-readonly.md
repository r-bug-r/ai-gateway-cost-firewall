# 0007 — Dashboard: same-process, no build step, read-only policy

- **Status:** accepted
- **Date:** 2026-10-20
- **Scope:** Phase B (UI/UX) for `ai-gateway-cost-firewall`

## Context

Phase A shipped a gateway with a CLI and JSON endpoints. The UI plan asks for a control room,
a request ledger, a routing simulator, policy visibility, and report export, and it requires
the UI to work at 320px, under 200% text zoom, by keyboard, and with a screen reader.

Three choices were expensive enough to record:

1. Where the UI runs and how it is built.
2. How the browser proves it may read a project's ledger.
3. Whether policy is editable from the UI.

## Decision

**Same process, no build step.** The dashboard is four static files served by the gateway
itself from `src/cost_firewall/dashboard/`. No bundler, no `node_modules`, no CDN, no second
service, no framework.

**The operator's own project key is the credential.** The shell is static and holds no data;
every figure is fetched from the existing `/v1/gateway/*` endpoints with a key the operator
pastes. The key is kept in that tab's `sessionStorage` and sent as a bearer token to the same
origin. There is no cookie, no server-side session, and no new auth mechanism. The project
selector is populated from `whoami`, so the UI can only offer projects the key can read.

**Policy is read-only in the UI.** Caps, allowlist, limits, retention, and redaction mode are
displayed; changing them stays in the TOML file plus `check-config`.

**Strict CSP on the dashboard only.** `default-src 'none'; script-src 'self'; style-src
'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none';
frame-ancestors 'none'`, plus `nosniff` and `no-referrer`. API paths are untouched.

## Why

- **Deployment cost.** The product's whole promise is a $0 local deployment. A UI that needs
  Node at build time, or a CDN at run time, breaks that on an air-gapped host and adds a
  supply-chain surface for a page that shows numbers. Serving the shell from the process that
  already has the data also means `curl` and the UI cannot disagree about what is running.
- **One authorization model.** Reusing the project key means the UI cannot be a way around
  the gateway's own rules. A separate admin credential would need its own storage, rotation,
  and audit trail, and would be a second thing to get wrong. The cost is that the browser
  holds a key — which is exactly why the CSP exists.
- **Read-only policy avoids a half-built write path.** Editing a budget over HTTP needs a
  write credential, an audit trail for policy changes, optimistic-concurrency handling, and a
  resolution path for "the new cap is below what is already reserved". None of that is
  designed yet, and a form that silently fails to lower a cap is worse than no form. The
  read-only view still answers "what is enforced right now", which is the question the
  control room exists for.
- **The CSP is scoped to the dashboard** because the key lives in the page. Applying it to
  JSON responses would be noise for non-browser clients and would obscure debugging.

## Consequences

- The dashboard cannot be deployed separately from the gateway, and cannot be used with a
  remote gateway unless that gateway's origin is reachable from the browser (CORS is not
  configured; same-origin is assumed).
- There is no prompt reveal, now or later, because the ledger never stores prompt text. That
  is a product constraint, not a UI omission.
- Browser automation is not in CI yet. The accessibility and reflow gates were run manually
  against a live server and are recorded in `docs/dashboard.md`; adding Playwright is a
  follow-up.
- Any future write path (policy editing, key rotation) needs its own ADR covering the
  credential, the audit event, and the conflict rule.

## Verification

- `tests/integration/test_dashboard_api.py` pins the endpoints the UI consumes: per-project
  scoping, no prompt text, refusals visible as `blocked` rows, policy view free of key
  material, `whoami` scoping, provider health, CSV matching the JSON report, and the
  dashboard's CSP plus the absence of inline script and style.
- `tests/integration/test_dashboard_api.py::test_api_responses_are_not_given_the_dashboard_policy`
  pins the scoping of the header.
- Manual browser checks (Chromium, live server): axe-core 4.10.2 with 0 violations on all six
  views and on the open request dialog; zero horizontal overflow at 320/390/768/1024/1440 and
  at 200% text zoom on 320px; keyboard tab order, arrow keys, dialog focus and restore;
  44px controls; long identifiers and six-figure currency values.
