# 0008 — Show the daily cap next to the monthly meter, and style through the CSSOM

- **Status:** accepted
- **Date:** 2026-10-21
- **Scope:** Phase B (UI/UX) for `ai-gateway-cost-firewall`

## Context

Two defects were found by driving the real dashboard against a running gateway rather than
by reading the markup.

**1. The control room reported the wrong binding limit.** The budget panel rendered a single
monthly meter. A project configured with `monthly_budget_usd = 0.05` and
`daily_budget_usd = 0.0002` sat at `0.26%` of the month while refusing every request with
`daily_budget_exhausted`. The page said "$0.000132 committed of $0.0500" and "0.26% used" —
both true, and both useless: nothing on screen explained why traffic had stopped. The daily
figure existed only as a caption sentence, which is the least scannable place for the number
that is actually enforcing the policy.

**2. Every dynamically sized element rendered at zero width.** The dashboard ships
`style-src 'self'`. That directive blocks inline **style attributes**, not just `<style>`
blocks. The DOM helper built elements with `setAttribute("style", "width: 66%")`, so the
attribute text survived in the DOM (and in `outerHTML`, which is why the mistake is easy to
miss) while the browser discarded every declaration. Measured: a 66% fill inside a 1182px
track rendered 1px wide. Assigning the same value through `element.style.setProperty()`
rendered 779px. The monthly fill was small enough that the collapse looked like a sub-pixel
rounding artifact.

## Decision

**Render the daily cap as a second, compact meter** directly under the monthly one, with the
same settled/reserved/80%-mark structure, its own accessible name, and a badge that reads
`Daily cap reached` when committed spend meets or exceeds it. Replace the single binding
sentence with an explicit statement of which cap is refusing traffic, covering four cases:
both reached, month only, day only, and no daily cap configured.

**Route every inline style through the CSSOM.** The `el()` helper recognises a `style` key and
applies each declaration with `element.style.setProperty()`. A static declaration
(`flex-direction: column`) was replaced with the `legend--stacked` class, so the only remaining
dynamic styles are the two meter widths and the 80% mark position.

## Why

- **The screen must name the limit that stopped the request.** An operator whose traffic is
  being refused needs the enforcing cap, not the largest number on the page. Ratio comparison
  alone is not enough: when both caps are reached, "the monthly cap is tighter" is true and
  misleading, so the both-reached case gets its own sentence.
- **Two meters, not one combined meter.** Monthly and daily budgets are independent caps with
  independent reset periods. A single normalised bar would invent a scale the gateway does not
  enforce.
- **CSSOM rather than relaxing the policy.** `style-src 'unsafe-inline'` would fix the bars and
  re-open the injection path the CSP exists to close: the page holds a project key in memory.
  `setProperty` is not covered by the inline-style restriction, so the strict policy and the
  dynamic widths can coexist. The constraint is documented at the helper so the next dynamic
  style does not repeat the mistake.

## Consequences

- The budget panel is taller by one compact meter (~56px). It still fits the 1440px first
  viewport with the usage and provider panels below.
- Any new dynamic style must go through `el({style})` or a class; a raw
  `setAttribute("style", …)` will silently render nothing under the shipped policy.
- Browser automation is still not in CI (see 0007), so this class of defect is caught by the
  manual browser pass recorded in `docs/dashboard.md`, not by `pytest`. A source-text
  assertion would be a tautology; the honest fix is a Playwright gate, which remains a
  follow-up.

## Verification

- `curl` against a live gateway produced 11 allowed responses then
  `429 budget_exhausted` with `daily_budget_exhausted` in the message.
- Chromium, live server, 1440px: daily meter renders `$0.000006 committed of $0.000200`,
  `3.00% used`, fill 35px inside a 1118px track; monthly fill 3px.
- Four cap combinations driven through request interception — both reached, day only, month
  only, no daily cap — produce the expected badge and binding sentence, and the no-daily case
  renders one bar instead of two.
- Over-budget rendering (`$0.0582` committed against a `$0.0500` cap) shows `Over budget`,
  `Daily cap reached`, full bars, and the existing callout.
- Zero horizontal overflow at 320/390/768/1024/1440; fills scale with the track at every
  width (8px at 320 → 35px at 1440).
- axe-core 4.10.2, WCAG 2.0/2.1/2.2 A+AA plus best-practice, all six views at 390px and
  1440px: 0 violations.
- `ruff format --check`, `ruff check`, `mypy src`: clean. `pytest`: 139 passed.
