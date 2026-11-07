import { Cartesian, type CartesianProps } from "./Cartesian";
import { colorClass } from "./common";
import { areaPath, isolatedPoints, linePath, segments } from "./scale";

/**
 * Lines over time (or over categories), one per series, with an optional
 * soft area under each. A null value breaks the line - a day with nothing
 * to measure is shown as a gap, never as a fall to zero - and a point
 * standing alone between gaps is drawn as a dot.
 *
 * <LineChart title="Answer time" labels={days} series={[{key: "p50", label: "Median", values}]} />
 */
export function LineChart({ area = false, ...props }: CartesianProps & { area?: boolean }) {
  return (
    <Cartesian
      {...props}
      layout="point"
      renderMarks={({ x, y, baseline, active }) =>
        props.series.map((item, index) => {
          const runs = segments(item.values, x, y);
          const tone = colorClass(item.color, index);
          const value = active === null ? null : item.values[active];
          return (
            <g key={item.key} className={tone}>
              {area ? <path className="chart-area" d={areaPath(runs, baseline)} /> : null}
              <path className="chart-line" d={linePath(runs)} />
              {isolatedPoints(runs).map(([px, py]) => (
                <circle key={`${px}-${py}`} className="chart-dot" cx={px} cy={py} r={3} />
              ))}
              {active !== null && value !== null && value !== undefined ? (
                <circle className="chart-dot chart-dot-active" cx={x(active)} cy={y(value)} r={4.5} />
              ) : null}
            </g>
          );
        })
      }
    />
  );
}
