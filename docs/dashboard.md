# Dashboard

`GET /dashboard/` serves a single-page operator console. It is part of the gateway process:
no build step, no bundler, no external asset, no second service. Three files
(`index.html`, `app.js`, `app.css` plus `tokens.css`) are served from
`src/cost_firewall/dashboard/`.

## What it is for

An operator needs to answer three questions without reading JSON by hand:

1. How much has this project spent, and how much headroom is left right now?
2. Where would this request go, and would the firewall allow it?
3. What actually happened to a specific request — which attempts, which provider, which
   cost, and why was it refused?

## Credential model

The shell is static and contains no data, so serving the HTML needs no credential. Every
number is fetched by the browser with a project API key the operator pastes in. The key is
kept in `sessionStorage` for that tab only: closing the tab discards it, and it is never
written to disk, a URL, or a log. There is no server-side session and no cookie.

Authorization is the gateway's own: the dashboard calls the same endpoints a script would
(`/v1/gateway/*`), with the same per-project rules. The project selector is populated from
`/v1/gateway/whoami`, which lists only projects sharing the presented credential, so the UI
cannot offer a project the key cannot read.

## Content security policy

The dashboard is served with:

```
default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;
connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```

This matters because the page holds a key in memory: without a policy, any injected script
could read it and exfiltrate spend data. The shell therefore has no inline script and no
inline style, and `tests/integration/test_dashboard_api.py` asserts both the header and the
absence of inline content. API responses are unaffected, so non-browser clients see no
change.

`style-src 'self'` also blocks the style *attribute*, not just `<style>` blocks. A node built
with `setAttribute("style", "width: 66%")` keeps the attribute text — it still appears in
`outerHTML` — while every declaration is discarded, so the element renders at its intrinsic
size. The dashboard's DOM helper therefore applies styles through the CSSOM
(`element.style.setProperty`), which the directive does not restrict. Static declarations use
a class instead. Adding a raw style attribute will silently render nothing.

## Views

| View | Answers | Data source |
| --- | --- | --- |
| Control room | Spend, reservations, headroom, which cap is binding, provider health, recent blocks | `budget`, `usage`, `providers`, `requests` |
| Requests | Which request failed or cost what; per-request trace | `requests`, `requests/{id}` |
| Routing simulator | Would this request be allowed, where would it go, what would it reserve | `policy`, `dry-run` |
| Budgets and policy | The effective caps, allowlist, limits, redaction mode, route order | `policy` |
| Reports | CSV/JSON export for the selected window | `usage`, `usage.csv` |
| Settings | Retention, redaction, rate limits (read-only) | `policy` |

The policy view is deliberately **read-only**. Editing budgets over HTTP would need a write
credential, an audit trail for policy changes, and a resolution path for "new cap is below
what is already reserved" — a separate design with its own threat model. Configuration stays
in the TOML file, validated by `cost-firewall check-config`, until that exists.

The simulator posts to `POST /v1/gateway/dry-run`: it reserves nothing and calls no provider,
so it is safe to use on a production deployment.

## States

Every view has four states, and each is reachable in a browser:

- **Loading** — skeleton blocks in the shape of the incoming content, not a spinner.
- **Empty** — a sentence saying what would appear and why it is absent ("No requests yet.",
  "No attempts observed."). Never a blank panel.
- **Error** — the message, plus a Retry button that re-runs the same load.
- **Rejected key** — every panel is cleared, not just the one that failed. Leaving the
  previous project's numbers on screen would attribute one tenant's spend to another's key.

A failed load never leaves stale numbers visible next to a "connected" status.

### Budget states

The budget panel shows two meters — monthly, then a compact daily row — because the gateway
enforces both caps independently. The binding sentence names the cap that is refusing
traffic, in four cases:

| State | Badge | Binding sentence |
| --- | --- | --- |
| Both caps reached | `Over budget` + `Daily cap reached` | "Both caps are reached; a request is refused by whichever it hits first." |
| Monthly only | `Over budget` / `Fully committed` | "The monthly cap is reached, so new requests are refused regardless of the daily figure." |
| Daily only | `Daily cap reached` | "The daily cap is reached; the month still has room, so traffic resumes when the day resets." |
| No daily cap configured | none | "No daily cap is configured, so only the monthly cap can refuse traffic." |

Each meter carries its own accessible name with the settled, reserved, and committed
figures, so a screen reader hears the numbers rather than "image".

## Privacy

Prompt and completion text never reach the browser, because the ledger never stores them.
The request table states this in its caption rather than showing an empty "content" column.
There is no reveal setting and no local-only prompt preview: the data does not exist to
reveal.

## Accessibility

Verified in a real browser against the running server, not asserted from markup:

| Gate | Result |
| --- | --- |
| axe-core 4.10.2, WCAG 2.0/2.1/2.2 A + AA + best-practice, all six views at 390 and 1440 | 0 violations |
| axe on the open request dialog at 390px | 0 violations |
| Horizontal overflow at 320/390/768/1024/1440, all six views | 0 px everywhere |
| Text-only reflow: 200% root font at 320px, all six views | 0 px overflow |
| Keyboard: tab order, arrow keys on the tablist, Enter to open a request, Escape to close | works; focus returns to the originating row |
| Touch targets ≥44px | all controls; skip link included |
| Status announcements | `role="status"`, `aria-live="polite"` |
| Status area height | reserved; a long message does not shift the page |
| Contrast | every pair measured in [design-tokens.md](design-tokens.md) |
| Status meaning | glyph + word, never colour alone |
| Reduced motion (`prefers-reduced-motion: reduce`) | animations and transitions collapse to 0.01ms; the toast does not animate |
| Tablist arrow keys | Right/Left move and select, Home/End jump to the ends, focus and the visible panel follow |

Wide tables scroll inside a container that becomes focusable, named (`role="group"` +
`aria-labelledby` pointing at the table caption) and reachable by keyboard only while it
actually overflows. A focusable div without a role or name would be an unlabelled tab stop;
a scroll container without a tab stop is unreachable without a pointer.

The request dialog is a native `<dialog>` with `aria-labelledby`, focus moved to Close on
open, and focus restored to the row button on close. Its scrollable body is a named
`role="region"` with `tabindex="0"`.

## Known limitations

- **No prompt inspection by design** — the ledger stores metadata only.
- **No policy editing** — see above; configuration is file-based.
- **No live streaming** — the gateway rejects `stream: true` (ADR 0005), so the dashboard has
  no streaming view.
- **Provider health is observed, not probed** — a provider with no traffic shows no row, and
  the panel says so. There is no synthetic uptime check.
- **Single project per tab** — the selector switches projects by refetching; there is no
  side-by-side comparison.
- **No browser automation in CI** — the gates above were run manually against a local server.
  Adding Playwright to CI is a follow-up, not a claim.
