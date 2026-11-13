import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../api/client";
import { homeApi } from "../../api/home";
import type { Dashboard, Department, Me } from "../../api/types";
import App from "../../App";
import { AuthProvider } from "../../auth/AuthContext";
import { ToastProvider } from "../ui";

const ROOT: Me = {
  id: 1,
  username: "admin",
  email: "",
  first_name: "",
  last_name: "",
  is_superuser: true,
  role: "SYSTEM_ADMIN",
  // What createsuperuser gives a system administrator: no department.
  department: "",
  capabilities: {
    administer_users: true,
    administer_departments: true,
    view_audit_log: true,
    view_operations: true,
    view_system_status: true,
    upload_to_any_department: true,
    create_collections: true,
  },
};

const EMPTY_LIBRARY: Dashboard = {
  documents: { total: 0, by_status: {} },
  recent_documents: [],
  needs_attention: [],
  recent_conversations: [],
};

const department = (id: number, name: string): Department => ({ id, name, code: "" });

function renderHome() {
  render(
    <MemoryRouter initialEntries={["/"]}>
      <AuthProvider>
        <ToastProvider>
          <App />
        </ToastProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

async function sampleStep(): Promise<HTMLElement> {
  const title = await screen.findByText("Load sample documents");
  return title.closest("li") as HTMLElement;
}

beforeEach(() => {
  vi.spyOn(api, "me").mockResolvedValue(ROOT);
  vi.spyOn(api, "dashboard").mockResolvedValue(EMPTY_LIBRARY);
  vi.spyOn(homeApi, "samplesAvailable").mockResolvedValue(true);
  vi.spyOn(homeApi, "suggestions").mockResolvedValue([]);
  vi.spyOn(homeApi, "deadlines").mockResolvedValue([]);
  vi.spyOn(homeApi, "collectionCount").mockResolvedValue(0);
});

afterEach(() => vi.restoreAllMocks());

describe("the sample-documents step", () => {
  it("asks a system administrator with no department where the samples go, and loads them there", async () => {
    vi.spyOn(api, "departments").mockResolvedValue([department(1, "Physics"), department(2, "Chemistry")]);
    const load = vi.spyOn(homeApi, "loadSamples").mockResolvedValue({ queued: 16, skipped: 0 });
    renderHome();
    const step = await sampleStep();

    // Nothing to post until a department is chosen: the server refuses "".
    const button = within(step).getByRole("button", { name: "Load samples" });
    expect(button).toBeDisabled();
    await userEvent.selectOptions(
      within(step).getByRole("combobox", { name: "Department for the samples" }),
      "Chemistry",
    );
    expect(step).toHaveTextContent("A small sample library for Chemistry");
    await userEvent.click(button);

    expect(load).toHaveBeenCalledWith("Chemistry");
    expect(await screen.findByText("16 sample documents are being processed.")).toBeInTheDocument();
  });

  it("starts from the administrator's own department, in the registry's spelling", async () => {
    vi.spyOn(api, "me").mockResolvedValue({ ...ROOT, department: "physics" });
    vi.spyOn(api, "departments").mockResolvedValue([department(1, "Physics"), department(2, "Chemistry")]);
    const load = vi.spyOn(homeApi, "loadSamples").mockResolvedValue({ queued: 16, skipped: 0 });
    renderHome();
    const step = await sampleStep();

    expect(within(step).getByRole("combobox", { name: "Department for the samples" })).toHaveValue("Physics");
    await userEvent.click(within(step).getByRole("button", { name: "Load samples" }));
    expect(load).toHaveBeenCalledWith("Physics");
  });

  it("uses the only department there is without asking", async () => {
    vi.spyOn(api, "departments").mockResolvedValue([department(1, "Physics")]);
    const load = vi.spyOn(homeApi, "loadSamples").mockResolvedValue({ queued: 16, skipped: 0 });
    renderHome();
    const step = await sampleStep();

    expect(within(step).queryByRole("combobox")).toBeNull();
    expect(step).toHaveTextContent("A small sample library for Physics");
    await userEvent.click(within(step).getByRole("button", { name: "Load samples" }));
    expect(load).toHaveBeenCalledWith("Physics");
  });

  it("leads to Departments when none is set up, instead of offering a load that cannot succeed", async () => {
    vi.spyOn(api, "departments").mockResolvedValue([]);
    const load = vi.spyOn(homeApi, "loadSamples");
    renderHome();
    const step = await sampleStep();

    expect(within(step).queryByRole("button", { name: "Load samples" })).toBeNull();
    expect(within(step).getByRole("link", { name: "Start" })).toHaveAttribute("href", "/admin/departments");
    expect(load).not.toHaveBeenCalled();
  });
});
