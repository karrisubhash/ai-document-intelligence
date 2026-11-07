import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  announceArrivals,
  notificationsApi,
  useVisiblePolling,
  type AppNotification,
  type NotificationSummary,
} from "../api/notifications";
import { formatDateTime, formatRelative } from "../lib/format";
import "../styles/notifications.css";
import { Icon, type IconName } from "./icons";
import { Button, Spinner, errorMessage } from "./ui";

type Tone = "success" | "danger" | "warning" | "info" | "brand" | "neutral";

/** How each kind of news looks. Unknown kinds still render, as a plain bell. */
const KIND_LOOK: Record<string, { icon: IconName; tone: Tone; label: string }> = {
  "document.ready": { icon: "check-circle", tone: "success", label: "Document ready" },
  "document.failed": { icon: "alert-circle", tone: "danger", label: "Processing problem" },
  "document.quarantined": { icon: "shield", tone: "warning", label: "Quarantined" },
  "signup.requested": { icon: "user-plus", tone: "info", label: "Sign-up request" },
  "account.welcome": { icon: "sparkles", tone: "brand", label: "Welcome" },
};
const PLAIN_LOOK = { icon: "bell" as IconName, tone: "neutral" as Tone, label: "Notification" };

export function lookOf(kind: string) {
  return KIND_LOOK[kind] ?? PLAIN_LOOK;
}

function time(value: string | null) {
  return value ? new Date(value).getTime() : 0;
}

/**
 * The bell in the top bar: an unread count, and a panel of the latest news.
 *
 * It polls only the cheap summary (once a minute while the tab is visible,
 * and when the window regains focus), and fetches the list when the panel
 * opens - or when the summary says something new arrived, so other screens
 * can react (useNotificationArrivals) and a screen reader hears about it.
 * A failed poll keeps what was last known: the bell is a convenience, and
 * an error in it must never get in the way of the page.
 */
