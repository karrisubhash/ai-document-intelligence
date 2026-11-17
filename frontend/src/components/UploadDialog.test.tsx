import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, type UploadInput } from "../api/client";
import type { DocumentDetail, Me } from "../api/types";
import App from "../App";
import { AuthProvider } from "../auth/AuthContext";
import { MAX_BATCH_FILES } from "../lib/batch";
import { PYTHON_ZIP, fromBase64, zipFile } from "../test/zips";
import UploadDialog from "./UploadDialog";
import { ToastProvider } from "./ui";

const ME: Me = {
  id: 1,
  username: "prof",
  email: "",
  first_name: "Ravi",
  last_name: "K",
  is_superuser: false,
  role: "FACULTY",
  department: "Computer Science",
  capabilities: {
    administer_users: false,
    administer_departments: false,
    view_audit_log: false,
    view_operations: false,
    view_system_status: false,
    upload_to_any_department: false,
    create_collections: true,
  },
};

const file = (name: string, content = "content") => new File([content], name);

let nextId = 1;
const asDocument = (input: UploadInput) => ({ id: nextId++, title: input.title }) as DocumentDetail;

type UploadOptions = { idempotencyKey?: string } | undefined;
const keyOf = (call: unknown[]) => (call[2] as UploadOptions)?.idempotencyKey;
const titleOf = (call: unknown[]) => (call[0] as UploadInput).title;

function renderDialog() {
  const handlers = { onClose: vi.fn(), onUploaded: vi.fn(), onBatchUploaded: vi.fn() };
  const { unmount } = render(
    <AuthProvider>
      <UploadDialog open {...handlers} />
    </AuthProvider>,
  );
  return { ...handlers, unmount };
}

async function choose(files: File[], options: { applyAccept?: boolean } = {}) {
  const dialog = await screen.findByRole("dialog", { name: "Upload documents" });
  const input = dialog.querySelector<HTMLInputElement>('input[type="file"]');
  if (!input) throw new Error("no file input");
  await userEvent.setup({ applyAccept: options.applyAccept ?? true }).upload(input, files);
  return dialog;
}

beforeEach(() => {
  nextId = 1;
  vi.spyOn(api, "me").mockResolvedValue(ME);
  vi.spyOn(api, "departments").mockResolvedValue([]);
});

afterEach(() => vi.restoreAllMocks());

describe("UploadDialog, one file", () => {
  it("keeps the single form: a title to edit, then the document opens", async () => {
    const upload = vi.spyOn(api, "upload").mockImplementation(async (input) => asDocument(input));
    const { onUploaded, onBatchUploaded } = renderDialog();

    const pdf = file("exam_regulations-2024.pdf");
    const dialog = await choose([pdf]);
    const title = within(dialog).getByLabelText(/^Title/);
    expect(title).toHaveValue("Exam regulations 2024");
    await userEvent.clear(title);
    await userEvent.type(title, "Exam rules");
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload" }));

    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith({ id: 1, title: "Exam rules" }));
    expect(upload).toHaveBeenCalledTimes(1);
    expect(keyOf(upload.mock.calls[0])).toMatch(/^[0-9a-f]{64}$/);
    expect(upload.mock.calls[0][0]).toEqual({
      title: "Exam rules",
      document_type: "GENERAL",
      department: undefined,
      file: pdf,
    });
    expect(onBatchUploaded).not.toHaveBeenCalled();
  });

  it("says why a file cannot be uploaded, and does not send it", async () => {
    const upload = vi.spyOn(api, "upload");
    renderDialog();
    const dialog = await choose([file("budget.xlsm")], { applyAccept: false });
    expect(within(dialog).getByText(/Unsupported file type/)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload" }));
    expect(upload).not.toHaveBeenCalled();
  });
});

