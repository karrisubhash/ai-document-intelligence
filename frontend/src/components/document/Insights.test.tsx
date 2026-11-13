import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../api/client";
import { insightsApi, type DocumentInsights } from "../../api/insights";
import type { DocumentDetail, DocumentSummary } from "../../api/types";
import { tabsFor } from "../../pages/DocumentDetailPage";
import { InsightsOverview, healthLine, typeProvenance } from "./Insights";
import { DocumentThumb, StageNote, TypeCell, fileLine } from "./LibraryCells";

function documentWith(overrides: Partial<DocumentDetail> = {}): DocumentDetail {
  return {
    id: 7,
    title: "Transport circular",
    document_type: "CIRCULAR",
    document_type_source: "auto",
    type_confidence: 0.82,
    department: "Computer Science",
    owner: 1,
    owner_username: "asha",
    status: "COMPLETED",
    current_stage: null,
    current_version: {
      id: 3,
      version_number: 1,
      file_name: "circular.pdf",
      file_size: 2048,
      mime_type: "application/pdf",
      uploaded_by: 1,
      uploaded_by_name: "Asha Rao",
      created_at: "2026-10-05T10:00:00Z",
    },
    page_count: 2,
    has_thumbnail: true,
    counts: { tables: 0, figures: 1, fields_found: 3 },
    collections: [{ id: 4, name: "Transport" }],
    is_archived: false,
    archived_at: null,
    can_manage: true,
    created_at: "2026-10-05T10:00:00Z",
    updated_at: "2026-10-05T10:00:00Z",
    ...overrides,
  };
}

const READY: DocumentInsights = {
  document_id: 7,
  version_id: 3,
  content_id: 9,
  status: "READY",
  method: "extractive-v1",
  generated_at: "2026-10-05T10:01:00Z",
  summary: [
    { text: "A bus pass costs Rs. 4,500 per\nsemester.", page_number: 1, start: 120, end: 160 },
  ],
  key_points: [
    { text: "A student must show the pass.", page_number: 1, start: 200, end: 229, kind: "obligation" },
  ],
  key_phrases: [{ text: "bus pass", score: 1 }],
  entities: [
    {
      type: "DEADLINE",
      text: "15th March 2027",
      normalized: "2027-03-15",
      page_number: 2,
      start: 10,
      end: 25,
      count: 1,
      label: "Last date for renewal of the bus pass",
    },
    {
      type: "MONEY",
      text: "Rs. 4,500",
      normalized: "INR 4500",
      page_number: 1,
      start: 136,
      end: 145,
      count: 2,
      label: "",
    },
  ],
  stats: { pages: 2, words: 1200, reading_minutes: 6, ocr_pages: 1, language: "en" },
  suggested_questions: ["What is the last date for renewal of the bus pass?"],
  document_type: { value: "CIRCULAR", source: "auto", confidence: 0.82 },
  ai: null,
};

function AskProbe() {
  const location = useLocation();
  const state = location.state as { question?: string; documentId?: number } | null;
  return (
    <p>
      Asked: {state?.question} about {state?.documentId}
    </p>
  );
}

