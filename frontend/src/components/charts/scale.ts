/**
 * The arithmetic under every chart: domains, nice ticks, band positions,
 * paths with gaps, stacks. Pure functions, no DOM, so they are unit-tested
 * directly (charts.test.ts).
 */

/** A step of 1, 2 or 5 x 10^n that cuts `range` into about `count` parts. */
export function niceStep(range: number, count: number): number {
  if (!(range > 0) || !Number.isFinite(range)) return 1;
  const raw = range / Math.max(1, count);
  const power = 10 ** Math.floor(Math.log10(raw));
  const fraction = raw / power;
  // Thresholds at the geometric midpoints between 1, 2, 5 and 10 (as d3
  // does), so the tick count lands as close to `count` as round steps allow.
  const factor = fraction >= Math.sqrt(50) ? 10 : fraction >= Math.sqrt(10) ? 5 : fraction >= Math.sqrt(2) ? 2 : 1;
  return factor * power;
}

/** Decimal places that print every multiple of `step` exactly. */
function decimals(step: number): number {
  return step >= 1 ? 0 : Math.min(10, Math.ceil(-Math.log10(step) - 1e-9));
}

/**
 * Round ticks covering [min, max]: [0, 5, 10, 15] for 0-13. `integer`
 * keeps the step at least 1, so a count of 2 is not ticked 0, 0.5, 1, 1.5.
 * The first tick is at or below `min` and the last at or above `max`, so
 * they double as the axis domain.
 */
export function niceTicks(min: number, max: number, count = 4, integer = false): number[] {
  let low = Number.isFinite(min) ? min : 0;
  let high = Number.isFinite(max) ? max : 0;
  if (low > high) [low, high] = [high, low];
  if (low === high) {
    // Nothing to spread: give the value some room, upwards from zero.
    if (high === 0) high = 1;
    else if (high > 0) low = Math.min(0, low);
    else high = 0;
  }
  let step = niceStep(high - low, count);
  if (integer) step = Math.max(1, Math.round(step));
  const places = decimals(step);
  const first = Math.floor(low / step + 1e-9) * step;
  const last = Math.ceil(high / step - 1e-9) * step;
  const ticks: number[] = [];
  for (let value = first; value <= last + step / 2; value += step) {
    ticks.push(Number(value.toFixed(places)));
  }
  return ticks;
}

/** A linear map from a domain onto a pixel range. */
export function linearScale(domain: [number, number], range: [number, number]) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  return (value: number) => r0 + ((value - d0) / span) * (r1 - r0);
}

export interface Band {
  /** Left edge of band `index`. */
  x: (index: number) => number;
  /** Centre of band `index`. */
  center: (index: number) => number;
  width: number;
  step: number;
}

/**
 * `count` equal bands across `width`, with `padding` (a fraction of a step)
 * between them. The band never goes below one pixel, so 90 days still draw.
 */
export function bandScale(count: number, width: number, padding = 0.25): Band {
  const step = count > 0 ? width / count : width;
  const bandWidth = Math.max(1, step * (1 - padding));
  const inset = (step - bandWidth) / 2;
  return {
    x: (index) => index * step + inset,
    center: (index) => index * step + step / 2,
    width: bandWidth,
    step,
  };
}

/** The index of the band under pixel `x`, clamped to the bands. */
export function indexAt(x: number, count: number, width: number): number {
  if (count <= 0 || width <= 0) return 0;
  return Math.min(count - 1, Math.max(0, Math.floor((x / width) * count)));
}

/**
 * Every how many labels to print so that neighbours stay `minSpacing`
 * pixels apart: 1 prints all, 7 prints every seventh.
 */
export function labelEvery(count: number, width: number, minSpacing = 56): number {
  if (count <= 1 || width <= 0) return 1;
  return Math.max(1, Math.ceil((count * minSpacing) / width));
}

/**
 * Which indices get an axis label: every `every`-th counted back from the
 * LAST one, so the most recent day is always labelled.
 */
export function labelledIndices(count: number, every: number): number[] {
  const indices: number[] = [];
  for (let index = count - 1; index >= 0; index -= Math.max(1, every)) indices.unshift(index);
  return indices;
}

export type Point = [number, number];

/**
 * Runs of consecutive known points. A null value is a GAP - a day with
 * nothing to measure - never a zero, so a line breaks there rather than
 * diving to the axis.
 */
