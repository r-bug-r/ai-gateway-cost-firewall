# Design tokens

`src/cost_firewall/dashboard/tokens.css` is the only source of colour, spacing, radius, type,
and motion values for the dashboard. A component may not introduce a value that is not
registered there. This file is the register: it records what each token means and the
measured contrast of every pair the dashboard actually uses.

## Palette

| Token | Light | Dark | Used for |
| --- | --- | --- | --- |
| `--color-canvas` | `#f5f3ee` | `#17150f` | Page background, table stripe base |
| `--color-surface` | `#fffdf8` | `#1e1c15` | Panels, header, tab strip |
| `--color-surface-raised` | `#ffffff` | `#262319` | Inputs, buttons, dialog |
| `--color-ink` | `#20221f` | `#f2efe4` | Body text, headings, values |
| `--color-ink-muted` | `#646860` | `#a9a596` | Labels, hints, captions |
| `--color-border` | `#d6d2c8` | `#3a372c` | Row separators, panel edges |
| `--color-border-strong` | `#7c8079` | `#7a7666` | Control outlines, meter track |
| `--color-accent` | `#b6422b` | `#e57a5e` | Primary action, selected tab, focus ring |
| `--color-accent-ink` | `#fffaf2` | `#1a1712` | Text on an accent fill |
| `--color-success` | `#356b4b` | `#7fbf93` | Settled/success badge |
| `--color-warning` | `#8c641e` | `#d9a44a` | 80% threshold, degraded badge |
| `--color-danger` | `#a72f2f` | `#e78d84` | Blocked/failed badge, rejected key |

Light and dark are the same token names, selected by `prefers-color-scheme`. There is no
separate theme switch, so a value cannot be missing from one mode without the other showing
it immediately.

## Measured contrast

Computed from the sRGB hex values above with the WCAG 2.x relative-luminance formula
(`(L1 + 0.05) / (L2 + 0.05)`). Recompute if any hex changes; a failing pair is a release
blocker, not a note.

| Pair | Light | Dark | Requirement | Result |
| --- | --- | --- | --- | --- |
| `ink` on `canvas` | 14.46:1 | 15.85:1 | 4.5:1 body | pass |
| `ink` on `surface` | 15.77:1 | 14.81:1 | 4.5:1 body | pass |
| `ink` on `surface-raised` | 16.03:1 | 13.65:1 | 4.5:1 body | pass |
| `ink-muted` on `canvas` | 5.13:1 | 7.40:1 | 4.5:1 body | pass |
| `ink-muted` on `surface` | 5.60:1 | 6.91:1 | 4.5:1 body | pass |
| `ink-muted` on `surface-raised` | 5.69:1 | 6.37:1 | 4.5:1 body | pass |
| `accent` on `surface` | 5.43:1 | 5.91:1 | 4.5:1 text, 3:1 UI | pass |
| `accent` on `canvas` | 4.98:1 | 6.32:1 | 4.5:1 text | pass |
| `accent-ink` on `accent` | 5.31:1 | 6.19:1 | 4.5:1 text | pass |
| `success` on `surface` | 6.16:1 | 7.93:1 | 4.5:1 text | pass |
| `success` on `surface-raised` | 6.26:1 | 7.31:1 | 4.5:1 text | pass |
| `warning` on `surface` | 5.22:1 | 7.60:1 | 4.5:1 text | pass |
| `warning` on `surface-raised` | 5.31:1 | 7.00:1 | 4.5:1 text | pass |
| `danger` on `surface` | 6.68:1 | 6.93:1 | 4.5:1 text | pass |
| `danger` on `surface-raised` | 6.79:1 | 6.39:1 | 4.5:1 text | pass |
| `border-strong` on `surface` | 3.96:1 | 3.74:1 | 3:1 UI component | pass |
| `border-strong` on `canvas` | 3.63:1 | 4.01:1 | 3:1 UI component | pass |
| `border` on `surface` | 1.48:1 | 1.43:1 | decorative separator | exempt |

`--color-border` is deliberately below 3:1: it draws row separators inside a table that is
already separated by spacing and alignment, and it is never the sole indicator of a control
boundary. Every interactive control uses `--line-strong`, which clears 3:1 in both modes.

The focus ring is `0 0 0 3px var(--color-canvas), 0 0 0 5px var(--color-accent)`: the inner
canvas-coloured ring separates the accent from the control so the ring stays visible on both
surfaces, and the accent itself is ≥4.98:1 against both backgrounds.

## Status is never colour alone

Every status is a glyph plus a word: `✓ settled`, `✕ blocked`, `!` warning, `·` neutral. The
glyph is `aria-hidden` and the word is the accessible text, so the meaning survives
greyscale, colour blindness, and a screen reader.

## Type and spacing

| Token | Value | Use |
| --- | --- | --- |
| `--text-xs` | 0.75rem | All-caps labels, badge text |
| `--text-sm` | 0.8125rem | Table cells, hints |
| `--text-base` | 0.9375rem | Body |
| `--text-lg` | 1.125rem | Panel titles |
| `--text-xl` | 1.5rem | Section headings |
| `--text-2xl` | 2rem | KPI values, page title |
| `--space-1…12` | 0.25–3rem | Every gap, padding, and margin |
| `--radius-control` | 0.25rem | Inputs, buttons, badges |
| `--radius-panel` | 0.5rem | Panels, dialog |
| `--motion-fast` | 150ms | Control feedback |
| `--motion-standard` | 220ms | Panel and dialog transitions |
| `--measure` | 78rem | Maximum content width |

All type sizes are `rem`, so browser text scaling changes them and nothing is viewport-scaled.
`--measure` caps line length on wide displays without changing type size.

Fonts are system stacks only (`ui-sans-serif`, `ui-monospace`, `ui-serif`). No font file is
downloaded, which keeps the dashboard working on an air-gapped host and removes a
render-blocking request. The serif is used for display text and KPI values; the monospace
face is used only for identifiers, timestamps, model names, and money.

## Motion

`prefers-reduced-motion: reduce` removes every transition and transform, including the skip
link's slide and the toast's pop-in. State changes remain immediate and visible; nothing is
communicated by motion alone.
