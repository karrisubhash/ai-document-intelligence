import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chunkRecovery, isChunkLoadError, recoverFromChunkError } from "./lazy";

const STALE = new TypeError(
  "Failed to fetch dynamically imported module: https://example.test/assets/DocumentsPage-1a2b3c.js",
);

/** Settles within a tick, or reports that it did not. */
async function outcome(promise: Promise<unknown>) {
  const pending = Symbol("pending");
  const result = await Promise.race([
    promise.then(
      (value) => ({ value }),
      (error: unknown) => ({ error }),
    ),
    new Promise((resolve) => setTimeout(() => resolve(pending), 20)),
  ]);
  return result === pending ? "pending" : result;
}

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  window.sessionStorage.clear();
});

describe("isChunkLoadError", () => {
  it("recognises what each browser and Vite say when a chunk cannot be fetched", () => {
    expect(isChunkLoadError(STALE)).toBe(true);
    expect(isChunkLoadError(new TypeError("error loading dynamically imported module: x.js"))).toBe(true);
    expect(isChunkLoadError(new TypeError("Importing a module script failed."))).toBe(true);
    expect(isChunkLoadError(new Error("Unable to preload CSS for /assets/ChatPage-9f.css"))).toBe(true);
    const named = new Error("whatever");
    named.name = "ChunkLoadError";
    expect(isChunkLoadError(named)).toBe(true);
  });

  it("does not mistake an ordinary bug for a stale chunk", () => {
    expect(isChunkLoadError(new TypeError("Cannot read properties of undefined (reading 'map')"))).toBe(false);
    expect(isChunkLoadError("Failed to fetch dynamically imported module")).toBe(false);
    expect(isChunkLoadError(null)).toBe(false);
  });
});

describe("recoverFromChunkError", () => {
  it("reloads once for a stale chunk while the server answers, and stays pending", async () => {
    const reload = vi.spyOn(chunkRecovery, "reload").mockImplementation(() => undefined);
    vi.spyOn(chunkRecovery, "serverReachable").mockResolvedValue(true);
    vi.spyOn(chunkRecovery, "now").mockReturnValue(1_000_000);

    expect(await outcome(recoverFromChunkError(STALE))).toBe("pending");
    expect(reload).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem("adi-chunk-reload")).toBe("1000000");
  });

  it("does not reload again soon after the last time: the error is shown instead", async () => {
    const reload = vi.spyOn(chunkRecovery, "reload").mockImplementation(() => undefined);
    vi.spyOn(chunkRecovery, "serverReachable").mockResolvedValue(true);
    window.sessionStorage.setItem("adi-chunk-reload", "1000000");
    vi.spyOn(chunkRecovery, "now").mockReturnValue(1_000_000 + 30_000);

    expect(await outcome(recoverFromChunkError(STALE))).toEqual({ error: STALE });
    expect(reload).not.toHaveBeenCalled();
  });

  it("reloads again for a later deploy, once the guard has passed", async () => {
    const reload = vi.spyOn(chunkRecovery, "reload").mockImplementation(() => undefined);
    vi.spyOn(chunkRecovery, "serverReachable").mockResolvedValue(true);
    window.sessionStorage.setItem("adi-chunk-reload", "1000000");
    vi.spyOn(chunkRecovery, "now").mockReturnValue(1_000_000 + 10 * 60_000);

    expect(await outcome(recoverFromChunkError(STALE))).toBe("pending");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("does not reload into the browser's offline page when the server cannot be reached", async () => {
    const reload = vi.spyOn(chunkRecovery, "reload").mockImplementation(() => undefined);
    vi.spyOn(chunkRecovery, "serverReachable").mockResolvedValue(false);

    expect(await outcome(recoverFromChunkError(STALE))).toEqual({ error: STALE });
    expect(reload).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem("adi-chunk-reload")).toBeNull();
  });

  it("rethrows any other error without probing or reloading", async () => {
    const reload = vi.spyOn(chunkRecovery, "reload").mockImplementation(() => undefined);
    const probe = vi.spyOn(chunkRecovery, "serverReachable").mockResolvedValue(true);
    const bug = new TypeError("Cannot read properties of undefined (reading 'title')");

    expect(await outcome(recoverFromChunkError(bug))).toEqual({ error: bug });
    expect(probe).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });

  it("does not reload when it cannot remember having done so (storage blocked)", async () => {
    const reload = vi.spyOn(chunkRecovery, "reload").mockImplementation(() => undefined);
    vi.spyOn(chunkRecovery, "serverReachable").mockResolvedValue(true);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    expect(await outcome(recoverFromChunkError(STALE))).toEqual({ error: STALE });
    expect(reload).not.toHaveBeenCalled();
  });
});
