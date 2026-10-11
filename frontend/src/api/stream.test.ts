import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, streamChat } from "./client";

/** A text/event-stream response delivered in awkward chunk boundaries. */
function eventStream(chunks: string[]) {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const DONE = { conversation_id: 4, answer: "Hello there", source: "general", abstained: false };

afterEach(() => vi.unstubAllGlobals());

describe("streamChat", () => {
  it("delivers deltas as they arrive and resolves with the authoritative done payload", async () => {
    document.cookie = "csrftoken=abc123; path=/";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        eventStream([
          ": keep-alive\n\n",
          'event: reset\ndata: {}\n\nevent: delta\ndata: {"text": "Hel',
          'lo"}\n\nevent: delta\ndata: {"text": " there"}\n\n',
          `event: done\ndata: ${JSON.stringify(DONE)}\n\n`,
        ]),
      ),
    );
    const seen: string[] = [];
    const response = await streamChat(
      { question: "hi" },
      { onDelta: (text) => seen.push(text), onReset: () => seen.push("<reset>") },
    );
    expect(seen).toEqual(["<reset>", "Hello", " there"]);
    expect(response).toEqual(DONE);
  });

  it("turns an error event into an ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(eventStream(['event: error\ndata: {"detail": "Try again."}\n\n'])),
    );
    const error = (await streamChat({ question: "hi" }).catch((e) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe("Try again.");
  });

  it("reports a JSON error response the way request() does", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Slow down." }), {
          status: 429,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const error = (await streamChat({ question: "hi" }).catch((e) => e)) as ApiError;
    expect(error.status).toBe(429);
  });
});
