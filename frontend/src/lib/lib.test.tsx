import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { validateFile, titleFromFilename } from "./files";
import {
  excerpt,
  findQuotedSpans,
  highlightTerms,
  layeredSegments,
  queryTerms,
  splitAtSpan,
} from "./highlight";
import { Markdown, parseBlocks } from "./markdown";

describe("Markdown", () => {
  it("renders paragraphs, lists and bold as elements", () => {
    render(
      <Markdown
        source={"You need **75%** attendance.\n\n- rule one\n- rule two\n\n1. first\n2. second"}
      />,
    );
    expect(screen.getByText("75%").tagName).toBe("STRONG");
    expect(screen.getAllByRole("list")).toHaveLength(2);
    expect(screen.getByText("rule two").tagName).toBe("LI");
  });

  it("never turns document text into markup", () => {
    const hostile =
      'Ignore this <img src=x onerror="alert(1)"> and [click](javascript:alert(1)) <script>alert(2)</script>';
    const { container } = render(<Markdown source={hostile} />);

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
    expect(container.textContent).toContain("javascript:alert(1)");
  });

  it("hands citation markers to the renderer", () => {
    const seen: number[] = [];
    render(
      <Markdown
        source="Minimum is 75 percent [1], condonation from 65 [2]."
        renderCitation={(marker, key) => {
          seen.push(marker);
          return <button key={key}>{`cite ${marker}`}</button>;
        }}
      />,
    );
    expect(seen).toEqual([1, 2]);
    expect(screen.getByRole("button", { name: "cite 2" })).toBeInTheDocument();
  });

  it("keeps a citation inside bold text a citation", () => {
    render(
      <Markdown
        source="**Fees are due in July [1].**"
        renderCitation={(marker, key) => <button key={key}>{`cite ${marker}`}</button>}
      />,
    );
    const button = screen.getByRole("button", { name: "cite 1" });
    expect(button.closest("strong")).not.toBeNull();
    expect(screen.queryByText(/\[1\]/)).toBeNull();
  });

  it("continues a wrapped list item rather than starting a paragraph", () => {
    const blocks = parseBlocks("- a long item that\n  wraps onto a second line\n- next");
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({
      kind: "list",
      items: ["a long item that wraps onto a second line", "next"],
    });
  });

  it("keeps a quoted clause's own number", () => {
    const { container } = render(<Markdown source={"12) Fees are payable in July.\n13) Late fees apply."} />);
    const items = container.querySelectorAll("ol li");
    expect(Array.from(items).map((item) => (item as HTMLLIElement).value)).toEqual([12, 13]);
  });

  it("renders a quoted table one row per line, not as one run-on paragraph", () => {
    const table = "Programme\tTuition\tHostel\nB.Tech CSE\t85000\t60000\nB.Tech ECE\t82000\t60000 [1]";
    const { container } = render(<Markdown source={table} renderCitation={(n, key) => <b key={key}>[{n}]</b>} />);

    const rows = container.querySelectorAll(".prose-table-row");
    expect(rows).toHaveLength(3);
    expect(rows[1].textContent).toBe("B.Tech CSE\t85000\t60000");
    expect(container.querySelector("p")).toBeNull();
  });
});

