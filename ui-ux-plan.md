# AI Gateway Cost Firewall — UI/UX Plan

> Project-specific section extracted from `../open-source-projects-ui-ux-plan.md`. Shared UI/UX gates in that source remain authoritative.

## Shared UI/UX Quality Gate (authoritative source excerpt)

## 1. UI/UX quality gate

### 1.1 Product-wide design contract

Every project UI must:

- Put the primary user task first: orient, inspect, act, confirm.
- Use product-specific data and labels; never invent users, testimonials, metrics, or activity.
- Define loading, empty, error, success, disabled, permission, offline, and destructive-confirmation states before implementation.
- Keep primary actions visible at the point where the task ends; avoid scroll-back for approval, save, or export.
- Use semantic HTML, visible keyboard focus, labels, useful error copy, and screen-reader announcements.
- Never rely on color alone to communicate status.
- Use fixed-height persistent chrome and reserve space for transient status so loading or error text cannot move the layout.
- Preserve the user's context when a side panel, modal, filter, or tab changes.
- Make data deletion, provider use, retention, and permission boundaries visible at the moment they matter.
- Support 320, 390, 768, 1024, and 1440 CSS-pixel widths without horizontal overflow.

### 1.2 Shared design stance

**Aesthetic direction:** editorial industrial utility. The interface should feel like a trustworthy working instrument: crisp borders, compact data tables, deliberate whitespace, one restrained accent, and typography that makes hierarchy obvious. Avoid purple SaaS gradients, floating-card stacks, glassmorphism, excessive pills, giant marketing heroes, and decorative AI motifs.

**Differentiation anchor:** every project exposes its most important trust boundary as a visible structural element:

- Maintainer Autopilot: a proposal-to-approval rail.
- Trace-to-Eval: a trace timeline with pass/fail evidence.
- Agent Firewall: an allow/approval/deny decision ledger.
- Gateway: a live budget meter and routing decision.
- Home File: a document citation rail connecting extracted fields to the source page.

If the logo is removed, this anchor should still identify the product.

**DFII estimate:** 11/15 for developer tools and 9/15 for Home File. The direction fits operational products, is feasible with CSS and semantic HTML, performs well without effects, and remains maintainable when tokens are enforced.

### 1.3 Token architecture

Create one versioned token file per product, with names stable across light and dark themes. No component may use an unregistered color or spacing value.

```css
:root {
  --color-canvas: #f5f3ee;
  --color-surface: #fffdf8;
  --color-surface-raised: #ffffff;
  --color-ink: #20221f;
  --color-ink-muted: #646860;
  --color-border: #d6d2c8;
  --color-border-strong: #9a9d94;
  --color-accent: #b6422b;
  --color-accent-ink: #fffaf2;
  --color-success: #356b4b;
  --color-warning: #8c641e;
  --color-danger: #a72f2f;
  --space-1: 0.25rem;
  --space-2: 0.5rem;
  --space-3: 0.75rem;
  --space-4: 1rem;
  --space-6: 1.5rem;
  --space-8: 2rem;
  --space-12: 3rem;
  --radius-control: 0.25rem;
  --radius-panel: 0.5rem;
  --focus-ring: 0 0 0 3px #f5f3ee, 0 0 0 5px #b6422b;
  --motion-fast: 150ms;
  --motion-standard: 220ms;
}
```

These are starting tokens, not measured brand values. Before production, calculate every text/background pair with a WCAG checker and record the ratio in a token register. The accent is for actions and emphasis, not body text unless it passes the contrast gate.

Typography:

- Display: a locally available, licensed serif or strong editorial face such as Newsreader only if its license is verified; otherwise use a local system fallback with no external download.
- Body/UI: a readable sans-serif already available on the platform or a checked open font; do not make a paid font a dependency.
- Data/code: a monospace face only for IDs, hashes, timestamps, policies, token counts, and logs.
- Use a type scale rather than viewport-scaled text. Keep letter spacing at 0 except where a documented all-caps label needs spacing.

