import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, request, setUnauthorizedHandler } from "./client";

function respond(status: number, body: unknown = {}) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
});

describe("request", () => {
  it("sends the session cookie and never an Authorization header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(respond(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await request("/api/documents/");

    const [, init] = fetchMock.mock.calls[0];
    expect(init.credentials).toBe("same-origin");
    expect(init.headers.Authorization).toBeUndefined();
  });

  it("puts the CSRF cookie on every unsafe request", async () => {
    document.cookie = "csrftoken=abc123; path=/";
    const fetchMock = vi.fn().mockResolvedValue(respond(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    await request("/api/documents/1/archive/", { method: "POST" });

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers["X-CSRFToken"]).toBe("abc123");
  });

  it("fetches a CSRF cookie first when there is none", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(async () => {
        document.cookie = "csrftoken=fresh; path=/";
        return respond(200, { csrf_token: "fresh" });
      })
      .mockResolvedValue(respond(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    await request("/api/chat/", { method: "POST", body: { question: "q" } });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/auth/csrf/");
    expect(fetchMock.mock.calls[1][1].headers["X-CSRFToken"]).toBe("fresh");
  });

  it("raises field errors rather than swallowing them", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(respond(400, { title: ["This field is required."] })),
    );

    const error = (await request("/api/documents/", { method: "GET" }).catch(
      (e) => e,
    )) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(400);
    expect(error.fieldErrors()).toEqual({ title: "This field is required." });
  });

  it("tells the app when the session has ended", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(respond(401, { detail: "no" })));

    await expect(request("/api/documents/")).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("does not treat the session check itself as an expiry", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(respond(401, {})));

    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });

  it("turns a network failure into a status-0 error with a readable message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const error = (await request("/api/documents/").catch((e) => e)) as ApiError;
    expect(error.status).toBe(0);
    expect(error.message).toMatch(/could not be reached/);
  });

  it("reports an unreachable server as status 0 even before there is a CSRF cookie", async () => {
    // No csrftoken cookie, so the CSRF fetch is the first request - and it fails.
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const error = (await api.login("asha", "secret").catch((e) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.message).toMatch(/could not be reached/);
  });

  it("never shows an HTML error page as the message, but keeps a plain-text one", async () => {
    // Django's built-in 500 page: no 500.html and no custom handler here.
    const page =
      '\n<!doctype html>\n<html lang="en">\n<head>\n  <title>Server Error (500)</title>\n</head>\n<body>\n  <h1>Server Error (500)</h1><p></p>\n</body>\n</html>\n';
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(new Response(page, { status: 500, headers: { "Content-Type": "text/html" } }))
        .mockResolvedValueOnce(new Response("Down for maintenance until 18:00.", { status: 503 })),
    );

    const html = (await request("/api/chat/conversations/").catch((e) => e)) as ApiError;
    expect(html.status).toBe(500);
    expect(html.message).toBe("The server had a problem handling this request.");

    const text = (await request("/api/chat/conversations/").catch((e) => e)) as ApiError;
    expect(text.message).toBe("Down for maintenance until 18:00.");
  });

  it("keeps search queries out of the URL", async () => {
    document.cookie = "csrftoken=t; path=/";
    const fetchMock = vi.fn().mockResolvedValue(respond(200, { results: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await api.search({ query: "confidential salary revision" });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/search/");
    expect(init.method).toBe("POST");
    expect(url).not.toContain("salary");
  });
});

describe("collections", () => {
  it("reads every page, so nothing past the first fifty goes missing", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        respond(200, {
          count: 3,
          next: "http://testserver/api/v1/collections/?page=2",
          previous: null,
          results: [{ id: 1 }, { id: 2 }],
        }),
      )
      .mockResolvedValueOnce(
        respond(200, { count: 3, next: null, previous: "/?page=1", results: [{ id: 3 }] }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const all = await api.collections();

    expect(all.results.map((collection) => collection.id)).toEqual([1, 2, 3]);
    expect(all.next).toBeNull();
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/collections/",
      "/api/v1/collections/?page=2",
    ]);
  });
});

describe("fetchBlob", () => {
  it("turns a network failure into a status-0 error, as request() does", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const error = (await api.fileBlob(7).catch((e) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.message).toMatch(/could not be reached/);
  });

  it("leaves an aborted download an abort", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("aborted", "AbortError")));
    const error = (await api.fileBlob(7).catch((e) => e)) as DOMException;
    expect(error.name).toBe("AbortError");
  });
});

describe("upload", () => {
  /** Just enough XMLHttpRequest to answer one upload. */
  /** Answers once sent - or, with `hang`, never, until aborted. */
  function stubXhr(
    status: number,
    body: unknown,
    headers: Record<string, string> = {},
    hang = false,
  ) {
    const sent: { headers: Record<string, string> } = { headers: {} };
    class FakeXhr {
      status = status;
      responseText = JSON.stringify(body);
      upload = { onprogress: null };
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;
      open() {}
      setRequestHeader(name: string, value: string) {
        sent.headers[name] = value;
      }
      getResponseHeader(name: string) {
        return headers[name] ?? null;
      }
      send() {
        if (!hang) queueMicrotask(() => this.onload?.());
      }
      abort() {
        this.onabort?.();
      }
    }
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    return sent;
  }

  const input = {
    title: "Fee schedule",
    document_type: "POLICY" as const,
    file: new File(["x"], "fees.pdf"),
  };

  it("sends an Idempotency-Key only when given one", async () => {
    document.cookie = "csrftoken=abc123; path=/";
    let sent = stubXhr(202, { id: 1 });
    await api.upload(input, () => undefined, { idempotencyKey: "k-1" });
    expect(sent.headers["Idempotency-Key"]).toBe("k-1");

    sent = stubXhr(202, { id: 2 });
    await api.upload(input, () => undefined);
    expect(sent.headers).not.toHaveProperty("Idempotency-Key");
  });

  it("says how long a throttled upload must wait", async () => {
    document.cookie = "csrftoken=abc123; path=/";
    stubXhr(429, { detail: "Request was throttled." }, { "Retry-After": "1150" });
    const error = await api.upload(input, () => undefined).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(429);
    expect((error as ApiError).retryAfter).toBe(1150);
  });

  it("rejects a stopped upload as an abort, as fetch does", async () => {
    document.cookie = "csrftoken=abc123; path=/";
    stubXhr(202, { id: 1 }, {}, true);
    const control = new AbortController();
    const pending = api.upload(input, () => undefined, { signal: control.signal });
    await Promise.resolve();
    control.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });

    // Stopped before it began: never sent.
    const sent = stubXhr(202, { id: 2 });
    await expect(
      api.upload(input, () => undefined, { signal: control.signal }),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(sent.headers).toEqual({});
  });
});
