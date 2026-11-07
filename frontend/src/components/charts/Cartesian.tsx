/**
 * The frame LineChart and BarChart draw into: y axis with nice ticks and
 * recessive gridlines, an x axis of labels thinned to fit, a hover and
 * keyboard cursor with a tooltip, a live region, and the data table.
 * Internal to the kit - use LineChart or BarChart.
 */
import type { ReactNode } from "react";

import { formatCompact, formatLongDay, formatNumber, formatShortDay } from "../../lib/format";
import {
  ChartEmpty,
  ChartSkeleton,
  ChartTooltip,
  DataTable,
  colorClass,
  useActiveIndex,
  useChartIds,
  useChartWidth,
  type Series,
  type ValueFormat,
} from "./common";
import { Legend } from "./Legend";
import { bandScale, hasData, indexAt, labelEvery, labelledIndices, linearScale, maxValue, niceTicks } from "./scale";

export interface CartesianProps {
  /** What the chart shows. Its accessible name and the table's caption. */
  title: string;
  /** A sentence for screen readers; a date range is generated without it. */
  description?: string;
  /** The x values: "YYYY-MM-DD" days (labelType "date") or category names. */
  labels: string[];
  labelType?: "date" | "category";
  series: Series[];
  /** Pixels. Reserved before the width is known, so nothing shifts. */
  height?: number;
  /** Fix the top of the y axis, e.g. 1 for a rate. Default: the data's. */
  yMax?: number;
  /** Counts: ticks are whole numbers. */
  integer?: boolean;
  /** Values in tooltips and the table. Default: 1,234. */
  format?: ValueFormat;
  /** Values on the y axis. Default: compact (1.2K), or `format`. */
  axisFormat?: ValueFormat;
  loading?: boolean;
  /** Shown when every value is zero or missing. */
  empty?: ReactNode;
  /** Default: shown for two series or more. */
  legend?: boolean;
  /** Show the data table to everyone, under the chart. */
  showTable?: boolean;
}

export interface MarkContext {
  x: (index: number) => number;
  y: (value: number) => number;
  /** Bars: the band width; lines: the step between points. */
  bandWidth: number;
  bandX: (index: number) => number;
  baseline: number;
  top: number;
  active: number | null;
}

const MARGIN = { top: 10, right: 12, bottom: 26 };

