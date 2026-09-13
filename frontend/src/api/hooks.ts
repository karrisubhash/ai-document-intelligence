import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./client";

export interface Resource<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** Present only when the call failed with a status; 404 is often normal. */
  status: number | null;
  reload: () => void;
}

/**
 * Loads one API call and re-runs it when `deps` change or `reload` is called.
 *
 * A 404 is kept as data-less rather than thrown at the UI, because several
 * of these endpoints answer 404 for the perfectly ordinary case of "the
 * pipeline has not produced this yet".
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

  useEffect(() => {
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
        setData(null);
        setError(caught);
        setStatus(caught instanceof ApiError ? caught.status : null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, nonce, ...deps]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { data, error, loading, status, reload };
}

/**
 * Calls `tick` on an interval while `active` is true.
 *
 * Used to follow a document through the pipeline: processing is
 * asynchronous by design, so a console that only showed the state at page
 * load would make a working queue look like a stuck one. The polling stops
 * as soon as nothing is in flight - there is no value in hammering an idle
 * API.
 */
export function usePolling(tick: () => void, active: boolean, intervalMs = 2500) {
  const tickRef = useRef(tick);
  tickRef.current = tick;

  useEffect(() => {
    if (!active) return;

    const id = window.setInterval(() => tickRef.current(), intervalMs);
    return () => window.clearInterval(id);
  }, [active, intervalMs]);
}
