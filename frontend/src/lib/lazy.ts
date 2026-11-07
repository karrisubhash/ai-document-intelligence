import { lazy as reactLazy, type ComponentType, type LazyExoticComponent } from "react";

/*
 * React.lazy, plus recovery from the one failure a deploy guarantees.
 *
 * Every screen is its own content-hashed chunk, and the edge image carries
 * only the current build's chunks. A tab opened before a deploy still runs
 * the old index, so its next route change asks for a chunk that no longer
 * exists - and React.lazy, given a rejected import, throws during render:
 * the page went blank. Here a failed chunk load makes ONE guarded hard
 * reload instead, which fetches the new index and its chunks. The guard (a
 * timestamp in sessionStorage) stops a chunk that is genuinely broken from
 * reloading the tab forever: a second failure soon after the first is left
 * to the error boundary, which says so and offers a Reload button.
 *
 * Named `lazy` and called exactly like React's, so a route added with
 * `lazy(() => import("./pages/X"))` gets the recovery by importing this
 * module instead of React's export.
 */

const RELOADED_AT = "adi-chunk-reload";

/** A second chunk failure within this window is not a stale tab: show it. */
const GUARD_MS = 60_000;

// What browsers and Vite say when a dynamic import or its preloaded CSS
// cannot be fetched: Chromium, Firefox, Safari, and Vite's preload helper.
const CHUNK_FAILURE =
  /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|Unable to preload CSS|Loading (CSS )?chunk [\w-]+ failed/i;

export function isChunkLoadError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return error.name === "ChunkLoadError" || CHUNK_FAILURE.test(error.message);
}

/**
 * The side effects, as an object so tests can replace them: jsdom's
 * location.reload cannot be spied on directly.
 */
export const chunkRecovery = {
  reload(): void {
    window.location.reload();
  },
  /**
   * Whether the server answers at all. A chunk that fails because the
   * network is down would fail again after a reload - and the reload itself
   * would replace the app with the browser's offline page, taking whatever
   * was typed with it.
   */
  async serverReachable(): Promise<boolean> {
    if (typeof navigator !== "undefined" && navigator.onLine === false) return false;
    try {
      const response = await fetch(window.location.pathname, { method: "HEAD", cache: "no-store" });
      return response.ok;
    } catch {
      return false;
    }
  },
  now(): number {
    return Date.now();
  },
};

function lastReload(): number | null {
  try {
    const value = Number(window.sessionStorage.getItem(RELOADED_AT));
    return Number.isFinite(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
}

function markReload(at: number): boolean {
  try {
    window.sessionStorage.setItem(RELOADED_AT, String(at));
    return true;
  } catch {
    // Without somewhere to remember the reload there is no guard against a
    // loop, so there is no automatic reload either: the boundary offers one.
    return false;
  }
}

/**
 * Reloads the page once for a stale chunk, and otherwise rethrows. Resolves
 * never when it reloads (the page is going away), so nothing renders the
 * error in the meantime.
 */
export async function recoverFromChunkError(error: unknown): Promise<never> {
  if (isChunkLoadError(error)) {
    const now = chunkRecovery.now();
    const previous = lastReload();
    const guarded = previous !== null && now - previous < GUARD_MS;
    if (!guarded && (await chunkRecovery.serverReachable()) && markReload(now)) {
      chunkRecovery.reload();
      return new Promise<never>(() => undefined);
    }
  }
  throw error;
}

// `any` exactly as React.lazy's own signature has it: a page of any props.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function lazy<T extends ComponentType<any>>(
  load: () => Promise<{ default: T }>,
): LazyExoticComponent<T> {
  return reactLazy(() => load().catch(recoverFromChunkError));
}
