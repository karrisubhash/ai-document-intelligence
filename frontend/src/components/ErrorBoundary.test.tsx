import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Suspense, useState } from "react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { chunkRecovery, lazy } from "../lib/lazy";
import { AppErrorBoundary, RouteErrorBoundary } from "./ErrorBoundary";

function Broken({ error }: { error: unknown }): never {
  throw error;
}

/** Navigates from inside the router, like a sidebar link would. */
let go: (path: string) => void = () => undefined;
function Navigator() {
  const navigate = useNavigate();
  go = (path) => navigate(path);
  return null;
}

function Counter() {
  const [count, setCount] = useState(0);
  return (
    <button type="button" onClick={() => setCount((value) => value + 1)}>
      Clicked {count}
    </button>
  );
}

beforeEach(() => {
  // React reports every caught error to the console; the fallback is what
  // these tests look at.
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  window.sessionStorage.clear();
});

describe("route error boundary", () => {
  function renderRoutes(initial: string) {
    return render(
      <MemoryRouter initialEntries={[initial]}>
        <Navigator />
        <RouteErrorBoundary>
          <Routes>
            <Route path="/" element={<p>The dashboard</p>} />
            <Route path="/broken" element={<Broken error={new TypeError("x is undefined")} />} />
            <Route path="/ask" element={<Counter />} />
            <Route path="/ask/:id" element={<Counter />} />
          </Routes>
        </RouteErrorBoundary>
      </MemoryRouter>,
    );
  }

  it("replaces a page that throws with an explanation and two ways out", () => {
    renderRoutes("/broken");
    const heading = screen.getByRole("heading", { name: "Something went wrong" });
    expect(heading).toHaveFocus();
    expect(screen.getByText(/unexpected error/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to dashboard" })).toBeInTheDocument();
    // The message is there for a report, folded away.
    expect(screen.getByText("Technical details")).toBeInTheDocument();
    expect(screen.getByText("x is undefined")).toBeInTheDocument();
  });

  it("clears itself when the person moves to another screen", async () => {
    renderRoutes("/broken");
    expect(screen.getByRole("heading", { name: "Something went wrong" })).toBeInTheDocument();
    await act(async () => go("/"));
    expect(screen.queryByRole("heading", { name: "Something went wrong" })).toBeNull();
    expect(screen.getByText("The dashboard")).toBeInTheDocument();
  });

  it("goes to the dashboard from its own button", async () => {
    renderRoutes("/broken");
    await userEvent.click(screen.getByRole("button", { name: "Go to dashboard" }));
    expect(screen.getByText("The dashboard")).toBeInTheDocument();
  });

  it("never remounts a working page when only its path changes", async () => {
    // Ask moves /ask to /ask/12 under a conversation in progress.
    renderRoutes("/ask");
    await userEvent.click(screen.getByRole("button", { name: "Clicked 0" }));
    await act(async () => go("/ask/12"));
    expect(screen.getByRole("button", { name: "Clicked 1" })).toBeInTheDocument();
  });

  it("quotes the request id of a failed API call as a reference", () => {
    render(
      <MemoryRouter>
        <RouteErrorBoundary>
          <Broken error={new ApiError(500, "The server had a problem.", null, null, "4f2c9e0d1b")} />
        </RouteErrorBoundary>
      </MemoryRouter>,
    );
    expect(screen.getByText("4f2c9e0d1b")).toBeInTheDocument();
    expect(screen.getByText(/quote it if you report this/)).toBeInTheDocument();
  });

  it("explains a chunk that would not load, even after the one reload", async () => {
    // A reload already happened moments ago: this one is not a stale tab.
    window.sessionStorage.setItem("adi-chunk-reload", String(Date.now()));
    const reload = vi.spyOn(chunkRecovery, "reload").mockImplementation(() => undefined);
    const Page = lazy(() =>
      Promise.reject(new TypeError("Failed to fetch dynamically imported module: /assets/Page-1.js")),
    );
    render(
      <MemoryRouter>
        <RouteErrorBoundary>
          <Suspense fallback={<p>Loading</p>}>
            <Page />
          </Suspense>
        </RouteErrorBoundary>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Something went wrong" })).toBeInTheDocument();
    expect(screen.getByText(/could not be loaded/)).toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
  });
});

describe("application error boundary", () => {
  it("needs no router, session or toasts to explain itself", () => {
    render(
      <AppErrorBoundary>
        <Broken error={new Error("the provider broke")} />
      </AppErrorBoundary>,
    );
    expect(screen.getByRole("heading", { name: "Something went wrong" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to dashboard" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("renders its children when nothing is wrong", () => {
    render(
      <AppErrorBoundary>
        <p>All well</p>
      </AppErrorBoundary>,
    );
    expect(screen.getByText("All well")).toBeInTheDocument();
  });
});
