/**
 * The chart kit: hand-rolled SVG and HTML charts, no dependencies, safe
 * under the production CSP (no <style> injection, no innerHTML, no eval -
 * colours are classes over tokens, positions are React style props).
 *
 * WHICH CHART
 *   KpiTile      one headline number, its change vs the previous period
 *                (coloured by whether that direction is good) and a sparkline
 *   Sparkline    a word-sized trend, no axes
 *   LineChart    one or more measures over time; `area` for a soft fill;
 *                null values are gaps, not zeros
 *   BarChart     counts per day or per category; `stacked` for parts of a
 *                whole per bar, `grouped` to compare series side by side
 *   BarList      a ranked list with values (top documents, failure reasons)
 *   Donut        parts of one whole, 2-6 parts, total in the middle
 *   SegmentedBar parts of one whole in one compact bar
 *   Legend       swatch + label (+ value) list; the charts render their own
 *
 * DATA. Cartesian charts take `labels` (one per x position: "YYYY-MM-DD"
 * days with labelType "date", the default, or category names) and
 * `series: {key, label, values, color?}[]` with one value per label. Days
 * are parsed as LOCAL dates (lib/format parseLocalDate), so a bucket is
 * never labelled with the day before.
 *
 * COLOUR. `color` is a categorical slot 1-8, assigned IN ORDER (series 1 is
 * slot 1, and so on - the order is what was validated for colour-blind
 * separation in light and dark, see styles/charts.css), or a status
 * ("success", "warning", "danger", "info", "muted") for data whose meaning
 * is a status. Omit it and series get slots in order. A colour follows the
 * entity: when a filter removes a series, pass the survivors their own
 * slots, not new ones.
 *
 * ACCESSIBILITY. Every chart has a `title` (its accessible name, the SVG
 * <title>, the table caption) and renders its numbers as a visually hidden
 * table (`showTable` shows it to everyone). LineChart and BarChart are
 * focusable: arrow keys move between points, Home/End jump, and each point
 * is announced through a live region; the same point shows the tooltip a
 * pointer gets. Identity is never colour alone: two series or more get a
 * legend. Motion is CSS transitions only, which prefers-reduced-motion
 * turns off (styles.css).
 *
 * SIZE. Charts fill their container's width (ResizeObserver) and take a
 * fixed `height` (default 220), reserved before measuring - no layout
 * shift. `loading` shows a skeleton of the same size; all-zero or empty
 * data shows the `empty` message instead of a flat line.
 */
export { BarChart, barPath } from "./BarChart";
export { BarList, type BarListItem } from "./BarList";
export type { CartesianProps } from "./Cartesian";
export {
  ChartEmpty,
  ChartSkeleton,
  DataTable,
  colorClass,
  type ChartColor,
  type Series,
  type ValueFormat,
} from "./common";
export { KpiTile, describeDelta, type DeltaStyle, type GoodDirection, type KpiDelta } from "./KpiTile";
export { Legend, type LegendItem } from "./Legend";
export { LineChart } from "./LineChart";
export { Donut, SegmentedBar, type PartItem } from "./PartToWhole";
export { Sparkline } from "./Sparkline";
export * as scales from "./scale";