export default function NotificationBell() {
  const [summary, setSummary] = useState<NotificationSummary | null>(null);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<AppNotification[] | null>(null);
  const [nextPage, setNextPage] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [marking, setMarking] = useState(false);
  const [announcement, setAnnouncement] = useState("");

  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const openRef = useRef(open);
  openRef.current = open;
  // undefined until the first summary: what arrives before it is not "new".
  const seenLatest = useRef<string | null | undefined>(undefined);
  const panelId = useId();
  const headingId = useId();

  const load = useCallback(async (page = 1) => {
    setLoading(true);
    setError(null);
    try {
      const result = await notificationsApi.list({ page });
      setItems((current) => (page === 1 || !current ? result.results : [...current, ...result.results]));
      setNextPage(result.next ? page + 1 : null);
    } catch (caught) {
      setError(caught);
    } finally {
      setLoading(false);
    }
  }, []);

  const poll = useCallback(async () => {
    let fresh: NotificationSummary;
    try {
      fresh = await notificationsApi.summary();
    } catch {
      return;
    }
    const previous = seenLatest.current;
    seenLatest.current = fresh.latest_at;
    setSummary(fresh);
    if (previous === undefined || !fresh.latest_at || time(fresh.latest_at) <= time(previous)) return;

    // News arrived (or a batch notification grew) since the last look.
    try {
      const page = await notificationsApi.list();
      const arrived = page.results.filter((item) => time(item.updated_at) > time(previous));
      if (openRef.current) {
        setItems(page.results);
        setNextPage(page.next ? 2 : null);
      } else {
        setItems(null);
      }
      announceArrivals(arrived);
      const unread = arrived.filter((item) => !item.read_at);
      if (unread.length === 1) setAnnouncement(`New notification: ${unread[0].title}`);
      else if (unread.length > 1) setAnnouncement(`${unread.length} new notifications`);
    } catch {
      // The count is already right; the details can wait for the next look.
    }
  }, []);

  useVisiblePolling(() => void poll());

  const close = useCallback((returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) button.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    panel.current?.focus();
    const onPointer = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) close(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && root.current?.contains(document.activeElement)) {
        event.preventDefault();
        close(true);
      }
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);

  const toggle = () => {
    if (open) {
      close(false);
      return;
    }
    setOpen(true);
    void load(1);
  };

  const unread = summary?.unread ?? 0;

  const markRead = (item: AppNotification) => {
    if (item.read_at) return;
    const now = new Date().toISOString();
    setItems((current) => current?.map((entry) => (entry.id === item.id ? { ...entry, read_at: now } : entry)) ?? null);
    setSummary((current) => (current ? { ...current, unread: Math.max(0, current.unread - 1) } : current));
    notificationsApi.markRead(item.id).catch(() => {
      // Left unread on the server; the next poll puts the count right.
    });
  };

  const markAllRead = async () => {
    const until = summary?.latest_at ?? items?.[0]?.updated_at ?? null;
    setMarking(true);
    try {
      await notificationsApi.markAllRead(until);
      const now = new Date().toISOString();
      setItems(
        (current) =>
          current?.map((entry) =>
            entry.read_at || time(entry.updated_at) > time(until) ? entry : { ...entry, read_at: now },
          ) ?? null,
      );
      await poll();
    } catch (caught) {
      setError(caught);
    } finally {
      setMarking(false);
    }
  };

  const label = unread ? `Notifications, ${unread} unread` : "Notifications";

  return (
    <div
      className="bell"
      ref={root}
      onBlur={(event) => {
        // Tabbing out of the panel closes it, as for the menus.
        if (open && event.relatedTarget && !root.current?.contains(event.relatedTarget as Node)) {
          setOpen(false);
        }
      }}
    >
      <button
        ref={button}
        type="button"
        className={`icon-button bell-button${unread ? " has-unread" : ""}`}
        aria-label={label}
        title={label}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={toggle}
      >
        <Icon name="bell" size={18} />
        {unread ? (
          <span className="bell-count" aria-hidden="true">
            {unread > 99 ? "99+" : unread}
          </span>
        ) : null}
      </button>
      <span className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </span>

      {open ? (
        <div
          ref={panel}
          id={panelId}
          className="bell-panel"
          role="dialog"
          aria-labelledby={headingId}
          tabIndex={-1}
        >
          <div className="bell-panel-header">
            <h2 id={headingId} className="bell-panel-title">
              Notifications
            </h2>
            {unread ? (
              <Button size="sm" variant="ghost" icon="check-check" loading={marking} onClick={markAllRead}>
                Mark all as read
              </Button>
            ) : null}
          </div>

          <div className="bell-panel-body">
            {error && !items ? (
              <div className="bell-problem" role="alert">
                <p>{errorMessage(error)}</p>
                <Button size="sm" icon="refresh" onClick={() => void load(1)}>
                  Try again
                </Button>
              </div>
            ) : !items ? (
              <div className="bell-loading">
                <Spinner label="Loading notifications…" />
              </div>
            ) : !items.length ? (
              <div className="bell-empty">
                <span className="bell-empty-icon" aria-hidden="true">
                  <Icon name="bell" size={22} />
                </span>
                <p className="bell-empty-title">You are all caught up</p>
                <p className="bell-empty-text">
                  When your uploads finish processing, or something needs your decision, it will
                  show up here.
                </p>
              </div>
            ) : (
              <ul className="bell-list">
                {items.map((item) => (
                  <NotificationRow key={item.id} item={item} onOpen={() => markRead(item)} onClose={() => close(false)} />
                ))}
              </ul>
            )}
            {items && nextPage ? (
              <div className="bell-more">
                <Button size="sm" variant="ghost" loading={loading} onClick={() => void load(nextPage)}>
                  Show older
                </Button>
              </div>
            ) : null}
            {items && error ? <p className="bell-inline-error">{errorMessage(error)}</p> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function NotificationRow({
  item,
  onOpen,
  onClose,
}: {
  item: AppNotification;
  onOpen: () => void;
  onClose: () => void;
}) {
  const look = lookOf(item.kind);
  const unread = !item.read_at;
  const content = (
    <>
      <span className={`bell-item-icon bell-tone-${look.tone}`} aria-hidden="true">
        <Icon name={look.icon} size={16} />
      </span>
      <span className="bell-item-text">
        <span className="bell-item-title">{item.title}</span>
        {item.body ? <span className="bell-item-body">{item.body}</span> : null}
        <span className="bell-item-meta">
          <span className="visually-hidden">{unread ? "Unread. " : ""}</span>
          {look.label} ·{" "}
          <time dateTime={item.updated_at} title={formatDateTime(item.updated_at)}>
            {formatRelative(item.updated_at)}
          </time>
        </span>
      </span>
      {unread ? <span className="bell-item-dot" aria-hidden="true" /> : null}
    </>
  );
  const className = `bell-item${unread ? " unread" : ""}`;
  return (
    <li>
      {item.url ? (
        <Link
          to={item.url}
          className={className}
          onClick={() => {
            onOpen();
            onClose();
          }}
        >
          {content}
        </Link>
      ) : (
        <button type="button" className={className} onClick={onOpen}>
          {content}
        </button>
      )}
    </li>
  );
}