Component primitives:

- `AppShell`, `SkipLink`, `PageHeader`, `Breadcrumbs`
- `Button` with text and familiar icon only when the icon improves recognition
- `IconButton` with a tooltip and accessible name
- `StatusBadge` with text plus icon/pattern
- `DataTable` with responsive stacked fallback
- `FilterBar`, `SearchField`, `SegmentedControl`
- `Panel` for genuinely framed tools; do not nest cards inside cards
- `Timeline`, `Diff`, `Citation`, `AuditEvent`
- `Dialog`, `Drawer`, `Toast`, `ConfirmAction`
- `Skeleton`, `EmptyState`, `ErrorState`, `PermissionState`

### 1.4 Interaction and motion

One motion personality per product: **Snap** for developer tools, **Silk** for Home File. Motion communicates state, never decorates the screen.

- 150ms control feedback.
- 220ms panel and drawer transitions.
- Skeleton shimmer only for known content shapes.
- Success confirmation uses a short, non-blocking pop-in.
- No animated KPI numbers, infinite ambient loops, parallax, or delayed primary content.
- Honor `prefers-reduced-motion` by removing transforms and keeping state changes immediate.

### 1.5 Accessibility and responsive gates

Before a UI release:

- Run axe-core and fix all serious/critical findings; document accepted false positives.
- Tab through every flow without a mouse; focus must remain visible and ordered.
- Test with a screen reader on the primary workflow and verify headings, labels, status announcements, dialog focus, and table relationships.
- Meet WCAG 2.2 AA contrast targets: 4.5:1 normal text, 3:1 large text and essential UI indicators.
- Use 44px minimum touch targets for compact controls where practical; never make a primary action smaller than 44px high.
- Use `aria-live="polite"` for asynchronous result/status updates and `role="alert"` only for urgent errors.
- Test zoom at 200% and text-only reflow at 320px.
- Check `document.documentElement.scrollWidth - document.documentElement.clientWidth === 0` at 390px, 768px, 1024px, and 1440px.
- Render screenshots at each breakpoint and inspect them for overlap, clipping, ragged alignment, unreadable density, and broken focus states.
- Use `min-width: 0`, `overflow-wrap: anywhere`, wrapping toolbars, reserved status heights, and `body { overflow-x: clip; }` as a backstop. The backstop does not replace root-cause fixes.

### 1.6 UI implementation sequence

1. Write the task flow and acceptance states.
2. Create tokens and a semantic component inventory.
3. Design the primary screen in a plain local fixture with real-length copy.
4. Add all states before adding visual polish.
5. Implement keyboard and screen-reader behavior.
6. Add responsive rules and verify narrow overflow.
7. Add restrained motion and reduced-motion behavior.
8. Run browser screenshots, axe checks, and a fresh-eyes critique.
9. Fix measured gates first, then deliberate visual signals.
10. Record the design decision and ship a changelog entry.

---

---

# 5. UI/UX Plan: AI Gateway Cost Firewall

## User task

An operator needs to see where a request will route, know whether it is within budget, and investigate cost or provider failures without exposing prompt content.

**Primary task flow:** select project -> inspect budget -> simulate route -> inspect request ledger -> configure policy -> export usage report.

## Information architecture

- **Control room:** current budget, requests, provider health, and recent blocks.
- **Requests:** searchable request ledger with redacted content preview.
- **Routing simulator:** model/provider decision with estimated cost and fallback path.
- **Budgets and policies:** project caps, model allowlist, rate limits, redaction rules.
- **Reports:** CSV/JSON export and date/provider/project filters.
- **Settings:** auth, retention, provider keys, local mode.

## Visual direction

A compact operations workbook, not a finance dashboard. Use a strong horizontal **budget meter** with an explicit numeric label, a neutral grid, and one amber/red warning accent for thresholds. Avoid animated counters and avoid green/red-only status.

