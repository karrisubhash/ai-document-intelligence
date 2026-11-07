import { Cartesian, type CartesianProps } from "./Cartesian";
import { colorClass } from "./common";
import { stack } from "./scale";

/** A bar's outline with only its data end (the top) rounded. */
export function barPath(x: number, y: number, width: number, height: number, radius = 4): string {
  if (height <= 0 || width <= 0) return "";
  const r = Math.max(0, Math.min(radius, width / 2, height));
  const f = (value: number) => Number(value.toFixed(2));
  return (
    `M${f(x)},${f(y + height)}V${f(y + r)}Q${f(x)},${f(y)} ${f(x + r)},${f(y)}` +
    `H${f(x + width - r)}Q${f(x + width)},${f(y)} ${f(x + width)},${f(y + r)}` +
    `V${f(y + height)}Z`
  );
}

// The surface-coloured space between stacked segments, in pixels.
const GAP = 2;

/**
 * Vertical bars per label: one series, or several `stacked` (parts of a
 * whole per day) or grouped side by side (`grouped`, for comparing them).
 * Bars stay at least a pixel wide, so 90 days still read as a shape.
 *
 * <BarChart stacked title="Questions" labels={days} series={[answered, unanswered]} />
 */
export function BarChart({
  stacked = false,
  grouped = false,
  ...props
}: CartesianProps & { stacked?: boolean; grouped?: boolean }) {
  const many = props.series.length > 1;
  const mode = many && stacked ? "stacked" : many && grouped ? "grouped" : "overlay";
  return (
    <Cartesian
      {...props}
      layout="band"
      stacked={mode === "stacked"}
      totalRow={mode === "stacked"}
      renderMarks={({ y, bandX, bandWidth, active }) => {
        if (mode === "stacked") {
          const stacks = stack(props.series.map((item) => item.values));
          // The topmost non-empty segment of each column gets the rounded end.
          const topmost = props.labels.map((_, index) => {
            let top = -1;
            stacks.forEach((spans, position) => {
              if (spans[index][1] > spans[index][0]) top = position;
            });
            return top;
          });
          return props.series.map((item, position) => (
            <g key={item.key} className={colorClass(item.color, position)}>
              {stacks[position].map(([start, end], index) => {
                if (end <= start) return null;
                const top = y(end);
                const height = y(start) - top - (start > 0 ? GAP : 0);
                return (
                  <path
                    key={index}
                    className={`chart-bar${active === index ? " chart-bar-active" : ""}`}
                    d={barPath(bandX(index), top, bandWidth, Math.max(1, height), topmost[index] === position ? 4 : 0)}
                  />
                );
              })}
            </g>
          ));
        }
        const slots = mode === "grouped" ? props.series.length : 1;
        const slotWidth = mode === "grouped" ? Math.max(1, (bandWidth - GAP * (slots - 1)) / slots) : bandWidth;
        return props.series.map((item, position) => (
          <g key={item.key} className={colorClass(item.color, position)}>
            {item.values.map((value, index) => {
              if (value === null || value === undefined || value <= 0) return null;
              const left = bandX(index) + (mode === "grouped" ? position * (slotWidth + GAP) : 0);
              const top = y(value);
              return (
                <path
                  key={index}
                  className={`chart-bar${active === index ? " chart-bar-active" : ""}`}
                  d={barPath(left, top, slotWidth, Math.max(1, y(0) - top))}
                />
              );
            })}
          </g>
        ));
      }}
    />
  );
}
