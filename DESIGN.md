# CodeBot Operations Console Design System

## 0. Research Log

- Embedded references: shortlisted Sentry, Vercel, and Warp. Picked the operational dashboard discipline in `taste-skill.md` with Sentry's data-dense, warm-dark developer-tool system as source material.
- Lazyweb: skipped; this first iteration is constrained to the existing `botop live` data contract rather than a new product workflow.
- Imagen drafts: skipped; the product surface is live operational data, not a marketing or image-led experience.

## 1. Atmosphere & Identity

A browser-native terminal mirror for operators who need the `botop live` hierarchy at a glance. The signature is a layered midnight console with a single acid-lime health signal; compact box-drawn regions retain terminal density without recreating cards.

## 2. Color

### Palette

| Role | Token | Value | Usage |
|---|---|---:|---|
| Surface/primary | `--surface-primary` | `#150f23` | Application canvas |
| Surface/secondary | `--surface-secondary` | `#1f1633` | Header and panel backgrounds |
| Surface/elevated | `--surface-elevated` | `#2a2140` | Hover and focused elements |
| Text/primary | `--text-primary` | `#f7f4fb` | Titles and main values |
| Text/secondary | `--text-secondary` | `#d8d2e2` | Supporting operational detail |
| Text/tertiary | `--text-tertiary` | `#aea5bc` | Captions and timestamps |
| Border/default | `--border-default` | `#4a3d63` | Tables and panel boundaries |
| Accent/primary | `--accent-primary` | `#c2ef4e` | Healthy state, focused controls |
| Accent/hover | `--accent-hover` | `#d7ff78` | Hovered interactive text |
| Status/warning | `--status-warning` | `#ffb287` | Cautions and rework |
| Status/error | `--status-error` | `#fa7faa` | Dead or failed state |
| Status/info | `--status-info` | `#9da9ff` | Informational state |

Colors appear only through these tokens. Accent color conveys action or healthy state, never decoration.

## 3. Typography

| Level | Token | Size | Weight | Usage |
|---|---|---:|---:|---|
| Page title | `--type-title` | 24px | 700 | Console identity |
| Section title | `--type-section` | 16px | 650 | Panel labels |
| Body | `--type-body` | 14px | 400 | Operational details |
| Caption | `--type-caption` | 12px | 600 | Metadata and labels |
| Metric | `--type-metric` | 20px | 650 | Primary counts |
| Mono | `--type-mono` | 12px | 500 | IDs, models, times, state values |

- Primary: `system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`.
- Mono: `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`.
- Body text is never below 14px; compact data can use the mono token at 12px.

## 4. Spacing & Layout

All spacing uses a 4px base: `--space-1` 4px, `--space-2` 8px, `--space-3` 12px, `--space-4` 16px, `--space-5` 20px, `--space-6` 24px, `--space-8` 32px.

- The `scroll-body-shell` owns vertical scrolling in its main region; the header remains fixed within the grid shell.
- The content grid is intrinsic: `repeat(auto-fit, minmax(min(18rem, 100%), 1fr))`.
- At under 768px, panels become a single column and tables remain horizontally scrollable only inside their named data region.

## 5. Components

### Status badge
- **Structure:** concise text in a semantic inline element.
- **Variants:** healthy, warning, error, information, neutral.
- **States:** default and text-equivalent fallback; color is never the only state indicator.
- **Accessibility:** status words remain visible at all color settings.

### Metric strip
- **Structure:** label, value, optional context in a compact grid cell.
- **States:** populated, unavailable, loading, error.
- **Accessibility:** values remain text, not canvas-only charts.

### Data panel
- **Structure:** titled `section` with a box-drawn header, state-aware body, and optional bounded table region.
- **States:** populated, loading, empty, error, optional-hidden.
- **Layout:** a tonal panel; table regions own horizontal overflow.

### Ticket group
- **Structure:** state label with an authoritative count and up to three ticket previews.
- **States:** count-only when details are unavailable, populated when previews exist, hidden when the count is zero.
- **Accessibility:** state and count stay as visible text; a missing ticket-detail feed never changes the known count.

### Console frame
- **Structure:** mono header, two metadata lines, and named box-drawn regions.
- **States:** healthy or drain in the header; each region remains visible with an explicit count-only or empty message.
- **Layout:** content uses a bounded horizontal scroll region on narrow viewports rather than wrapping operational columns.

### Ticket explorer shell
- **Structure:** lifecycle-stage rail, independently scrolling ticket list, and independently scrolling selected-ticket work record.
- **States:** loading, no matching tickets, selected ticket, missing ticket detail, and end-of-results.
- **Scale:** the rail renders counts only; the list requests no more than 200 tickets per page and the browser retains only the loaded page set. The work record is fetched only after selection.
- **Accessibility:** lifecycle stages are native buttons with `aria-pressed`; ticket rows are native buttons with a visible selected state and the detail pane uses an `aria-live` status line.

## 6. Motion & Interaction

- Refresh feedback uses a 150ms opacity transition on the status text only.
- Hover and active controls use 150ms transform/opacity transitions.
- `prefers-reduced-motion` disables all transitions.
- No automatic decorative animation; data changes are the meaningful movement.

## 7. Depth & Surface

The console uses a mixed tonal-shift and border strategy: `--surface-secondary` panels are separated by `--border-default`, with one subtle purple-tinted shadow for the connected shell. Interactive controls use a small inset shadow to establish pressability. Radius: 8px for fields and panels, 999px for compact status badges.

## 8. Accessibility Constraints & Accepted Debt

- WCAG 2.2 AA contrast target, visible focus on every control, keyboard-reachable token submission, semantic regions and table headers, and reduced-motion support.
- The local dashboard reads its loopback snapshot without an operator token and never persists credentials in browser storage, cookies, or URLs.
- Accepted debt: no role-based access or mutating controls are included. The exit is a separately specified authenticated operations-control iteration.
