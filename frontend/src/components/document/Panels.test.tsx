import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../../api/client";
import type { DocumentDetail, DocumentStatus, DocumentType, ExtractionResult } from "../../api/types";
import { FieldsPanel, PreviewPanel } from "./Panels";

// A fresh object every call, exactly as each poll of the document returns.
function detail(status: DocumentStatus): DocumentDetail {
  return {
    id: 7,
    title: "Hostel circular",
    document_type: "GENERAL",
    department: "Physics",
    owner: 1,
    owner_username: "asha",
    status,
    current_version: {
      id: 3,
      version_number: 1,
      file_name: "circular.txt",
      file_size: 21,
      mime_type: "text/plain",
      uploaded_by: 1,
      created_at: "2026-10-04T10:00:00Z",
    },
    is_archived: false,
    archived_at: null,
    created_at: "2026-10-04T10:00:00Z",
    updated_at: "2026-10-04T10:00:00Z",
    can_manage: true,
  };
}

function panel(document: DocumentDetail) {
  return (
    <MemoryRouter>
      <PreviewPanel document={document} />
    </MemoryRouter>
  );
}

afterEach(() => vi.restoreAllMocks());

describe("PreviewPanel", () => {
  it("downloads the file once while the page polls a processing document", async () => {
    const fileBlob = vi
      .spyOn(api, "fileBlob")
      .mockResolvedValue(new Blob(["Curfew is at 10 pm."], { type: "text/plain" }));

    const { rerender } = render(panel(detail("PROCESSING")));
    expect(await screen.findByText(/Curfew is at 10 pm/)).toBeInTheDocument();

    // Three more polls: same document, same version, new objects - and the
    // status moving on. None of it changes which file is shown.
    rerender(panel(detail("PROCESSING")));
    rerender(panel(detail("PROCESSING")));
    rerender(panel(detail("COMPLETED")));

    expect(screen.getByText(/Curfew is at 10 pm/)).toBeInTheDocument();
    expect(fileBlob).toHaveBeenCalledTimes(1);
  });

  it("downloads again when a new version becomes current", async () => {
    const fileBlob = vi
      .spyOn(api, "fileBlob")
      .mockResolvedValue(new Blob(["text"], { type: "text/plain" }));

    const { rerender } = render(panel(detail("COMPLETED")));
    await waitFor(() => expect(fileBlob).toHaveBeenCalledTimes(1));

    const next = detail("COMPLETED");
    next.current_version = { ...next.current_version!, id: 4, version_number: 2 };
    rerender(panel(next));
    await waitFor(() => expect(fileBlob).toHaveBeenCalledTimes(2));
  });

  it("abandons an unfinished download when it is no longer wanted", async () => {
    let signal: AbortSignal | undefined;
    vi.spyOn(api, "fileBlob").mockImplementation((_id, _version, given) => {
      signal = given;
      return new Promise<Blob>(() => {});
    });

    const { unmount } = render(panel(detail("COMPLETED")));
    await waitFor(() => expect(signal).toBeDefined());
    unmount();
    expect(signal?.aborted).toBe(true);
  });
});

describe("FieldsPanel", () => {
  function typed(document_type: DocumentType): DocumentDetail {
    return { ...detail("COMPLETED"), document_type };
  }

  /** What field extraction recorded while the document was a circular. */
  const CIRCULAR_FIELDS: ExtractionResult = {
    id: 31,
    model_name: "rule-extractor",
    model_version: "v1",
    schema_name: "circular",
    schema_version: "v1",
    document_type: "CIRCULAR",
    field_count: 2,
    found_count: 1,
    metadata: {},
    created_at: "2026-10-04T10:00:05Z",
    extracted_fields: [
      {
        id: 1,
        field_name: "circular_number",
        status: "FOUND",
        value: "TR/2026/14",
        confidence: 0.9,
        page_number: 1,
        character_start: 10,
        character_end: 20,
        matched_text: "TR/2026/14",
        metadata: {},
      },
    ],
  };

  function fields(document: DocumentDetail) {
    return (
      <MemoryRouter>
        <FieldsPanel document={document} />
      </MemoryRouter>
    );
  }

  it("never lists another type's fields as this document's", async () => {
    vi.spyOn(api, "extraction").mockResolvedValue([CIRCULAR_FIELDS]);

    render(fields(typed("OTHER")));

    expect(await screen.findByText("No fields extracted")).toBeInTheDocument();
    expect(screen.getByText(/this document's type \(Other\)/)).toBeInTheDocument();
    expect(screen.queryByText("TR/2026/14")).not.toBeInTheDocument();
  });

  it("loads the fields again when the type changes", async () => {
    const extraction = vi
      .spyOn(api, "extraction")
      .mockResolvedValueOnce([CIRCULAR_FIELDS])
      .mockResolvedValue([]);

    const { rerender } = render(fields(typed("CIRCULAR")));
    expect(await screen.findByText("TR/2026/14")).toBeInTheDocument();

    rerender(fields(typed("OTHER")));
    await waitFor(() => expect(extraction).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("No fields extracted")).toBeInTheDocument();
    expect(screen.queryByText("TR/2026/14")).not.toBeInTheDocument();
  });
});
