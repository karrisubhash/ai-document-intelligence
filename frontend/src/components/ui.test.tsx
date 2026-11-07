import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { Tone } from "../lib/labels";
import { ErrorState, ToastProvider, useToast } from "./ui";

function Raise({ message, tone }: { message: string; tone?: Tone }) {
  const toast = useToast();
  return (
    <button type="button" onClick={() => toast(message, tone)}>
      Raise {message}
    </button>
  );
}

function renderToasts(...items: { message: string; tone?: Tone }[]) {
  return render(
    <ToastProvider>
      {items.map((item) => (
        <Raise key={item.message} {...item} />
      ))}
    </ToastProvider>,
  );
}

const raise = (message: string) =>
  fireEvent.click(screen.getByRole("button", { name: `Raise ${message}` }));
const advance = (ms: number) => act(() => vi.advanceTimersByTime(ms));

describe("toasts", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("lets a confirmation go after five seconds", () => {
    renderToasts({ message: "Saved." });
    raise("Saved.");
    expect(screen.getByText("Saved.")).toBeInTheDocument();
    advance(4900);
    expect(screen.getByText("Saved.")).toBeInTheDocument();
    advance(200);
    expect(screen.queryByText("Saved.")).toBeNull();
  });

  it("holds its time while the pointer is over it, and resumes where it stopped", () => {
    renderToasts({ message: "Saved." });
    raise("Saved.");
    advance(3000);
    fireEvent.mouseEnter(screen.getByText("Saved.").closest(".toasts")!);
    advance(60_000);
    expect(screen.getByText("Saved.")).toBeInTheDocument();
    fireEvent.mouseLeave(screen.getByText("Saved.").closest(".toasts")!);
    // Two seconds were left when the pointer arrived, not a fresh five.
    advance(1900);
    expect(screen.getByText("Saved.")).toBeInTheDocument();
    advance(200);
    expect(screen.queryByText("Saved.")).toBeNull();
  });

  it("holds its time while keyboard focus is inside it", () => {
    renderToasts({ message: "Saved." });
    raise("Saved.");
    const dismiss = screen.getByRole("button", { name: "Dismiss notification" });
    act(() => dismiss.focus());
    advance(30_000);
    expect(screen.getByText("Saved.")).toBeInTheDocument();
    act(() => dismiss.blur());
    advance(5100);
    expect(screen.queryByText("Saved.")).toBeNull();
  });

  it("keeps an error until the person dismisses it", () => {
    renderToasts({ message: "Sign-out did not complete.", tone: "danger" });
    raise("Sign-out did not complete.");
    advance(10 * 60_000);
    expect(screen.getByText("Sign-out did not complete.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss notification" }));
    expect(screen.queryByText("Sign-out did not complete.")).toBeNull();
  });

  it("restarts a message raised again instead of stacking a copy", () => {
    renderToasts({ message: "Saved." });
    raise("Saved.");
    advance(4000);
    raise("Saved.");
    expect(screen.getAllByText("Saved.")).toHaveLength(1);
    advance(4000);
    expect(screen.getByText("Saved.")).toBeInTheDocument();
    advance(1100);
    expect(screen.queryByText("Saved.")).toBeNull();
  });

  it("waits while the tab is hidden", () => {
    renderToasts({ message: "4 documents uploaded" });
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    raise("4 documents uploaded");
    advance(60_000);
    expect(screen.getByText("4 documents uploaded")).toBeInTheDocument();
    visibility.mockReturnValue("visible");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    advance(5100);
    expect(screen.queryByText("4 documents uploaded")).toBeNull();
  });
});

describe("error state", () => {
  it("quotes the request id of a server fault as a reference", () => {
    render(<ErrorState error={new ApiError(502, "The server had a problem.", null, null, "a1b2c3")} />);
    expect(screen.getByText("The server had a problem.")).toBeInTheDocument();
    expect(screen.getByText("a1b2c3")).toBeInTheDocument();
  });

  it("does not for an answer the person can act on themselves", () => {
    render(<ErrorState error={new ApiError(404, "Not found.", null, null, "a1b2c3")} />);
    expect(screen.getByText("It does not exist, or you do not have access to it.")).toBeInTheDocument();
    expect(screen.queryByText("a1b2c3")).toBeNull();
  });
});
