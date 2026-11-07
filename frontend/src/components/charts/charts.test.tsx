import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { formatShortDay, parseLocalDate } from "../../lib/format";
import { BarChart, BarList, Donut, KpiTile, LineChart, SegmentedBar, Sparkline, describeDelta, scales } from ".";

const { arcs, isolatedPoints, labelEvery, labelledIndices, linePath, niceTicks, segments, stack, maxValue, bandScale, indexAt } =
  scales;

describe("scales", () => {
  it("cuts a range into round ticks that cover it", () => {
    expect(niceTicks(0, 13)).toEqual([0, 5, 10, 15]);
    expect(niceTicks(0, 100)).toEqual([0, 20, 40, 60, 80, 100]);
    expect(niceTicks(0, 0.83)).toEqual([0, 0.2, 0.4, 0.6, 0.8, 1]);
  });

  it("gives an empty or flat series room instead of a zero-height axis", () => {
    expect(niceTicks(0, 0)[0]).toBe(0);
    expect(niceTicks(0, 0).at(-1)).toBe(1);
    expect(niceTicks(0, 0, 4, true)).toEqual([0, 1]);
    expect(niceTicks(5, 5)[0]).toBe(0);
    expect(niceTicks(5, 5).at(-1)).toBeGreaterThanOrEqual(5);
  });

  it("keeps count ticks whole", () => {
    expect(niceTicks(0, 2, 4, true)).toEqual([0, 1, 2]);
    expect(niceTicks(0, 3, 4, true).every(Number.isInteger)).toBe(true);
  });

  it("breaks a line at a gap and keeps a lone point", () => {
    const runs = segments([1, null, 2, 3, null], (i) => i * 10, (v) => v);
    expect(runs).toEqual([[[0, 1]], [[20, 2], [30, 3]]]);
    expect(isolatedPoints(runs)).toEqual([[0, 1]]);
    expect(linePath(runs)).toBe("M20,2L30,3");
  });

  it("never treats a gap as zero", () => {
    const runs = segments([null, null], (i) => i, (v) => v);
    expect(runs).toEqual([]);
    expect(linePath(runs)).toBe("");
  });

  it("stacks series, a missing value as nothing", () => {
    expect(stack([[1, 2], [3, null]])).toEqual([
      [[0, 1], [0, 2]],
      [[1, 4], [2, 2]],
    ]);
    expect(maxValue([[1, 2], [3, null]], true)).toBe(4);
    expect(maxValue([[1, 2], [3, null]])).toBe(3);
  });

  it("thins labels and always keeps the latest", () => {
    expect(labelEvery(90, 300)).toBe(17);
    expect(labelEvery(7, 700)).toBe(1);
    const indices = labelledIndices(30, 7);
    expect(indices.at(-1)).toBe(29);
    expect(indices[1] - indices[0]).toBe(7);
  });

  it("maps a pointer to its band", () => {
    const band = bandScale(10, 200);
    expect(band.step).toBe(20);
    expect(band.width).toBeGreaterThanOrEqual(1);
    expect(indexAt(0, 10, 200)).toBe(0);
    expect(indexAt(199, 10, 200)).toBe(9);
    expect(indexAt(999, 10, 200)).toBe(9);
  });

  it("splits a circle in proportion", () => {
    const spans = arcs([1, 3, 0]);
    expect(spans[0][1]).toBeCloseTo(Math.PI / 2);
    expect(spans[1][1]).toBeCloseTo(Math.PI * 2);
    expect(spans[2][1] - spans[2][0]).toBe(0);
  });
});

describe("dates", () => {
  it("reads a day as a local date, not as UTC midnight", () => {
    const day = parseLocalDate("2026-03-01");
    expect(day?.getFullYear()).toBe(2026);
    expect(day?.getMonth()).toBe(2);
    expect(day?.getDate()).toBe(1);
    expect(parseLocalDate("not a day")).toBeNull();
    expect(formatShortDay("2026-03-01")).toMatch(/1/);
  });
});

describe("describeDelta", () => {
  it("prints a count's change as a percentage, coloured by its direction", () => {
    expect(describeDelta({ value: 120, previous: 100 })).toEqual({ text: "+20%", direction: 1, tone: "good" });
    expect(describeDelta({ value: 80, previous: 100, good: "down" }).tone).toBe("good");
    expect(describeDelta({ value: 120, previous: 100, good: "down" }).tone).toBe("bad");
    expect(describeDelta({ value: 5, previous: 0 }).text).toBe("New");
    expect(describeDelta({ value: 5, previous: 5 }).tone).toBe("neutral");
  });

  it("prints a rate's change in points", () => {
    expect(describeDelta({ value: 0.5, previous: 0.4, style: "points" }).text).toBe("+10.0 pts");
    expect(describeDelta({ value: 0.4, previous: 0.5, style: "points" }).text).toBe("−10.0 pts");
  });

  it("says when there is nothing to compare with", () => {
    expect(describeDelta({ value: null, previous: 0.5, style: "points" })).toMatchObject({
      text: "No comparison",
      tone: "neutral",
    });
  });
});

const DAYS = ["2026-11-01", "2026-11-02", "2026-11-03"];

