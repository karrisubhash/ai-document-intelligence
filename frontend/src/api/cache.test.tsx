import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { invalidateResources, readCached } from "./cache";
import { api, request } from "./client";
import { useResource, type ResourceOptions } from "./hooks";

function respond(status: number, body: unknown) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A fetch whose GET answers are released by the test, one by one. */
function controlledFetch() {
  const waiting: ((response: Response) => void)[] = [];
  const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") return Promise.resolve(respond(200, { ok: true }));
    return new Promise<Response>((resolve) => waiting.push(resolve));
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    fetchMock,
    answer: async (body: unknown, status = 200) => {
      const next = waiting.shift();
      if (!next) throw new Error("no request is waiting");
      await act(async () => next(respond(status, body)));
    },
    pending: () => waiting.length,
  };
}

interface Item {
  name: string;
}

function Probe({
  path = "/api/v1/things/",
  options,
  transform = false,
}: {
  path?: string;
  options?: ResourceOptions;
  transform?: boolean;
}) {
  const resource = useResource<Item>(
    () => (transform ? request<Item>(path).then((item) => ({ name: item.name.toUpperCase() })) : request<Item>(path)),
    [path],
    options,
  );
  return (
    <output data-testid="probe">
      {resource.data ? resource.data.name : resource.error ? "failed" : "empty"}
      {resource.stale ? " (stale)" : ""}
    </output>
  );
}

const shown = () => screen.getByTestId("probe").textContent;

beforeEach(() => {
  document.cookie = "csrftoken=token; path=/";
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("stale-while-revalidate", () => {
  it("shows what a revisited screen showed last at once, and still reloads it", async () => {
    const server = controlledFetch();
    const first = render(<Probe />);
    expect(shown()).toBe("empty");
    await server.answer({ name: "first" });
    expect(shown()).toBe("first");
    first.unmount();

    render(<Probe />);
    // On the first paint, before any answer: the remembered data, flagged.
    expect(shown()).toBe("first (stale)");
    expect(server.pending()).toBe(1);
    await server.answer({ name: "second" });
    expect(shown()).toBe("second");
    expect(server.fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows a skeleton again once the remembered data is over a minute old", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    const server = controlledFetch();
    render(<Probe />).unmount();
    await server.answer({ name: "first" });
    vi.setSystemTime(Date.now() + 61_000);
    render(<Probe />);
    expect(shown()).toBe("empty");
  });

  it("shares identical requests made at the same moment", async () => {
    const server = controlledFetch();
    render(
      <>
        <Probe />
        <Probe />
      </>,
    );
    expect(server.fetchMock).toHaveBeenCalledTimes(1);
    await server.answer({ name: "shared" });
    expect(screen.getAllByTestId("probe").map((probe) => probe.textContent)).toEqual([
      "shared",
      "shared",
    ]);
  });

  it("forgets everything once a change has been answered", async () => {
    const server = controlledFetch();
    const first = render(<Probe />);
    await server.answer({ name: "before" });
    first.unmount();

    await act(() => api.deleteCollection(4));
    render(<Probe />);
    expect(shown()).toBe("empty");
  });

  it("keeps a read-only POST (search) from forgetting anything", async () => {
    const server = controlledFetch();
    const first = render(<Probe />);
    await server.answer({ name: "kept" });
    first.unmount();

    await act(() => api.search({ query: "fees" }));
    render(<Probe />);
    expect(shown()).toBe("kept (stale)");
  });

  it("does not remember a response that left the server before a change", async () => {
    const server = controlledFetch();
    render(<Probe />);
    // The list is on its way when something is deleted elsewhere.
    await act(() => api.deleteCollection(4));
    await server.answer({ name: "from before the delete" });
    expect(shown()).toBe("from before the delete");
    expect(readCached("/api/v1/things/")).toBeUndefined();
  });

  it("does not share a request that started before a change with one asked after it", async () => {
    const server = controlledFetch();
    void request("/api/v1/things/");
    await act(() => api.deleteCollection(4));
    void request("/api/v1/things/");
    expect(server.fetchMock.mock.calls.filter(([, init]) => !init?.method || init.method === "GET")).toHaveLength(2);
  });

  it("forgets everything when the session turns out to be gone", async () => {
    const server = controlledFetch();
    const first = render(<Probe />);
    await server.answer({ name: "someone's" });
    first.unmount();

    const other = render(<Probe path="/api/v1/other/" />);
    await server.answer({ detail: "Authentication credentials were not provided." }, 401);
    other.unmount();

    render(<Probe />);
    expect(shown()).toBe("empty");
  });

  it("stops showing remembered data when its reload fails", async () => {
    const server = controlledFetch();
    const first = render(<Probe />);
    await server.answer({ name: "deleted since" });
    first.unmount();

    render(<Probe />);
    expect(shown()).toBe("deleted since (stale)");
    await server.answer({ detail: "Not found." }, 404);
    // Exactly what a first load that failed shows.
    expect(shown()).toBe("failed");
    expect(readCached("/api/v1/things/")).toBeUndefined();
  });

  it("can be turned off per call", async () => {
    const server = controlledFetch();
    const first = render(<Probe options={{ cache: false }} />);
    await server.answer({ name: "private" });
    first.unmount();
    render(<Probe options={{ cache: false }} />);
    expect(shown()).toBe("empty");
  });

  it("never remembers a loader that reshapes its response", async () => {
    const server = controlledFetch();
    const first = render(<Probe transform />);
    await server.answer({ name: "raw" });
    expect(shown()).toBe("RAW");
    first.unmount();

    // A plain loader of the same path must not be handed the reshaped value.
    render(<Probe />);
    expect(shown()).toBe("empty");
  });

  it("starts a revisit from data the page replaced after its own change", async () => {
    const server = controlledFetch();
    let replace: (value: Item | null) => void = () => undefined;
    function Editor() {
      const resource = useResource<Item>(() => request<Item>("/api/v1/things/"), []);
      replace = resource.setData;
      return <output data-testid="probe">{resource.data?.name ?? "empty"}</output>;
    }
    const first = render(<Editor />);
    await server.answer({ name: "old title" });
    act(() => replace({ name: "new title" }));
    first.unmount();

    render(<Editor />);
    expect(shown()).toBe("new title");
    await waitFor(() => expect(server.pending()).toBe(1));
  });

  it("can be told to forget by path", async () => {
    const server = controlledFetch();
    const first = render(<Probe />);
    await server.answer({ name: "things" });
    first.unmount();
    invalidateResources("/api/v1/things/");
    render(<Probe />);
    expect(shown()).toBe("empty");
  });
});
