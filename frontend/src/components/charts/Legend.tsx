import type { ReactNode } from "react";

import { colorClass, type ChartColor } from "./common";

export interface LegendItem {
  key: string;
  label: ReactNode;
  color?: ChartColor;
  /** The item's position among its series - its default colour slot. */
  index?: number;
  /** A value printed beside the label, e.g. a total or a share. */
  value?: ReactNode;
}

/**
 * Names each series beside a swatch of its colour, so no colour carries
 * meaning on its own. Text stays in text colours; only the swatch is tinted.
 */
export function Legend({ items, className = "" }: { items: LegendItem[]; className?: string }) {
  return (
    <ul className={`chart-legend ${className}`.trim()}>
      {items.map((item, position) => (
        <li key={item.key} className="chart-legend-item">
          <span className={`chart-swatch ${colorClass(item.color, item.index ?? position)}`} aria-hidden="true" />
          <span className="chart-legend-label">{item.label}</span>
          {item.value !== undefined && item.value !== null ? (
            <span className="chart-legend-value">{item.value}</span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
