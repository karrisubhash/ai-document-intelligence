/**
 * What every chart in the kit shares: series colours, measuring the width,
 * the tooltip, keyboard movement between data points, the screen-reader
 * table, and the loading and empty states.
 *
 * Nothing here writes a <style> element or an innerHTML: colours are CSS
 * classes over the tokens in src/styles/charts.css, geometry is SVG
 * attributes, and positions are React style props (applied through the
 * CSSOM, which the production style-src 'self' allows).
 */
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import "../../styles/charts.css";

/**
 * A series colour: a categorical slot 1-8 (always given in order - slot 1
 * for the first series, 2 for the second; never chosen for the look), or a
 * status for data whose meaning IS a status, such as failed runs. Status
 * colours never carry meaning alone - a legend or label names them.
 */
export type ChartColor = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | "success" | "warning" | "danger" | "info" | "muted";

export function colorClass(color: ChartColor | undefined, index: number): string {
  return `chart-c-${color ?? (index % 8) + 1}`;
}

export interface Series {
  /** Stable identity: React key, and what the colour follows. */
  key: string;
  label: string;
  /** One value per label; null is "nothing to measure" (a gap), not zero. */
  values: (number | null)[];
  color?: ChartColor;
}

/** How a value is printed in tooltips, tables and labels. */
export type ValueFormat = (value: number) => string;

/**
 * The element's width, kept up to date by a ResizeObserver. Charts reserve
 * their height up front, so measuring the width never shifts the page.
 * Where ResizeObserver does not exist (tests), `fallback` is used.
 */
export function useChartWidth<T extends HTMLElement>(fallback = 600) {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (typeof ResizeObserver !== "function") {
      setWidth(element.clientWidth || fallback);
      return;
    }
    const measure = () => setWidth(Math.floor(element.clientWidth));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [fallback]);
  return [ref, width] as const;
}

/**
 * Keyboard and pointer access to the points of an index-based chart (one
 * index per label). Arrow keys move, Home/End jump, Escape lets go. Moves
 * made with the keyboard are announced through `announcement`; pointer
 * moves are not, which would talk over everything else.
 */
export function useActiveIndex(count: number) {
  const [active, setActive] = useState<number | null>(null);
  const [announce, setAnnounce] = useState(false);

  useEffect(() => {
    // The data changed under the cursor (another range): let go.
    setActive((current) => (current !== null && current >= count ? null : current));
  }, [count]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      if (!count) return;
      let next: number | null;
      // Up and Down are left to scroll the page.
      if (event.key === "ArrowRight") next = active === null ? count - 1 : Math.min(count - 1, active + 1);
      else if (event.key === "ArrowLeft") next = active === null ? count - 1 : Math.max(0, active - 1);
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = count - 1;
      else if (event.key === "Escape") next = null;
      else return;
      event.preventDefault();
      setAnnounce(true);
      setActive(next);
    },
    [active, count],
  );

  return {
    active,
    announce,
    onKeyDown,
    onFocus: () => {
      setAnnounce(true);
      setActive((current) => current ?? (count ? count - 1 : null));
    },
    onBlur: () => setActive(null),
    point: (index: number | null) => {
      setAnnounce(false);
      setActive(index);
    },
  };
}

export interface TooltipRow {
  key: string;
  label: string;
  value: string;
  colorClass?: string;
}

/** The floating box beside the active point. Hidden from assistive
 *  technology, which hears the live region instead. */
export function ChartTooltip({
  title,
  rows,
  x,
  width,
}: {
  title: string;
  rows: TooltipRow[];
  /** The point's x in the chart, in pixels. */
  x: number;
  /** The chart's width, to keep the box inside it. */
  width: number;
}) {
  // Flip to the left of the point past the middle, so it never overflows.
  const left = x > width / 2;
  return (
    <div
      className={`chart-tooltip${left ? " chart-tooltip-left" : ""}`}
      style={{ left: `${Math.round(x)}px` }}
      aria-hidden="true"
    >
      <div className="chart-tooltip-title">{title}</div>
      {rows.map((row) => (
        <div key={row.key} className="chart-tooltip-row">
          {row.colorClass ? <span className={`chart-swatch ${row.colorClass}`} /> : null}
          <span className="chart-tooltip-label">{row.label}</span>
          <span className="chart-tooltip-value">{row.value}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * The chart's numbers as a table, for screen readers (visually hidden) or
 * for everyone (`visible`). A picture is not the only way to the data.
 */
export function DataTable({
  caption,
  columns,
  rows,
  visible = false,
}: {
  caption: string;
  /** Header of the first column, then one per value column. */
  columns: string[];
  rows: { key: string; cells: string[] }[];
  visible?: boolean;
}) {
  return (
    <table className={visible ? "chart-table" : "visually-hidden"}>
      <caption>{caption}</caption>
      <thead>
        <tr>
          {columns.map((column, index) => (
            <th key={column + index} scope="col">
              {column}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key}>
            {row.cells.map((cell, index) =>
              index === 0 ? (
                <th key={index} scope="row">
                  {cell}
                </th>
              ) : (
                <td key={index}>{cell}</td>
              ),
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** A placeholder the size of the chart while its data loads. */
export function ChartSkeleton({ height }: { height: number }) {
  return <div className="chart-skeleton" style={{ height: `${height}px` }} aria-hidden="true" />;
}

/** Shown in place of the plot when there is nothing to plot. */
export function ChartEmpty({ height, children }: { height: number; children?: ReactNode }) {
  return (
    <div className="chart-empty" style={{ minHeight: `${height}px` }}>
      <svg className="chart-empty-art" viewBox="0 0 64 32" aria-hidden="true">
        <path d="M2 28 L18 20 L30 24 L46 10 L62 14" />
        <line x1="2" y1="30" x2="62" y2="30" />
      </svg>
      <p>{children ?? "Nothing to show for this period yet."}</p>
    </div>
  );
}

/** A stable id for aria-labelledby / aria-describedby wiring. */
export function useChartIds() {
  const id = useId();
  return { title: `${id}-title`, desc: `${id}-desc`, live: `${id}-live` };
}
