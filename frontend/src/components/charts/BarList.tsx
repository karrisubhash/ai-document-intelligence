import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { formatNumber } from "../../lib/format";
import { ChartEmpty, colorClass, type ChartColor, type ValueFormat } from "./common";

export interface BarListItem {
  key: string;
  label: ReactNode;
  value: number;
  /** An in-app path; the label becomes a link. */
  href?: string;
  /** A second line under the label, e.g. "3 readers". */
  hint?: ReactNode;
  /** A tooltip for the label, e.g. a longer explanation. */
  title?: string;
}

/**
 * A ranked list with a bar behind each value - "top documents", "failures
 * by reason". Plain HTML (an ordered list), so it reads, wraps and links
 * like text; the bars are proportional to the largest value (or `max`).
 *
 * <BarList title="Most cited" items={[{key, label, value, href}]} />
 */
export function BarList({
  title,
  items,
  format = formatNumber,
  max,
  color = 1,
  loading = false,
  empty,
  rows = 5,
}: {
  /** The list's accessible name. */
  title: string;
  items: BarListItem[];
  format?: ValueFormat;
  max?: number;
  color?: ChartColor;
  loading?: boolean;
  empty?: ReactNode;
  /** Placeholder rows while loading. */
  rows?: number;
}) {
  if (loading) {
    return (
      <ol className="barlist" aria-label={title} aria-busy="true">
        {Array.from({ length: rows }, (_, index) => (
          <li key={index} className="barlist-row barlist-row-loading">
            <span className="barlist-bar barlist-skeleton" style={{ width: `${85 - index * 12}%` }} />
          </li>
        ))}
      </ol>
    );
  }
  if (!items.length) return <ChartEmpty height={rows * 36}>{empty}</ChartEmpty>;
  const top = max ?? Math.max(...items.map((item) => item.value), 0);
  return (
    <ol className={`barlist ${colorClass(color, 0)}`} aria-label={title}>
      {items.map((item) => {
        const share = top > 0 ? Math.max(0, Math.min(1, item.value / top)) : 0;
        return (
          <li key={item.key} className="barlist-row">
            <span className="barlist-bar" style={{ width: `${(share * 100).toFixed(1)}%` }} aria-hidden="true" />
            <span className="barlist-label" title={item.title}>
              {item.href ? (
                <Link to={item.href} className="barlist-link">
                  {item.label}
                </Link>
              ) : (
                item.label
              )}
              {item.hint ? <span className="barlist-hint">{item.hint}</span> : null}
            </span>
            <span className="barlist-value">{format(item.value)}</span>
          </li>
        );
      })}
    </ol>
  );
}
