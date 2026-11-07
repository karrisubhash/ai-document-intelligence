# Chart kit

`frontend/src/components/charts/` - hand-rolled SVG and HTML charts with no dependency,
exported from `index.ts` (whose header is the usage guide). Styles and series colours are
in `frontend/src/styles/charts.css`.

| Component | Use |
|---|---|
| `KpiTile` | A headline number, its change against the previous period (`describeDelta`: percent for counts, points for rates; coloured by `good: "up" \| "down" \| "neutral"`), a sparkline, a same-size loading skeleton |
| `Sparkline` | A word-sized trend; stretches to its box without measuring |
| `LineChart` | One or more series over days or categories; `area`; `null` is a gap, never zero |
| `BarChart` | One series, `stacked`, or `grouped`; bars stay at least 1px wide at 90 days |
| `BarList` | A ranked HTML list with proportional bars, values, optional links and hints |
| `Donut`, `SegmentedBar` | Parts of one whole, each part named with value and share |
| `Legend` | Swatch + label (+ value); cartesian charts add it for two series or more |

Rules the kit enforces, so screens built on it inherit them:

- **CSP.** No `<style>` injection, no `innerHTML`, no `eval`: colours are classes over CSS
  variables, geometry is SVG attributes, positions are React style props (CSSOM). Safe under
  `style-src 'self'`.
- **Colour.** Eight categorical slots assigned in order (series 1 = slot 1), validated for
  colour-blind separation on this app's light and dark card surfaces, with dark mode stepped
  separately; status colours (`success`, `warning`, `danger`, `info`, `muted`) only for data
  whose meaning is a status. Text never takes a series colour.
- **Accessibility.** Each chart has a `title` (accessible name, SVG `<title>`, table caption)
  and a `<desc>`; its numbers are also a visually hidden table (`showTable` shows it).
  Cartesian charts are focusable: arrow keys move between points, Home/End jump, Escape
  leaves, and each point is announced through a live region while the tooltip shows it.
  Motion is CSS transitions only, so `prefers-reduced-motion` turns it off; forced-colours
  mode keeps the series colours and outlines tiles.
- **Layout.** Width from a ResizeObserver, height reserved up front - no layout shift.
  `loading` draws a skeleton of the same size; all-zero data draws an empty message, not a
  flat line.
- **Dates.** `"YYYY-MM-DD"` days are parsed as local dates (`lib/format.ts` `parseLocalDate`),
  so a bucket is never labelled with the day before west of UTC.

Unit tests: `charts.test.tsx` (ticks, gaps, stacks, bands, arcs, deltas, rendering,
keyboard, empty and loading states).