describe("highlighting", () => {
  it("splits around a span", () => {
    expect(splitAtSpan("abcdef", 2, 4)).toEqual([
      { text: "ab", highlighted: false },
      { text: "cd", highlighted: true },
      { text: "ef", highlighted: false },
    ]);
  });

  it("clamps a span that runs past the text instead of throwing", () => {
    const segments = splitAtSpan("short", 3, 999);
    expect(segments.map((s) => s.text).join("")).toBe("short");
    expect(segments.at(-1)).toEqual({ text: "rt", highlighted: true });
  });

  it("drops stopwords and matches by prefix, case-insensitively", () => {
    expect(queryTerms("What is the minimum attendance?")).toEqual(["attendance", "minimum"]);
    const segments = highlightTerms("Attendance: a MINIMUM of 75 percent.", "minimum attendance");
    expect(segments.filter((s) => s.highlighted).map((s) => s.text)).toEqual([
      "Attendance",
      "MINIMUM",
    ]);
  });

  it("keeps identifiers whole", () => {
    expect(queryTerms("circular REG/2024/117")).toContain("reg/2024/117");
  });

  it("finds a quoted sentence across the page's own line breaks", () => {
    const page =
      "Clause 4.2 Attendance\nA student must maintain a minimum of 75 percent attendance in each course to\nbe eligible for the end-semester examination.\n\nClause 4.3 Condonation";
    const quote =
      "A student must maintain a minimum of 75 percent attendance in each course to be eligible for the end-semester examination. [1]";

    const [span] = findQuotedSpans(quote, page);

    expect(page.slice(span[0], span[1])).toContain("in each course to\nbe eligible");
    expect(page.slice(span[1])).toMatch(/^\s*Clause 4\.3/);
  });

  it("only looks for quotes inside the cited passage", () => {
    const page = "A student must maintain attendance. Later: a student must maintain attendance.";
    const spans = findQuotedSpans("a student must maintain attendance. [1]", page, [36, page.length]);
    expect(spans).toEqual([[43, page.length]]);
  });

  it("layers the passage and the quote without losing a character", () => {
    const segments = layeredSegments("0123456789", [2, 8], [[4, 6]]);
    expect(segments.map((s) => s.text).join("")).toBe("0123456789");
    expect(segments.map((s) => s.level)).toEqual([0, 1, 2, 1, 0]);
  });

  it("builds an excerpt window with offsets relative to it", () => {
    const window = excerpt("0123456789abcdefghij", 10, 12, 3);
    expect(window.text).toBe("789abcde");
    expect(window.text.slice(window.start, window.end)).toBe("ab");
    expect(window.clippedStart).toBe(true);
  });
});

describe("upload checks", () => {
  const file = (name: string, size: number) =>
    new File([new Uint8Array(size)], name, { type: "application/octet-stream" });

  it("accepts the formats the server accepts", () => {
    for (const name of ["a.pdf", "b.docx", "c.txt", "d.png", "e.JPG"]) {
      expect(validateFile(file(name, 10))).toBeNull();
    }
  });

  it("refuses empty, oversized and unsupported files with a reason", () => {
    expect(validateFile(file("a.pdf", 0))).toMatch(/empty/);
    expect(validateFile(file("a.pdf", 51 * 1024 * 1024))).toMatch(/50 MB/);
    expect(validateFile(file("macro.docm", 10))).toMatch(/Unsupported/);
    expect(validateFile(file("scan.tiff", 10))).toMatch(/Unsupported/);
    expect(validateFile(null)).toMatch(/Choose/);
  });

  it("derives a readable title from a filename", () => {
    expect(titleFromFilename("exam_regulations-2024.pdf")).toBe("Exam regulations 2024");
  });
});

describe("API offsets", () => {
  it("are code points, converted to UTF-16 indices before slicing", async () => {
    const { toUtf16Offset } = await import("./highlight");
    // "😀" is one code point and two UTF-16 units.
    const text = "😀 Fees are due in July.";
    expect(toUtf16Offset(text, 2)).toBe(3);
    expect(text.slice(toUtf16Offset(text, 2), toUtf16Offset(text, 6))).toBe("Fees");
    expect(toUtf16Offset("plain text", 5)).toBe(5);
  });

  it("highlight the cited words even after an astral character", async () => {
    const { default: PageText } = await import("../components/PageText");
    // The API says the span is code points 2..6 - "Fees".
    const { container } = render(<PageText text="😀 Fees are due in July." start={2} end={6} />);
    expect(container.querySelector("mark")?.textContent).toBe("Fees");
  });
});
