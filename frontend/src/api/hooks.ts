import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import {
  DEFAULT_MAX_AGE_MS,
  cacheGeneration,
  dropCached,
  readCached,
  requestKeyOf,
  writeCached,
} from "./cache";
import { ApiError } from "./client";

export { invalidateResources } from "./cache";

export interface Resource<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** The HTTP status of a failed call, when there was one. */
  status: number | null;
  /**
   * The data on screen is what this request returned on an earlier visit,
   * shown while it reloads; false once this visit's own response is in.
   */
  stale: boolean;
  reload: () => void;
  /** Replace the data locally, e.g. after a successful mutation. */
  setData: (value: T | null) => void;
}

export interface ResourceOptions {
  enabled?: boolean;
  /**
   * Show what this request returned on a recent visit while it reloads
   * (default true). Off for data that must never be seen out of date, even
   * for the moment a reload takes.
   */
  cache?: boolean;
  /** How old a remembered result may be and still be shown, in ms (default 60 s). */
  maxAge?: number;
}

/**
 * Loads one API call, and again when `deps` change or `reload` is called.
 *
 * Data from the previous load is kept while a reload of the same deps runs,
 * so a table does not flash empty every time it refreshes; `loading` says a
 * load is in flight either way. Responses that arrive after a newer load started are
 * discarded, so a slow early request can never overwrite a fast later one.
 *
 * Stale-while-revalidate: a screen revisited within a minute shows what the
 * same request returned last time AT ONCE, flagged `stale`, while it loads
 * again - it always loads again. Only a loader that returns the tagged
 * promise of one API call is remembered (api/cache.ts explains the keys);
 * any change made through the client forgets everything. A reload that
 * fails after showing remembered data shows the failure exactly as a first
 * load would, rather than leaving the old data standing.
 */
export function useResource<T>(
  loader: () => Promise<T>,
  deps: unknown[],
  options: ResourceOptions = {},
): Resource<T> {
  const enabled = options.enabled ?? true;
  const useCache = options.cache ?? true;
  const maxAge = options.maxAge ?? DEFAULT_MAX_AGE_MS;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [stale, setStale] = useState(false);
  const [nonce, setNonce] = useState(0);

  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  /** The cache key of the request on screen, when it has one. */
  const requestKey = useRef<string | null>(null);
  /** Mirrors `stale` for the load's callbacks. */
  const fromCache = useRef(false);

  // New deps mean a different thing is being loaded (another document, a
  // different page), and showing the old one's data while it loads - or
  // after it fails - would put the wrong content under the new heading.
  // A reload of the SAME deps keeps its data, so a refresh never flashes.
  const key = JSON.stringify(deps);
  // Unset at first, so the first load counts as new deps and may start
  // from the cache.
  const lastKey = useRef<string | null>(null);

  // A layout effect, so that remembered data replaces the empty first
  // render before the browser paints it: no skeleton flashes in between.
  useLayoutEffect(() => {
    const fresh = lastKey.current !== key;
    if (fresh) {
      lastKey.current = key;
      setData(null);
      setError(null);
      setStatus(null);
      setStale(false);
      fromCache.current = false;
    }

    if (!enabled) {
      setLoading(false);
      return;
    }

    let cancelled = false;
    const startedAt = cacheGeneration();
    const pending = loaderRef.current();
    const cacheKey = useCache ? requestKeyOf(pending) : null;
    requestKey.current = cacheKey;
    if (fresh && cacheKey) {
      const remembered = readCached<T>(cacheKey, maxAge);
      if (remembered !== undefined) {
        setData(remembered);
        setStale(true);
        fromCache.current = true;
      }
    }
    setLoading(true);

    pending
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
        setStatus(null);
        setStale(false);
        fromCache.current = false;
        if (cacheKey) writeCached(cacheKey, result, startedAt);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        if (fromCache.current) {
          // Shown from an earlier visit, never confirmed: gone, refused or
          // broken now, so it is not shown any longer.
          setData(null);
          setStale(false);
          fromCache.current = false;
        }
        if (cacheKey) dropCached(cacheKey);
        setError(caught);
        setStatus(caught instanceof ApiError ? caught.status : null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // The loader is read through a ref; deps arrive as `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, nonce, key, useCache, maxAge]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  // A local replacement is what the server now holds (the page made the
  // change), so a revisit starts from it too.
  const replace = useCallback((value: T | null) => {
    setData(value);
    setStale(false);
    fromCache.current = false;
    const cacheKey = requestKey.current;
    if (!cacheKey) return;
    if (value === null) dropCached(cacheKey);
    else writeCached(cacheKey, value);
  }, []);

  return { data, error, loading, status, stale, reload, setData: replace };
}

/**
 * Calls `tick` on an interval while `active` is true, and not at all while
 * the tab is hidden - a background tab polling a document through OCR is
 * load on the API that nobody is looking at.
 */
export function usePolling(tick: () => void, active: boolean, intervalMs = 3000) {
  const tickRef = useRef(tick);
  tickRef.current = tick;

  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") tickRef.current();
    }, intervalMs);
    return () => window.clearInterval(id);
  }, [active, intervalMs]);
}

export function useDebouncedValue<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}

/**
 * An explicit outcome, because `undefined` cannot tell a failed call from a
 * successful one that returns nothing (a 204 DELETE).
 */
export type ActionResult<R> = { ok: true; value: R } | { ok: false; error: unknown };

/**
 * Runs an async action with pending and error state, for buttons and forms.
 * The error is returned rather than thrown into React, so the caller decides
 * whether it belongs inline, in a toast, or on a field.
 */
export function useAction<A extends unknown[], R>(action: (...args: A) => Promise<R>) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const actionRef = useRef(action);
  actionRef.current = action;

  const run = useCallback(async (...args: A): Promise<ActionResult<R>> => {
    setPending(true);
    setError(null);
    try {
      return { ok: true, value: await actionRef.current(...args) };
    } catch (caught) {
      setError(caught);
      return { ok: false, error: caught };
    } finally {
      setPending(false);
    }
  }, []);

  return { run, pending, error, clearError: () => setError(null) };
}
