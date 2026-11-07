import { useState, type ReactNode } from "react";

import { formatNumber, formatPercent } from "../../lib/format";
import { ChartEmpty, ChartSkeleton, DataTable, colorClass, useChartIds, type ChartColor, type ValueFormat } from "./common";
import { Legend } from "./Legend";
import { arcPath, arcs } from "./scale";

export interface PartItem {
  key: string;
  label: string;
  value: number;
  color?: ChartColor;
}

function shares(items: PartItem[]) {
  const total = items.reduce((sum, item) => sum + Math.max(0, item.value), 0);
  return { total, share: (value: number) => (total > 0 ? Math.max(0, value) / total : 0) };
}

/** The legend under a part-to-whole chart: every part with value and share. */
function PartLegend({ items, format }: { items: PartItem[]; format: ValueFormat }) {
  const { share } = shares(items);
  return (
    <Legend
      className="chart-legend-stacked"
      items={items.map((item, index) => ({
        key: item.key,
        label: item.label,
        color: item.color,
        index,
        value: (
          <>
            {format(item.value)} <span className="chart-legend-share">{formatPercent(share(item.value))}</span>
          </>
        ),
      }))}
    />
  );
}

/**
 * Parts of one whole as a ring, the total in the middle, every part named
 * with its value and share beside it. Best for two to six parts; past
 * that, fold the tail into "Other" or use a BarList.
 *
 * <Donut title="Documents by type" items={[{key, label, value}]} centerLabel="documents" />
 */
export function Donut({
  title,
  items,
  format = formatNumber,
  centerLabel,
  size = 148,
  loading = false,
  empty,
}: {
  title: string;
  items: PartItem[];
  format?: ValueFormat;
  /** Under the total in the middle, e.g. "documents". */
  centerLabel?: string;
  size?: number;
  loading?: boolean;
  empty?: ReactNode;
}) {
  const ids = useChartIds();
  const [hover, setHover] = useState<string | null>(null);
  if (loading) return <ChartSkeleton height={size} />;
  const { total, share } = shares(items);
  if (total <= 0) return <ChartEmpty height={size}>{empty}</ChartEmpty>;

  const outer = size / 2;
  const inner = outer * 0.64;
  const visible = items.filter((item) => item.value > 0).length;
  // A sliver of surface between parts; none around a single whole.
  const pad = visible > 1 ? 0.012 : 0;
  const spans = arcs(items.map((item) => item.value));
  const focused = items.find((item) => item.key === hover);

  return (
    <div className="donut">
      <div className="donut-figure" style={{ width: `${size}px`, height: `${size}px` }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-labelledby={ids.title} aria-describedby={ids.desc}>
          <title id={ids.title}>{title}</title>
          <desc id={ids.desc}>
            {items.map((item) => `${item.label}: ${format(item.value)} (${formatPercent(share(item.value))})`).join("; ")}
          </desc>
          {items.map((item, index) => {
            const [start, end] = spans[index];
            if (end - start <= 0) return null;
            return (
              <path
                key={item.key}
                className={`donut-arc ${colorClass(item.color, index)}${hover && hover !== item.key ? " donut-arc-dim" : ""}`}
                d={arcPath(outer, outer, inner, outer - 1, start + pad / 2, Math.max(start + pad / 2, end - pad / 2))}
                onPointerEnter={() => setHover(item.key)}
                onPointerLeave={() => setHover(null)}
              />
            );
          })}
        </svg>
        <div className="donut-center" aria-hidden="true">
          <span className="donut-value">{format(focused ? focused.value : total)}</span>
          <span className="donut-label">{focused ? focused.label : centerLabel}</span>
        </div>
      </div>
      <PartLegend items={items} format={format} />
      <DataTable
        caption={title}
        columns={["Part", "Value", "Share"]}
        rows={items.map((item) => ({
          key: item.key,
          cells: [item.label, format(item.value), formatPercent(share(item.value))],
        }))}
      />
    </div>
  );
}

/**
 * Parts of one whole as a single horizontal bar - compact enough for a
 * card header or a table row - with the named parts listed under it.
 *
 * <SegmentedBar title="Library health" items={[{key: "ready", label: "Ready", value: 40, color: "success"}]} />
 */
export function SegmentedBar({
  title,
  items,
  format = formatNumber,
  legend = true,
  loading = false,
}: {
  title: string;
  items: PartItem[];
  format?: ValueFormat;
  legend?: boolean;
  loading?: boolean;
}) {
  if (loading) return <div className="segbar segbar-loading" aria-hidden="true" />;
  const { total, share } = shares(items);
  const summary = total
    ? items.map((item) => `${item.label} ${format(item.value)} (${formatPercent(share(item.value))})`).join(", ")
    : "nothing yet";
  return (
    <div className="segbar-wrap">
      <div className={`segbar${total ? "" : " segbar-empty"}`} role="img" aria-label={`${title}: ${summary}`}>
        {items.map((item, index) =>
          item.value > 0 ? (
            <span
              key={item.key}
              className={`segbar-part ${colorClass(item.color, index)}`}
              style={{ width: `${(share(item.value) * 100).toFixed(2)}%` }}
              title={`${item.label}: ${format(item.value)}`}
            />
          ) : null,
        )}
      </div>
      {legend ? <PartLegend items={items} format={format} /> : null}
    </div>
  );
}
