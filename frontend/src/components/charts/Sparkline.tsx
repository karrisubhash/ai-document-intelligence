import { colorClass, type ChartColor } from "./common";
import { areaPath, isolatedPoints, linePath, segments } from "./scale";

/**
 * A word-sized trend line: no axes, no interaction, scaled to its box
 * (100% wide, `height` tall). Its accessible name says where the trend
 * went, so it is never only a picture.
 *
 * <Sparkline values={kpi.series} label="Questions per day" />
 */
export function Sparkline({
  values,
  label,
  color = 1,
  height = 32,
  area = true,
  min = 0,
}: {
  values: (number | null)[];
  /** What the line is, e.g. "Questions per day". */
  label: string;
  color?: ChartColor;
  height?: number;
  area?: boolean;
  /** The bottom of the scale. 0 for counts; pass the data's minimum to zoom. */
  min?: number;
}) {
  // A fixed coordinate space stretched to the box: no measuring, so it can
  // sit in any tile without a layout pass. Strokes do not stretch
  // (vector-effect in charts.css).
  const width = 100;
  const known = values.filter((value): value is number => value !== null && value !== undefined);
  const max = Math.max(min + 1e-9, ...known);
  const pad = 2;
  const x = (index: number) => (values.length > 1 ? (index / (values.length - 1)) * width : width / 2);
  const y = (value: number) => pad + (1 - (value - min) / (max - min)) * (height - pad * 2);
  const runs = segments(values, x, y);
  const first = known[0];
  const last = known[known.length - 1];
  const name = known.length
    ? `${label}: from ${first.toLocaleString()} to ${last.toLocaleString()}`
    : `${label}: no data`;

  return (
    <svg
      className={`sparkline ${colorClass(color, 0)}`}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      style={{ height: `${height}px` }}
      role="img"
      aria-label={name}
    >
      {area ? <path className="chart-area" d={areaPath(runs, height)} /> : null}
      <path className="chart-line" d={linePath(runs)} />
      {/* A lone point between gaps: a round-capped stroke of no length,
          which stays round however the box stretches (a circle would not). */}
      {isolatedPoints(runs).map(([px, py]) => (
        <path key={`${px}-${py}`} className="sparkline-point" d={`M${px},${py}h0.01`} />
      ))}
    </svg>
  );
}