function renderOverview(document = documentWith()) {
  return render(
    <MemoryRouter initialEntries={["/documents/7"]}>
      <Routes>
        <Route path="/documents/:id" element={<InsightsOverview document={document} />} />
        <Route path="/ask" element={<AskProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.spyOn(api, "classification").mockResolvedValue([]);
  vi.spyOn(insightsApi, "related").mockResolvedValue({ results: [], method: "lexical" });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("InsightsOverview", () => {
  it("shows the brief with every sentence linked to its place", async () => {
    vi.spyOn(insightsApi, "insights").mockResolvedValue(READY);
    renderOverview();

    const sentence = await screen.findByRole("link", { name: "A bus pass costs Rs. 4,500 per semester." });
    expect(sentence).toHaveAttribute("href", "/documents/7?tab=text&page=1&start=120&end=160");
    expect(screen.getByText("Extracted from the document")).toBeInTheDocument();
    expect(screen.getByText("2 pages · 1,200 words · 6 min read · 1 page read by OCR · English")).toBeInTheDocument();
    expect(screen.getByText("Requirement")).toBeInTheDocument();
  });

  it("groups key facts and says what a deadline is for", async () => {
    vi.spyOn(insightsApi, "insights").mockResolvedValue(READY);
    renderOverview();

    const dates = await screen.findByRole("region", { name: "Dates and deadlines" });
    const deadline = within(dates).getByRole("link");
    expect(deadline).toHaveTextContent("Due");
    expect(deadline).toHaveTextContent("15 Mar 2027");
    expect(deadline).toHaveTextContent("Last date for renewal of the bus pass");
    expect(deadline).toHaveAttribute("href", "/documents/7?tab=text&page=2&start=10&end=25");
    const amounts = screen.getByRole("region", { name: "Amounts and figures" });
    expect(within(amounts).getByRole("link")).toHaveTextContent("×2");
    expect(screen.getByRole("link", { name: "bus pass" })).toHaveAttribute("href", "/search");
  });

  it("hands a suggested question to Ask, scoped to this document", async () => {
    vi.spyOn(insightsApi, "insights").mockResolvedValue(READY);
    renderOverview();

    await userEvent.click(
      await screen.findByRole("button", { name: /What is the last date for renewal/ }),
    );
    expect(
      await screen.findByText("Asked: What is the last date for renewal of the bus pass? about 7"),
    ).toBeInTheDocument();
  });

  it("says it is reading the document, and checks back until the brief is ready", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const pending = { ...READY, status: "PENDING" as const, summary: [], generated_at: null };
    const load = vi
      .spyOn(insightsApi, "insights")
      .mockResolvedValueOnce(pending)
      .mockResolvedValue(READY);
    renderOverview();

    expect(await screen.findByText("Reading this document…")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_500);
    });
    expect(load).toHaveBeenCalledTimes(2);
    expect(await screen.findByText("Extracted from the document")).toBeInTheDocument();
  });

  it("explains a missing brief in the reader's terms", async () => {
    vi.spyOn(insightsApi, "insights").mockResolvedValue({
      ...READY,
      status: "UNAVAILABLE",
      summary: [],
    });
    renderOverview(documentWith({ status: "QUARANTINED" }));
    expect(
      await screen.findByText("Quarantined files are not read, so there is no brief."),
    ).toBeInTheDocument();
  });

  it("marks a near-duplicate among related documents", async () => {
    vi.spyOn(insightsApi, "insights").mockResolvedValue(READY);
    vi.spyOn(insightsApi, "related").mockResolvedValue({
      method: "semantic",
      results: [
        {
          id: 8,
          title: "Transport circular (copy)",
          document_type: "CIRCULAR",
          department: "Computer Science",
          updated_at: "2026-10-05T10:00:00Z",
          similarity: 0.99,
          near_duplicate: true,
        },
      ],
    });
    renderOverview();
    expect(await screen.findByRole("link", { name: "Transport circular (copy)" })).toHaveAttribute(
      "href",
      "/documents/8",
    );
    expect(screen.getByText("Possible duplicate")).toBeInTheDocument();
    expect(screen.getByText("By meaning")).toBeInTheDocument();
  });

  it("shows how the type was decided, and a layout class only as a layout", async () => {
    vi.spyOn(insightsApi, "insights").mockResolvedValue(READY);
    vi.spyOn(api, "classification").mockResolvedValue([
      {
        id: 1,
        model_name: "rvl",
        model_version: "1",
        taxonomy: "layout_class",
        predicted_label: "letter",
        confidence: 0.71,
        abstained: false,
        metadata: {},
        created_at: "2026-10-05T10:00:00Z",
      },
    ]);
    renderOverview();
    expect(await screen.findByText("Auto-detected · 82%")).toBeInTheDocument();
    expect(await screen.findByText("Letter (71%)")).toBeInTheDocument();
    expect(screen.queryByText(/Not classified/)).not.toBeInTheDocument();
  });
});

describe("helpers", () => {
  it("phrases the health line and the type's provenance", () => {
    expect(
      healthLine({ pages: 1, words: 90, reading_minutes: 1, ocr_pages: 1, language: "unknown" }),
    ).toBe("1 page · 90 words · 1 min read · scanned, read by OCR");
    expect(typeProvenance(documentWith({ document_type_source: "user" }))).toBe("Chosen by a person");
    expect(
      typeProvenance(
        documentWith({ document_type: "GENERAL", document_type_source: "default", type_confidence: null }),
      ),
    ).toBe("No specific type detected");
  });

  it("omits empty tabs, counts the rest, and keeps a deep-linked one", () => {
    const tabs = tabsFor(documentWith(), "overview");
    const ids = tabs.map((tab) => tab.id);
    expect(ids).toEqual(["overview", "preview", "text", "fields", "figures", "versions", "processing"]);
    expect(tabs.find((tab) => tab.id === "fields")?.count).toBe(3);
    expect(tabsFor(documentWith(), "tables").map((tab) => tab.id)).toContain("tables");
    expect(tabsFor(documentWith({ counts: undefined }), "overview")).toHaveLength(8);
  });
});

describe("library cells", () => {
  const row = documentWith() as DocumentSummary;

  it("shows the first page, or a text card when there is none", () => {
    const { container, rerender } = render(<DocumentThumb row={row} />);
    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      "/api/v1/documents/7/thumbnail/?v=3",
    );
    rerender(<DocumentThumb row={{ ...row, has_thumbnail: false }} />);
    expect(container.querySelector(".doc-textcard")).toHaveTextContent("PDF");
  });

  it("adds pages, marks a detected type and names the live stage", () => {
    expect(fileLine(row)).toBe("circular.pdf · v1 · 2 pages · 2.0 KB");
    render(
      <>
        <TypeCell row={row} />
        <StageNote row={{ ...row, status: "PROCESSING", current_stage: "OCR" }} />
      </>,
    );
    expect(screen.getByText("auto")).toHaveAttribute("title", "Detected automatically (82%)");
    expect(screen.getByText("OCR")).toHaveClass("live-stage");
  });
});
