import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { formatNumber, formatPercent } from "../../lib/format";
import { Icon, type IconName } from "../icons";
import type { ChartColor, ValueFormat } from "./common";
import { Sparkline } from "./Sparkline";

/**
 * How a change is printed: a relative change for counts and sizes
 * ("+20%"), percentage points for a rate ("+3.0 pts" - a rate going from
 * 40% to 50% did not rise 25%), or the plain difference.
 */
export type DeltaStyle = "percent" | "points" | "absolute";

/** Which direction is good news; "neutral" colours neither way. */
export type GoodDirection = "up" | "down" | "neutral";

export interface KpiDelta {
  value: number | null;
  previous: number | null;
  style?: DeltaStyle;
  good?: GoodDirection;
  /** E.g. "vs previous 30 days". */
  period?: string;
}

/** The printed change, its direction, and its tone. Exported for tests. */
export function describeDelta({ value, previous, style = "percent", good = "up" }: KpiDelta, format: ValueFormat = formatNumber) {
  if (value === null || previous === null) return { text: "No comparison", direction: 0, tone: "neutral" as const };
  const difference = value - previous;
  const direction = Math.sign(Number(difference.toFixed(10)));
  let text: string;
  if (style === "points") {
    text = `${difference >= 0 ? "+" : "−"}${Math.abs(difference * 100).toFixed(1)} pts`;
  } else if (style === "absolute") {
    text = `${difference >= 0 ? "+" : "−"}${format(Math.abs(difference))}`;
  } else if (previous === 0) {
    text = value === 0 ? "No change" : "New";
  } else {
    const change = difference / previous;
    text = `${change >= 0 ? "+" : "−"}${formatPercent(Math.abs(change), Math.abs(change) < 0.1 ? 1 : 0)}`;
  }
  if (direction === 0) text = "No change";
  const tone =
    direction === 0 || good === "neutral"
      ? ("neutral" as const)
      : (direction > 0) === (good === "up")
        ? ("good" as const)
        : ("bad" as const);
  return { text, direction, tone };
}

/**
 * A headline number: label, value, change against the previous period
 * (coloured by whether that direction is good), and a sparkline. While
 * `loading` it holds its exact size with placeholders, so nothing jumps.
 *
 * <KpiTile label="Questions" value={120} delta={{value: 120, previous: 100}} series={daily} />
 */
export function KpiTile({
  label,
  value,
  format = formatNumber,
  delta,
  series,
  color = 1,
  icon,
  caption,
  href,
  loading = false,
}: {
  label: string;
  /** null prints as an em dash: nothing to measure yet. */
  value: number | null | undefined;
  format?: ValueFormat;
  delta?: KpiDelta;
  series?: (number | null)[];
  color?: ChartColor;
  icon?: IconName;
  caption?: ReactNode;
  /** The tile becomes a link to the detail behind the number. */
  href?: string;
  loading?: boolean;
}) {
  const change = delta ? describeDelta(delta, format) : null;
  const body = (
    <>
      <div className="kpi-head">
        <span className="kpi-label">{label}</span>
        {icon ? (
          <span className="kpi-icon" aria-hidden="true">
            <Icon name={icon} size={16} />
          </span>
        ) : null}
      </div>
      {loading ? (
        <>
          <span className="kpi-value kpi-skeleton" aria-hidden="true" />
          <span className="kpi-delta kpi-skeleton kpi-skeleton-short" aria-hidden="true" />
          <span className="kpi-spark kpi-skeleton" aria-hidden="true" />
          <span className="visually-hidden">Loading {label}</span>
        </>
      ) : (
        <>
          <span className="kpi-value">{value === null || value === undefined ? "—" : format(value)}</span>
          {change ? (
            <span className={`kpi-delta kpi-delta-${change.tone}`}>
              {change.direction !== 0 ? (
                <Icon name={change.direction > 0 ? "trending-up" : "trending-down"} size={14} />
              ) : null}
              <span>{change.text}</span>
              {delta?.period ? <span className="kpi-period">{delta.period}</span> : null}
            </span>
          ) : null}
          {caption ? <span className="kpi-caption">{caption}</span> : null}
          {series ? (
            <span className="kpi-spark">
              <Sparkline values={series} label={`${label}, day by day`} color={color} height={30} />
            </span>
          ) : null}
        </>
      )}
    </>
  );
  return href && !loading ? (
    <Link to={href} className="kpi kpi-link" aria-busy={loading || undefined}>
      {body}
    </Link>
  ) : (
    <div className="kpi" aria-busy={loading || undefined}>
      {body}
    </div>
  );
}