describe("LineChart", () => {
  it("is a named, described image with its data in a table", () => {
    render(
      <LineChart
        title="Answer time"
        labels={DAYS}
        series={[
          { key: "p50", label: "Median", values: [100, null, 300] },
          { key: "p95", label: "Slowest 5%", values: [200, null, 900] },
        ]}
      />,
    );
    expect(screen.getByRole("img", { name: "Answer time" })).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Answer time" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(4);
    // The gap is a dash, not a zero.
    expect(within(rows[2]).getAllByRole("cell")[0]).toHaveTextContent("—");
    // Two series: named in a legend.
    expect(screen.getByText("Slowest 5%", { selector: ".chart-legend-label" })).toBeInTheDocument();
  });

  it("can be read point by point from the keyboard", () => {
    render(<LineChart title="Questions" labels={DAYS} series={[{ key: "q", label: "Questions", values: [1, 2, 3] }]} />);
    const plot = screen.getByRole("group", { name: "Questions" });
    fireEvent.focus(plot);
    const live = plot.querySelector("[aria-live]") as HTMLElement;
    expect(live).toHaveTextContent("Questions 3");
    fireEvent.keyDown(plot, { key: "ArrowLeft" });
    expect(live).toHaveTextContent("Questions 2");
    fireEvent.keyDown(plot, { key: "Home" });
    expect(live).toHaveTextContent("Questions 1");
    fireEvent.keyDown(plot, { key: "Escape" });
    expect(live).toHaveTextContent("");
  });

  it("shows an empty state instead of a flat line", () => {
    render(
      <LineChart
        title="Questions"
        labels={DAYS}
        series={[{ key: "q", label: "Questions", values: [0, 0, null] }]}
        empty="No questions yet."
      />,
    );
    expect(screen.getByText("No questions yet.")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Questions" })).not.toBeInTheDocument();
  });

  it("holds its height while loading", () => {
    const { container } = render(
      <LineChart title="Questions" labels={DAYS} series={[]} loading height={180} />,
    );
    expect(container.querySelector(".chart-body")).toHaveStyle({ height: "180px" });
    expect(container.querySelector(".chart-skeleton")).toBeInTheDocument();
  });
});

describe("BarChart", () => {
  it("stacks series and totals them in the tooltip", () => {
    const { container } = render(
      <BarChart
        stacked
        title="Questions"
        labels={DAYS}
        series={[
          { key: "a", label: "Answered", values: [1, 0, 2] },
          { key: "u", label: "Unanswered", values: [1, 1, 0] },
        ]}
      />,
    );
    // One path per non-empty segment.
    expect(container.querySelectorAll("path.chart-bar")).toHaveLength(4);
    const plot = screen.getByRole("group", { name: "Questions" });
    fireEvent.focus(plot);
    expect(container.querySelector(".chart-tooltip")).toHaveTextContent("Total2");
  });

  it("puts grouped series side by side", () => {
    const { container } = render(
      <BarChart
        grouped
        title="Runs"
        labels={DAYS}
        series={[
          { key: "c", label: "Completed", values: [1, 1, 1] },
          { key: "f", label: "Failed", values: [1, 0, 1], color: "danger" },
        ]}
      />,
    );
    expect(container.querySelectorAll("path.chart-bar")).toHaveLength(5);
    expect(container.querySelector(".chart-c-danger")).toBeInTheDocument();
  });
});

describe("BarList", () => {
  it("ranks items with values and links", () => {
    render(
      <MemoryRouter>
        <BarList
          title="Most cited"
          items={[
            { key: "1", label: "Lab rules", value: 12, href: "/documents/1" },
            { key: "2", label: "Fees", value: 3 },
          ]}
        />
      </MemoryRouter>,
    );
    const list = screen.getByRole("list", { name: "Most cited" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Lab rules" })).toHaveAttribute("href", "/documents/1");
    expect(screen.getByText("12")).toBeInTheDocument();
  });

  it("says when there is nothing to rank", () => {
    render(<BarList title="Most cited" items={[]} empty="Nothing cited yet." />);
    expect(screen.getByText("Nothing cited yet.")).toBeInTheDocument();
  });
});

describe("part to whole", () => {
  const items = [
    { key: "pdf", label: "PDF", value: 3 },
    { key: "txt", label: "Text", value: 1 },
  ];

  it("Donut names every part with its share", () => {
    render(<Donut title="Files" items={items} centerLabel="files" />);
    expect(screen.getByRole("img", { name: "Files" })).toBeInTheDocument();
    // In the visible legend (and again in the screen-reader table).
    expect(screen.getByText("75%", { selector: ".chart-legend-share" })).toBeInTheDocument();
    expect(screen.getByText("4", { selector: ".donut-value" })).toBeInTheDocument();
  });

  it("SegmentedBar describes itself", () => {
    render(<SegmentedBar title="Health" items={items} />);
    expect(screen.getByRole("img", { name: /Health: PDF 3 \(75%\), Text 1 \(25%\)/ })).toBeInTheDocument();
  });
});

describe("KpiTile and Sparkline", () => {
  it("shows the value, the change and the trend", () => {
    const { container } = render(
      <KpiTile label="Questions" value={120} delta={{ value: 120, previous: 100, period: "vs previous 7 days" }} series={[1, 2, 3]} />,
    );
    expect(screen.getByText("120")).toBeInTheDocument();
    expect(screen.getByText("+20%")).toBeInTheDocument();
    expect(container.querySelector(".kpi-delta-good")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Questions, day by day: from 1 to 3" })).toBeInTheDocument();
  });

  it("holds its place while loading", () => {
    const { container } = render(<KpiTile label="Questions" value={null} loading />);
    expect(container.querySelector(".kpi")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("Loading Questions")).toBeInTheDocument();
  });

  it("a sparkline with nothing in it says so", () => {
    render(<Sparkline values={[null, null]} label="Searches" />);
    expect(screen.getByRole("img", { name: "Searches: no data" })).toBeInTheDocument();
  });
});