export function Cartesian({
  title,
  description,
  labels,
  labelType = "date",
  series,
  height = 220,
  yMax,
  integer = false,
  format = formatNumber,
  axisFormat,
  loading = false,
  empty,
  legend,
  showTable = false,
  layout,
  stacked = false,
  renderMarks,
  totalRow = false,
}: CartesianProps & {
  /** "band": bars centred in equal slots; "point": first and last at the edges. */
  layout: "band" | "point";
  stacked?: boolean;
  renderMarks: (context: MarkContext) => ReactNode;
  /** Add a Total row to the tooltip (stacked bars). */
  totalRow?: boolean;
}) {
  const [ref, width] = useChartWidth<HTMLDivElement>();
  const ids = useChartIds();
  const cursor = useActiveIndex(labels.length);
  const short = (label: string) => (labelType === "date" ? formatShortDay(label) : label);
  const long = (label: string) => (labelType === "date" ? formatLongDay(label) : label);
  const values = series.map((item) => item.values);

  const ticks = niceTicks(0, yMax ?? maxValue(values, stacked), 4, integer);
  const tickText = ticks.map((tick) => (axisFormat ?? (format === formatNumber ? formatCompact : format))(tick));
  // Room for the widest tick label, at ~7px a character in 11px figures.
  const left = Math.max(24, Math.max(...tickText.map((text) => text.length)) * 7 + 10);
  const plotWidth = Math.max(0, width - left - MARGIN.right);
  const baseline = height - MARGIN.bottom;
  const y = linearScale([ticks[0], ticks[ticks.length - 1]], [baseline, MARGIN.top]);
  const count = labels.length;

  const band = bandScale(count, plotWidth, layout === "band" ? 0.28 : 0);
  const pointStep = count > 1 ? plotWidth / (count - 1) : plotWidth;
  const x =
    layout === "band"
      ? (index: number) => left + band.center(index)
      : (index: number) => left + (count > 1 ? index * pointStep : plotWidth / 2);
  const hoverIndex = (px: number) =>
    layout === "band"
      ? indexAt(px - left, count, plotWidth)
      : Math.min(count - 1, Math.max(0, Math.round((px - left) / (pointStep || 1))));

  const every = labelEvery(count, plotWidth, labelType === "date" ? 58 : 72);
  const xLabels = labelledIndices(count, every);
  const active = cursor.active;

  const rowsFor = (index: number) => {
    const rows = series.map((item, position) => ({
      key: item.key,
      label: item.label,
      value: item.values[index] === null || item.values[index] === undefined ? "—" : format(item.values[index] as number),
      colorClass: colorClass(item.color, position),
    }));
    if (totalRow && series.length > 1) {
      const total = series.reduce((sum, item) => sum + (item.values[index] ?? 0), 0);
      rows.push({ key: "__total", label: "Total", value: format(total), colorClass: "" });
    }
    return rows;
  };
  const sentence = (index: number) =>
    `${long(labels[index])}: ${rowsFor(index)
      .map((row) => `${row.label} ${row.value}`)
      .join(", ")}`;

  const summary =
    description ??
    (count
      ? `${title}, ${long(labels[0])} to ${long(labels[count - 1])}. Use the arrow keys to read each point.`
      : title);

  const showLegend = legend ?? series.length > 1;
  const nothing = !loading && !hasData(values);

  return (
    <div className="chart">
      {showLegend && !loading ? (
        <Legend items={series.map((item, index) => ({ key: item.key, label: item.label, color: item.color, index }))} />
      ) : null}
      <div className="chart-body" ref={ref} style={{ height: `${height}px` }}>
        {loading ? (
          <ChartSkeleton height={height} />
        ) : nothing ? (
          <ChartEmpty height={height}>{empty}</ChartEmpty>
        ) : width > 0 ? (
          <div
            className="chart-plot"
            tabIndex={0}
            role="group"
            aria-label={title}
            aria-describedby={ids.desc}
            onKeyDown={cursor.onKeyDown}
            onFocus={cursor.onFocus}
            onBlur={cursor.onBlur}
          >
            <svg
              className="chart-svg"
              width={width}
              height={height}
              viewBox={`0 0 ${width} ${height}`}
              role="img"
              aria-labelledby={ids.title}
              onPointerMove={(event) => {
                const box = event.currentTarget.getBoundingClientRect();
                cursor.point(hoverIndex(event.clientX - box.left));
              }}
              onPointerLeave={() => cursor.point(null)}
            >
              <title id={ids.title}>{title}</title>
              <desc id={ids.desc}>{summary}</desc>
              <g className="chart-grid" aria-hidden="true">
                {ticks.map((tick, index) => (
                  <g key={tick}>
                    <line x1={left} x2={left + plotWidth} y1={y(tick)} y2={y(tick)} className={index === 0 ? "chart-baseline" : undefined} />
                    <text x={left - 8} y={y(tick)} dy="0.32em" textAnchor="end" className="chart-axis-text">
                      {tickText[index]}
                    </text>
                  </g>
                ))}
              </g>
              <g className="chart-x-axis" aria-hidden="true">
                {xLabels.map((index) => (
                  <text
                    key={index}
                    x={x(index)}
                    y={baseline + 17}
                    textAnchor={
                      layout === "point" && index === 0 ? "start" : layout === "point" && index === count - 1 ? "end" : "middle"
                    }
                    className="chart-axis-text"
                  >
                    {short(labels[index])}
                  </text>
                ))}
              </g>
              {active !== null && layout === "band" ? (
                <rect
                  className="chart-hover-band"
                  x={left + active * band.step}
                  y={MARGIN.top}
                  width={band.step}
                  height={baseline - MARGIN.top}
                  aria-hidden="true"
                />
              ) : null}
              <g aria-hidden="true">
                {renderMarks({
                  x,
                  y,
                  bandWidth: layout === "band" ? band.width : pointStep,
                  bandX: (index) => left + band.x(index),
                  baseline,
                  top: MARGIN.top,
                  active,
                })}
              </g>
            </svg>
            {active !== null && labels[active] !== undefined ? (
              <ChartTooltip title={long(labels[active])} rows={rowsFor(active)} x={x(active)} width={width} />
            ) : null}
            <div className="visually-hidden" aria-live="polite" id={ids.live}>
              {cursor.announce && active !== null && labels[active] !== undefined ? sentence(active) : ""}
            </div>
          </div>
        ) : null}
      </div>
      {!loading ? (
        <DataTable
          caption={title}
          visible={showTable}
          columns={[labelType === "date" ? "Date" : "Item", ...series.map((item) => item.label)]}
          rows={labels.map((label, index) => ({
            key: label + index,
            cells: [long(label), ...series.map((item) => (item.values[index] === null || item.values[index] === undefined ? "—" : format(item.values[index] as number)))],
          }))}
        />
      ) : null}
    </div>
  );
}