describe("UploadDialog, several files", () => {
  it("uploads each as its own document, each with its own idempotency key", async () => {
    const upload = vi.spyOn(api, "upload").mockImplementation(async (input) => asDocument(input));
    const { onBatchUploaded, onUploaded } = renderDialog();

    const dialog = await choose([file("fee_schedule.pdf"), file("leave-policy.txt")]);
    expect(within(dialog).getByText("2 files to upload")).toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/^Title/)).not.toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload 2 documents" }));

    expect(await within(dialog).findByText("2 documents uploaded")).toBeInTheDocument();
    expect(upload.mock.calls.map(titleOf).sort()).toEqual(["Fee schedule", "Leave policy"]);
    const keys = upload.mock.calls.map(keyOf);
    expect(keys[0]).toMatch(/^[0-9a-f]{64}$/);
    expect(keys[1]).toMatch(/^[0-9a-f]{64}$/);
    expect(keys[0]).not.toBe(keys[1]);

    await userEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(onBatchUploaded).toHaveBeenCalledTimes(1);
    expect(onBatchUploaded.mock.calls[0][0]).toHaveLength(2);
    expect(onUploaded).not.toHaveBeenCalled();
  });

  it("opens a zip and uploads what is in it, listing what it skips", async () => {
    const upload = vi.spyOn(api, "upload").mockImplementation(async (input) => asDocument(input));
    renderDialog();

    const dialog = await choose([zipFile(fromBase64(PYTHON_ZIP), "term.zip")]);
    expect(await within(dialog).findByText("5 files to upload")).toBeInTheDocument();
    const list = within(dialog).getByRole("list", { name: "Chosen files" });
    expect(within(list).getByText(/in term\.zip › reports$/)).toBeInTheDocument();
    // A workbook is a format the server reads, so it is listed to upload, not skipped.
    const sheet = within(list).getByText("sheet.xlsx").closest("li") as HTMLElement;
    expect(within(sheet).queryByText("Skipped")).not.toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: "Upload 5 documents" }));
    expect(await within(dialog).findByText("5 documents uploaded")).toBeInTheDocument();
    const sent = upload.mock.calls.map((call) => (call[0] as UploadInput).file);
    expect(sent.map((item) => item.name).sort()).toEqual([
      "exam_rules.txt",
      "forced64.txt",
      "résumé.txt",
      "scan.png",
      "sheet.xlsx",
    ]);
    const rules = sent.find((item) => item.name === "exam_rules.txt");
    expect(rules?.type).toBe("text/plain");
    expect(await rules?.text()).toMatch(/^Exam rules: 75 percent/);
  });

  it("retries a failed file with the same key, so a lost response cannot duplicate it", async () => {
    let failOnce = true;
    const upload = vi.spyOn(api, "upload").mockImplementation(async (input) => {
      if (input.title === "Leave policy" && failOnce) {
        failOnce = false;
        throw new ApiError(503, "The server had a problem handling this request.", null);
      }
      return asDocument(input);
    });
    renderDialog();

    const dialog = await choose([file("fee_schedule.pdf"), file("leave-policy.txt")]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload 2 documents" }));
    expect(await within(dialog).findByText("1 of 2 uploaded")).toBeInTheDocument();
    const row = within(dialog).getByText("leave-policy.txt").closest("li") as HTMLElement;
    expect(within(row).getByText("Failed")).toBeInTheDocument();
    expect(within(row).getByText(/server had a problem/)).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: "Upload 1 remaining" }));
    expect(await within(dialog).findByText("2 documents uploaded")).toBeInTheDocument();
    const leave = upload.mock.calls.filter((call) => titleOf(call) === "Leave policy");
    expect(leave).toHaveLength(2);
    expect(keyOf(leave[0])).toBe(keyOf(leave[1]));
  });

  it("does not offer to retry a file the server refused for what it is", async () => {
    vi.spyOn(api, "upload").mockImplementation(async (input) => {
      if (input.title === "Fake") {
        throw new ApiError(400, "file: Unsupported file type '.pdf'.", {
          file: ["Unsupported file type '.pdf'."],
        });
      }
      return asDocument(input);
    });
    renderDialog();

    const dialog = await choose([file("fake.pdf"), file("real.pdf")]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload 2 documents" }));
    expect(await within(dialog).findByText("1 of 2 uploaded")).toBeInTheDocument();
    expect(within(dialog).getByText("Unsupported file type '.pdf'.")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /remaining/ })).not.toBeInTheDocument();
  });

  it("stops at the upload limit and says when the rest can go", async () => {
    const upload = vi.spyOn(api, "upload").mockImplementation(async (input) => {
      if (input.title === "One") {
        throw new ApiError(429, "Request was throttled.", { detail: "Request was throttled." }, 1150);
      }
      return asDocument(input);
    });
    renderDialog();

    const dialog = await choose([file("one.txt"), file("two.txt"), file("three.txt")]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload 3 documents" }));
    expect(await within(dialog).findByText("1 of 3 uploaded")).toBeInTheDocument();
    expect(within(dialog).getByText(/upload limit.*about 20 minutes/)).toBeInTheDocument();
    // "two" was already on its way; "three" was never sent.
    expect(upload.mock.calls.map(titleOf).sort()).toEqual(["One", "Two"]);
    expect(within(dialog).getAllByText("Not uploaded")).toHaveLength(2);
    expect(within(dialog).getByRole("button", { name: "Upload 2 remaining" })).toBeInTheDocument();
  });

  it("stops at once, and sends what was cut off again under the same keys", async () => {
    let stalled = true;
    const upload = vi.spyOn(api, "upload").mockImplementation(
      (input, _progress, options) =>
        new Promise((resolve, reject) => {
          if (!stalled) return resolve(asDocument(input));
          options?.signal?.addEventListener("abort", () =>
            reject(new DOMException("The upload was stopped.", "AbortError")),
          );
        }),
    );
    const { onClose } = renderDialog();

    const dialog = await choose([file("a.txt"), file("b.txt"), file("c.txt")]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload 3 documents" }));
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
    // Not closable while files are on their way.
    await userEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(onClose).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole("button", { name: "Stop" }));
    expect(await within(dialog).findByText("0 of 3 uploaded")).toBeInTheDocument();
    expect(within(dialog).getAllByText("Not uploaded")).toHaveLength(3);

    stalled = false;
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload 3 remaining" }));
    expect(await within(dialog).findByText("3 documents uploaded")).toBeInTheDocument();
    const keysOf = (title: string) =>
      upload.mock.calls.filter((call) => titleOf(call) === title).map(keyOf);
    expect(new Set(keysOf("A")).size).toBe(1);
    expect(keysOf("A")).toHaveLength(2);
  });

  it("sends the same keys when the same files are chosen again, so nothing is made twice", async () => {
    const upload = vi.spyOn(api, "upload").mockImplementation(async (input) => asDocument(input));
    const chosen = [
      new File(["a"], "a.txt", { lastModified: 1 }),
      new File(["b"], "b.txt", { lastModified: 1 }),
    ];

    const first = renderDialog();
    let dialog = await choose(chosen);
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload 2 documents" }));
    await within(dialog).findByText("2 documents uploaded");
    first.unmount();

    renderDialog();
    dialog = await choose(chosen);
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload 2 documents" }));
    await within(dialog).findByText("2 documents uploaded");

    const keys = upload.mock.calls.map(keyOf);
    expect(keys.slice(2).sort()).toEqual(keys.slice(0, 2).sort());
  });

  it("sends a file again under a fresh key when its earlier document was deleted", async () => {
    let replayed = true;
    const upload = vi.spyOn(api, "upload").mockImplementation(async (input) => {
      if (replayed) {
        // What the server answers a key whose document is gone.
        replayed = false;
        throw new ApiError(404, "Not found.", { detail: "Not found." });
      }
      return asDocument(input);
    });
    const { onUploaded } = renderDialog();

    const dialog = await choose([file("notes.pdf")]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith({ id: 1, title: "Notes" }));
    const [derived, fresh] = upload.mock.calls.map(keyOf);
    expect(derived).toMatch(/^[0-9a-f]{64}$/);
    expect(fresh).toMatch(/^[0-9a-f]{32}$/);
  });

  it("refuses a choice that would pass the limit, adding none of it", async () => {
    renderDialog();
    const many = Array.from({ length: MAX_BATCH_FILES + 1 }, (_, i) => file(`doc-${i}.txt`));
    const dialog = await choose(many);
    expect(
      await within(dialog).findByText(new RegExp(`at most ${MAX_BATCH_FILES} go at a time`)),
    ).toBeInTheDocument();
    expect(within(dialog).queryByRole("list", { name: "Chosen files" })).not.toBeInTheDocument();
  });

  it("keeps the focus in the list when a file is removed", async () => {
    renderDialog();
    const dialog = await choose([file("a.pdf"), file("b.pdf"), file("c.pdf")]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove b.pdf" }));
    expect(within(dialog).getByRole("button", { name: "Remove c.pdf" })).toHaveFocus();
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove c.pdf" }));
    expect(within(dialog).getByRole("button", { name: "Remove a.pdf" })).toHaveFocus();
  });

  it("goes back to the single form when all but one file are removed", async () => {
    renderDialog();
    const dialog = await choose([file("a.pdf"), file("b.pdf")]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove b.pdf" }));
    expect(within(dialog).getByLabelText(/^Title/)).toHaveValue("A");
    expect(within(dialog).getByRole("button", { name: "Upload" })).toBeInTheDocument();
  });
});

describe("DocumentsPage after a batch", () => {
  it("closes the dialog and lists the new documents", async () => {
    const list = vi
      .spyOn(api, "documents")
      .mockResolvedValue({ count: 0, next: null, previous: null, results: [] });
    vi.spyOn(api, "upload").mockImplementation(async (input) => asDocument(input));
    render(
      <MemoryRouter initialEntries={["/documents"]}>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>,
    );

    await userEvent.click(
      (await screen.findAllByRole("button", { name: "Upload document" }, { timeout: 5000 }))[0],
    );
    const dialog = await choose([file("a.pdf"), file("b.pdf")]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload 2 documents" }));
    await userEvent.click(await within(dialog).findByRole("button", { name: "Done" }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Upload documents" })).not.toBeInTheDocument(),
    );
    expect(list).toHaveBeenCalledTimes(2);
  });
});
