import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./client";

export interface Resource<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** The HTTP status of a failed call, when there was one. */
  status: number | null;
  reload: () => void;
  /** Replace the data locally, e.g. after a successful mutation. */
  setData: (value: T | null) => void;
}

/**
 * Loads one API call, and again when `deps` change or `reload` is called.
 *
 * Data from the previous load is kept while a reload of the same deps runs,
 * so a table does not flash empty every time it refreshes; `loading` says a
 * load is in flight either way. Responses that arrive after a newer load started are
 * discarded, so a slow early request can never overwrite a fast later one.
 */
export function useResource<T>(
  loader: () => Promise<T>,
  deps: unknown[],
  options: { enabled?: boolean } = {},
): Resource<T> {
  const enabled = options.enabled ?? true;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [nonce, setNonce] = useState(0);

  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  // New deps mean a different thing is being loaded (another document, a
  // different page), and showing the old one's data while it loads - or
  // after it fails - would put the wrong content under the new heading.
  // A reload of the SAME deps keeps its data, so a refresh never flashes.
  const key = JSON.stringify(deps);
  const lastKey = useRef(key);

  useEffect(() => {
    if (lastKey.current !== key) {
      lastKey.current = key;
      setData(null);
      setError(null);
      setStatus(null);
    }

    if (!enabled) {
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);

    loaderRef
      .current()
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
        setStatus(null);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setError(caught);
        setStatus(caught instanceof ApiError ? caught.status : null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [enabled, nonce, key]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { data, error, loading, status, reload, setData };
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
