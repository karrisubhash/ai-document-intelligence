/**
 * Notifications: the bell's API, the administrators' attention counts, and
 * the small amount of plumbing both share - polling that stops while the
 * tab is hidden, and an event other screens can listen to when news
 * arrives (the documents list refreshes when an upload is ready).
 *
 * Everything here is text from the server, rendered as text. A
 * notification's `url` is an in-app path the server has already checked.
 */

import { useEffect, useRef, useState } from "react";

import { request } from "./client";
import type { Paginated } from "./types";

export interface AppNotification {
  id: number;
  /** e.g. "document.ready", "document.failed", "signup.requested". */
  kind: string;
  title: string;
  body: string;
  /** An in-app path, or "" for news with nowhere to go. */
  url: string;
  created_at: string;
  /** When the news in it last changed (a batch keeps updating one). */
  updated_at: string;
  read_at: string | null;
}

export interface NotificationSummary {
  unread: number;
  latest_at: string | null;
}

/** GET /api/v1/admin/attention/ - administrators only. */
export interface Attention {
  pending_signups: number;
  /** Quarantined documents included. */
  failed_documents: number;
  quarantined_documents: number;
  stalled_jobs: number;
}

export const notificationsApi = {
  list: (options: { page?: number; unread?: boolean; pageSize?: number } = {}) => {
    const params = new URLSearchParams();
    if (options.page && options.page > 1) params.set("page", String(options.page));
    if (options.unread) params.set("unread", "1");
    if (options.pageSize) params.set("page_size", String(options.pageSize));
    const query = params.toString();
    return request<Paginated<AppNotification>>(`/api/v1/notifications/${query ? `?${query}` : ""}`);
  },
  summary: () => request<NotificationSummary>("/api/v1/notifications/summary/"),
  markRead: (id: number) => request<void>(`/api/v1/notifications/${id}/read/`, { method: "POST" }),
  /** `until`: only news no newer than what the person has seen. */
  markAllRead: (until?: string | null) =>
    request<{ updated: number }>("/api/v1/notifications/read-all/", {
      method: "POST",
      body: until ? { until } : {},
    }),
  attention: () => request<Attention>("/api/v1/admin/attention/"),
};

// --- Arrivals --------------------------------------------------------------------

const ARRIVED = "adi:notifications-arrived";

/** Tell the open screens that these notifications just arrived or changed. */
export function announceArrivals(notifications: AppNotification[]) {
  if (!notifications.length) return;
  window.dispatchEvent(new CustomEvent<AppNotification[]>(ARRIVED, { detail: notifications }));
}

/**
 * Calls `onArrival` with the notifications that arrived while this screen
 * is open. The bell notices them on its next poll (within a minute, or as
 * soon as the window regains focus).
 */
export function useNotificationArrivals(onArrival: (notifications: AppNotification[]) => void) {
  const callback = useRef(onArrival);
  callback.current = onArrival;
  useEffect(() => {
    const listener = (event: Event) => callback.current((event as CustomEvent<AppNotification[]>).detail);
    window.addEventListener(ARRIVED, listener);
    return () => window.removeEventListener(ARRIVED, listener);
  }, []);
}

/** Whether a notification is about a document finishing processing. */
export function isProcessingNews(notification: AppNotification): boolean {
  return notification.kind.startsWith("document.");
}

// --- Attention badges --------------------------------------------------------------

/** The sidebar items that can carry a count (navigation.ts NavItem.badge). */
export type AttentionBadgeKey = "signups" | "processing";

const plural = (count: number, one: string, many: string) =>
  `${count.toLocaleString()} ${count === 1 ? one : many}`;

/** The count on a sidebar item, and the words a screen reader hears for it. */
export function attentionBadge(
  key: AttentionBadgeKey,
  attention: Attention | null | undefined,
): { count: number; description: string } | null {
  if (!attention) return null;
  if (key === "signups") {
    const count = attention.pending_signups;
    return count
      ? { count, description: `${plural(count, "sign-up request", "sign-up requests")} waiting` }
      : null;
  }
  const count = attention.failed_documents + attention.stalled_jobs;
  if (!count) return null;
  const parts: string[] = [];
  if (attention.failed_documents) {
    let failed = plural(attention.failed_documents, "failed document", "failed documents");
    if (attention.quarantined_documents) failed += ` (${attention.quarantined_documents} quarantined)`;
    parts.push(failed);
  }
  if (attention.stalled_jobs) parts.push(plural(attention.stalled_jobs, "stalled run", "stalled runs"));
  return { count, description: parts.join(", ") };
}

/**
 * The administrator's attention counts, polled like the bell (and again on
 * a change of route, so a decision made on one screen clears its badge on
 * the next). Null for anyone else, and until the first answer.
 */
export function useAttention(enabled: boolean, refreshKey?: unknown): Attention | null {
  const [counts, setCounts] = useState<Attention | null>(null);
  useVisiblePolling(
    () => {
      notificationsApi.attention().then(setCounts, () => {
        // Keep the last counts; a badge is not worth an error message.
      });
    },
    { enabled, refreshKey },
  );
  return enabled ? counts : null;
}

// --- Polling ---------------------------------------------------------------------

export const POLL_INTERVAL_MS = 60_000;
/** Focus and visibility events arrive in pairs; one call answers both. */
const MIN_GAP_MS = 5_000;

/**
 * Runs `tick` now, every `intervalMs` while the tab is visible, when the
 * tab becomes visible again and when the window regains focus - never while
 * hidden: a background tab polling for news nobody is looking at is load
 * on a small server for nothing. Calls closer together than a few seconds
 * are folded into one. A change of `refreshKey` (the route) polls again.
 */
export function useVisiblePolling(
  tick: () => void,
  { enabled = true, intervalMs = POLL_INTERVAL_MS, refreshKey }: {
    enabled?: boolean;
    intervalMs?: number;
    refreshKey?: unknown;
  } = {},
) {
  const tickRef = useRef(tick);
  tickRef.current = tick;
  const last = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    const run = (force = false) => {
      if (document.visibilityState === "hidden") return;
      const now = Date.now();
      if (!force && now - last.current < MIN_GAP_MS) return;
      last.current = now;
      tickRef.current();
    };
    run(true);
    const id = window.setInterval(() => run(true), intervalMs);
    const onVisible = () => {
      if (document.visibilityState === "visible") run();
    };
    const onFocus = () => run();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onFocus);
    };
  }, [enabled, intervalMs]);

  // A new screen is a natural moment to check, folded with the rest.
  const firstKey = useRef(true);
  useEffect(() => {
    if (firstKey.current) {
      firstKey.current = false;
      return;
    }
    if (!enabled || document.visibilityState === "hidden") return;
    const now = Date.now();
    if (now - last.current < MIN_GAP_MS) return;
    last.current = now;
    tickRef.current();
  }, [refreshKey, enabled]);
}