export function segments(values: (number | null)[], x: (index: number) => number, y: (value: number) => number): Point[][] {
  const runs: Point[][] = [];
  let current: Point[] = [];
  values.forEach((value, index) => {
    if (value === null || value === undefined || Number.isNaN(value)) {
      if (current.length) runs.push(current);
      current = [];
    } else {
      current.push([x(index), y(value)]);
    }
  });
  if (current.length) runs.push(current);
  return runs;
}

const fixed = (value: number) => Number(value.toFixed(2));

/** An SVG path through each run; a single-point run draws nothing (dots do). */
export function linePath(runs: Point[][]): string {
  return runs
    .filter((run) => run.length > 1)
    .map((run) => run.map(([px, py], index) => `${index ? "L" : "M"}${fixed(px)},${fixed(py)}`).join(""))
    .join("");
}

/** The area under each run, closed down to `baseline`. */
export function areaPath(runs: Point[][], baseline: number): string {
  return runs
    .filter((run) => run.length > 1)
    .map((run) => {
      const top = run.map(([px, py], index) => `${index ? "L" : "M"}${fixed(px)},${fixed(py)}`).join("");
      const last = run[run.length - 1];
      return `${top}L${fixed(last[0])},${fixed(baseline)}L${fixed(run[0][0])},${fixed(baseline)}Z`;
    })
    .join("");
}

/** Points that stand alone between gaps - shown as dots, or they vanish. */
export function isolatedPoints(runs: Point[][]): Point[] {
  return runs.filter((run) => run.length === 1).map((run) => run[0]);
}

/**
 * Cumulative [start, end] of each series at each index, in series order;
 * a missing value stacks as zero.
 */
export function stack(series: (number | null)[][]): [number, number][][] {
  const length = Math.max(0, ...series.map((values) => values.length));
  const totals = new Array<number>(length).fill(0);
  return series.map((values) =>
    Array.from({ length }, (_, index) => {
      const start = totals[index];
      totals[index] = start + Math.max(0, values[index] ?? 0);
      return [start, totals[index]] as [number, number];
    }),
  );
}

/** The largest value across series (stacked: the largest column total). */
export function maxValue(series: (number | null)[][], stacked = false): number {
  if (stacked) {
    const length = Math.max(0, ...series.map((values) => values.length));
    let max = 0;
    for (let index = 0; index < length; index += 1) {
      max = Math.max(max, series.reduce((sum, values) => sum + Math.max(0, values[index] ?? 0), 0));
    }
    return max;
  }
  return Math.max(0, ...series.flat().map((value) => value ?? 0));
}

/** Whether there is anything at all to draw. */
export function hasData(series: (number | null)[][]): boolean {
  return series.some((values) => values.some((value) => value !== null && value !== undefined && value !== 0));
}

/** Donut arcs: [startAngle, endAngle] in radians from 12 o'clock, clockwise. */
export function arcs(values: number[]): [number, number][] {
  const total = values.reduce((sum, value) => sum + Math.max(0, value), 0);
  let angle = 0;
  return values.map((value) => {
    const sweep = total > 0 ? (Math.max(0, value) / total) * Math.PI * 2 : 0;
    const arc: [number, number] = [angle, angle + sweep];
    angle += sweep;
    return arc;
  });
}

/** An annular sector path between radii `inner` and `outer`, centred on (cx, cy). */
export function arcPath(cx: number, cy: number, inner: number, outer: number, start: number, end: number): string {
  // A full circle cannot be one arc command; draw it as two halves.
  if (end - start >= Math.PI * 2 - 1e-6) {
    return (
      arcPath(cx, cy, inner, outer, start, start + Math.PI) +
      arcPath(cx, cy, inner, outer, start + Math.PI, start + Math.PI * 2)
    );
  }
  const point = (radius: number, angle: number) =>
    `${fixed(cx + radius * Math.sin(angle))},${fixed(cy - radius * Math.cos(angle))}`;
  const large = end - start > Math.PI ? 1 : 0;
  return (
    `M${point(outer, start)}A${outer},${outer} 0 ${large} 1 ${point(outer, end)}` +
    `L${point(inner, end)}A${inner},${inner} 0 ${large} 0 ${point(inner, start)}Z`
  );
}
