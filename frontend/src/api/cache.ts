/**
 * A short memory of what GET requests returned, so a screen revisited shows
 * what it showed last WHILE it reloads (stale-while-revalidate), instead of
 * skeletons on every visit.
 *
 * Keys are the requests themselves: client.ts tags the promise of every GET
 * with its path (and the promises of a few composite loaders with a name of
 * their own), and useResource caches a loader's result only when the
 * loader returned such a tagged promise unchanged. A loader that transforms
 * a response returns a new, untagged promise and is never cached - so two
 * screens can never be handed each other's shapes under one key.
 *
 * What it never does:
 *   - skip a request. Every mount still loads (some reads are audited - a
 *     document view writes an audit row - and must keep doing so); the
 *     cache only decides what is on screen while that load runs. The one
 *     merge is of IDENTICAL requests in flight at the same moment.
 *   - outlive a change. Any request that can change data (POST, PUT, PATCH,
 *     DELETE, an upload, a chat turn) drops every entry and every shared
 *     in-flight request as soon as the server has answered it; so does a
 *     401, so one person's data is never shown to the next to sign in.
 *   - keep anything long: an entry is shown for at most a minute, and only
 *     in this tab's memory.
 */

interface Entry {
  data: unknown;
  at: number;
}

/** Older than this, an entry is not worth showing: a skeleton is more honest. */
export const DEFAULT_MAX_AGE_MS = 60_000;

/** Enough for every screen a person moves between; the oldest go first. */
const MAX_ENTRIES = 60;

const entries = new Map<string, Entry>();
const inflight = new Map<string, Promise<unknown>>();
const keys = new WeakMap<Promise<unknown>, string>();
let generation = 0;

/** Marks a promise as the result of the request named `key`. */
export function tagRequest<T>(promise: Promise<T>, key: string): Promise<T> {
  keys.set(promise, key);
  return promise;
}

/** The request a promise is the result of, if it is one client.ts tagged. */
export function requestKeyOf(promise: unknown): string | null {
  return promise instanceof Promise ? (keys.get(promise) ?? null) : null;
}

/**
 * The same GET already on its way, or a new one. Two screens (or React's
 * development double mount) asking at once share one request.
 */
export function shareInflight<T>(key: string, start: () => Promise<T>): Promise<T> {
  const running = inflight.get(key) as Promise<T> | undefined;
  if (running) return running;
  const promise = start();
  inflight.set(key, promise);
  const forget = () => {
    if (inflight.get(key) === promise) inflight.delete(key);
  };
  promise.then(forget, forget);
  return promise;
}

/**
 * Changes whenever the cache is invalidated. A load records it when it
 * starts, and its result is cached only if it has not changed since: a
 * response that left the server before a change is not kept as current.
 */
export function cacheGeneration(): number {
  return generation;
}

export function readCached<T>(key: string, maxAge = DEFAULT_MAX_AGE_MS): T | undefined {
  const entry = entries.get(key);
  if (!entry) return undefined;
  if (Date.now() - entry.at > maxAge) return undefined;
  return entry.data as T;
}

export function writeCached(key: string, data: unknown, startedAt: number = generation): void {
  if (startedAt !== generation) return;
  const now = Date.now();
  entries.delete(key);
  entries.set(key, { data, at: now });
  // Drop what could no longer be shown, then the oldest beyond the cap.
  for (const [name, entry] of entries) {
    if (now - entry.at > DEFAULT_MAX_AGE_MS) entries.delete(name);
  }
  while (entries.size > MAX_ENTRIES) {
    const oldest = entries.keys().next().value;
    if (oldest === undefined) break;
    entries.delete(oldest);
  }
}

export function dropCached(key: string): void {
  entries.delete(key);
}

/**
 * Forgets cached results - all of them, or those whose key starts with
 * `prefix` (a path such as "/api/v1/documents/"). Requests already in
 * flight are no longer shared with later callers, and their results are
 * not kept. For changes the client cannot see on its own (a server push);
 * changes made through client.ts already call it.
 */
export function invalidateResources(prefix?: string): void {
  generation += 1;
  if (prefix === undefined) {
    entries.clear();
    inflight.clear();
    return;
  }
  for (const key of [...entries.keys()]) if (key.startsWith(prefix)) entries.delete(key);
  for (const key of [...inflight.keys()]) if (key.startsWith(prefix)) inflight.delete(key);
}