## Screens and acceptance criteria

### A. Control room

Show budget used/remaining, reservation state, provider availability, request rate, and the last blocked request. Every number has a period and unit. Empty metrics use “No requests yet.”

### B. Request detail

Show request ID, project, route decision, provider/model, estimated and settled cost, token counts, latency, retry/fallback chain, redaction outcome, and error class. Prompt content is hidden by default and requires an explicit local-only reveal setting.

### C. Routing simulator

Inputs: project, model preference, token estimate, idempotency, and request class. Output: allow/block, selected provider, estimated cost, budget reservation, fallback eligibility, and policy reasons. Provide copyable JSON for debugging without secrets.

### D. Budget and policy settings

Use forms with plain labels, unit suffixes, validation, and a test policy action. Prevent saving a budget lower than currently reserved spend without an explicit resolution path.

## UI verification

- Fake-provider run that exhausts a budget and produces a fallback.
- Verify no prompt values appear in request list or error banners by default.
- Test large currency/token values, long IDs, and offline provider states.
- Test keyboard and screen reader announcement for budget block and provider failure.
- Measure narrow overflow and screenshot the budget meter at 390px.

---

---

## Status (this repository)

Implemented in `src/cost_firewall/dashboard/` and served by the gateway at `GET /dashboard/`.
Decisions are recorded in `decisions/0007-dashboard-local-readonly.md` and
`decisions/0008-daily-cap-in-meter.md`; the view-by-view description, states, and the
accessibility evidence table live in `docs/dashboard.md`, and the token register with measured
contrast in `docs/design-tokens.md`.

| Plan item | Where | Verified by |
| --- | --- | --- |
| 1.5 axe-core, all views | `app.css`, `index.html` | axe-core 4.10.2, 0 violations at 390px and 1440px |
| 1.5 tab order, visible focus, dialog focus | `app.js` | Chromium: order, rings, Escape, focus restore to the originating row |
| 1.5 contrast 4.5:1 / 3:1 | tokens | every pair measured in `docs/design-tokens.md` |
| 1.5 44px touch targets | tokens | measured on every control, skip link included |
| 1.5 `aria-live` status, reserved height | `index.html` | `role="status"`, `aria-live="polite"`, `aria-atomic`, fixed block height |
| 1.5 200% zoom / text-only reflow at 320px | `app.css` | 0px overflow on all six views |
| 1.5 overflow at 390/768/1024/1440 (+320) | `app.css` | `scrollWidth - clientWidth === 0` at all five widths |
| 1.5 breakpoint screenshots | `.tmp-smoke/*.png` | inspected at 320/390/768/1024/1440, over-budget, empty ledger, large values |
| A. Control room | `renderControl` | monthly + daily meters, reservation split, provider outcomes, recent blocks |
| B. Request detail | `openRequest` | route, attempts, costs, tokens, latency, error class, redaction outcome |
| C. Routing simulator | `renderSimulator` | `POST /v1/gateway/dry-run`; allow/block, targets, reservation, reasons |
| D. Budget and policy | `renderPolicy` | read-only caps, allowlist, limits, retention, redaction mode |

Deliberate deviations, with reasons:

- **No prompt reveal (plan item B).** The ledger stores no prompt or completion text, so there
  is nothing to reveal and no "local-only reveal setting" that could leak. The request table
  says so in its caption. This is stricter than the plan and is a product constraint, not an
  unfinished feature.
- **Policy is read-only (plan item D).** A write path needs a write credential, a policy audit
  event, and a rule for "new cap is below reserved spend" — see ADR 0007. The simulator
  provides the plan's "test policy action" without persisting anything.
- **Provider health is observed, not probed.** A provider with no traffic has no row; a failing
  provider shows its failure count and last error class. Probing would add network calls the
  $0 offline mode forbids.
