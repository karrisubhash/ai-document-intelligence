import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type KeyboardEvent,
  type ReactElement,
  type ReactNode,
  type Ref,
  type RefObject,
} from "react";

import { ApiError } from "../api/client";
import type { DocumentStatus } from "../api/types";
import { STATUS_LABEL, STATUS_TONE, type Tone } from "../lib/labels";
import { Icon, type IconName } from "./icons";

// --- Buttons ----------------------------------------------------------------

type Variant = "primary" | "secondary" | "ghost" | "danger";

export function Button({
  variant = "secondary",
  size,
  loading = false,
  icon,
  iconEnd,
  children,
  className = "",
  disabled,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: "sm" | "lg";
  loading?: boolean;
  /** A leading icon. Decoration: the button's text still names it. */
  icon?: IconName;
  iconEnd?: IconName;
  /** A plain prop in React 19, passed through to the <button>. */
  ref?: Ref<HTMLButtonElement>;
}) {
  return (
    <button
      type={type}
      className={`btn btn-${variant}${size ? ` btn-${size}` : ""} ${className}`.trim()}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? (
        <span className="spinner spinner-inline" aria-hidden="true" />
      ) : icon ? (
        <Icon name={icon} />
      ) : null}
      {children}
      {iconEnd && !loading ? <Icon name={iconEnd} /> : null}
    </button>
  );
}

/** A square button that shows only an icon; `label` is its accessible name. */
export function IconButton({
  icon,
  label,
  size,
  className = "",
  type = "button",
  ...rest
}: Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> & {
  icon: IconName;
  label: string;
  size?: "sm";
}) {
  return (
    <button
      type={type}
      className={`icon-button${size ? ` icon-button-${size}` : ""} ${className}`.trim()}
      aria-label={label}
      title={label}
      {...rest}
    >
      <Icon name={icon} size={size === "sm" ? 14 : 18} />
    </button>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

// --- Status and badges ------------------------------------------------------

export function Badge({
  tone = "neutral",
  children,
  title,
  dot = false,
  icon,
}: {
  tone?: Tone;
  children: ReactNode;
  title?: string;
  /** A leading dot in the tone's colour; the text still carries the meaning. */
  dot?: boolean;
  icon?: IconName;
}) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {dot ? <span className="badge-dot" aria-hidden="true" /> : null}
      {icon ? <Icon name={icon} size={12} strokeWidth={2.2} /> : null}
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: DocumentStatus }) {
  const inFlight = status === "QUEUED" || status === "PROCESSING";
  return (
    <span className={`badge badge-${STATUS_TONE[status]}`}>
      <span className={`badge-dot${inFlight ? " pulse" : ""}`} aria-hidden="true" />
      {STATUS_LABEL[status]}
    </span>
  );
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <span className="spinner-wrap" role="status">
      <span className="spinner" aria-hidden="true" />
      <span className="spinner-label">{label}</span>
    </span>
  );
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="skeleton" aria-hidden="true">
      {Array.from({ length: lines }, (_, index) => (
        <span key={index} className="skeleton-line" style={{ width: `${90 - index * 12}%` }} />
      ))}
    </div>
  );
}

const AVATAR_TINTS = 6;

/** Initials on a tint chosen by the name, so a person keeps their colour everywhere. */
export function Avatar({ name, size = "md" }: { name: string; size?: "sm" | "md" | "lg" }) {
  const initials =
    name
      .split(/\s+/)
      .filter(Boolean)
      .map((part) => part[0])
      .join("")
      .slice(0, 2)
      .toUpperCase() || "?";
  let hash = 0;
  for (const character of name) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return (
    <span className={`avatar avatar-${size} avatar-tint-${hash % AVATAR_TINTS}`} aria-hidden="true">
      {initials}
    </span>
  );
}

// --- Messages ---------------------------------------------------------------

const ALERT_ICON: Record<Tone, IconName> = {
  info: "info",
  neutral: "info",
  muted: "info",
  success: "check-circle",
  warning: "alert-triangle",
  danger: "alert-circle",
};

export function Alert({
  tone = "info",
  title,
  children,
  action,
  role,
}: {
  tone?: Tone;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  /**
   * "alert" for a message that answers what the user just did but is not
   * an error. A polite status region that arrives already filled is not
   * reliably read out at all.
   */
  role?: "alert" | "status";
}) {
  return (
    <div
      className={`alert alert-${tone}`}
      role={role ?? (tone === "danger" ? "alert" : "status")}
    >
      <Icon name={ALERT_ICON[tone]} size={18} className="alert-icon" />
      <div className="alert-body">
        {title ? <p className="alert-title">{title}</p> : null}
        {children ? <div className="alert-text">{children}</div> : null}
      </div>
      {action ? <div className="alert-action">{action}</div> : null}
    </div>
  );
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

export function ErrorState({
  error,
  onRetry,
  title = "This could not be loaded",
}: {
  error: unknown;
  onRetry?: () => void;
  title?: string;
}) {
  const status = error instanceof ApiError ? error.status : null;
  const text =
    status === 403
      ? "You do not have permission to see this."
      : status === 404
        ? "It does not exist, or you do not have access to it."
        : errorMessage(error);
  // A server fault is the one a person may need to report: its request id
  // finds the matching log lines (backend/documents/observability).
  const reference =
    error instanceof ApiError && error.status >= 500 ? error.requestId : null;
  return (
    <Alert
      tone="danger"
      title={title}
      action={
        onRetry && status !== 403 && status !== 404 ? (
          <Button size="sm" icon="refresh" onClick={onRetry}>
            Try again
          </Button>
        ) : null
      }
    >
      {reference ? (
        <>
          <p>{text}</p>
          <p className="error-reference">
            Reference <code>{reference}</code>
          </p>
        </>
      ) : (
        text
      )}
    </Alert>
  );
}

export function EmptyState({
  title,
  children,
  action,
  icon = "file-text",
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  icon?: IconName;
}) {
  return (
    <div className="empty">
      <div className="empty-icon" aria-hidden="true">
        <Icon name={icon} size={22} />
      </div>
      <p className="empty-title">{title}</p>
      {children ? <div className="empty-text">{children}</div> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

// --- Layout pieces ------------------------------------------------------------

export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
  icon,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: ReactNode;
  /** A leading visual, such as the file-type tile of a document. */
  icon?: ReactNode;
}) {
  return (
    <header className="page-header">
      {icon ? <div className="page-header-icon">{icon}</div> : null}
      <div className="page-header-text">
        {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
        <h1>{title}</h1>
        {description ? <div className="page-description">{description}</div> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}

export function Card({
  title,
  leading,
  description,
  icon,
  actions,
  children,
  className = "",
  padded = true,
}: {
  title?: ReactNode;
  /** Before the title, outside the heading - e.g. a selection checkbox,
   *  which inside the <h2> would become part of the heading's name. */
  leading?: ReactNode;
  description?: ReactNode;
  icon?: IconName;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  padded?: boolean;
}) {
  return (
    <section className={`card ${className}`.trim()}>
      {title || actions ? (
        <div className="card-header">
          <div className="card-heading">
            {leading}
            {icon ? (
              <span className="card-icon" aria-hidden="true">
                <Icon name={icon} />
              </span>
            ) : null}
            <div className="card-heading-text">
              {title ? <h2 className="card-title">{title}</h2> : null}
              {description ? <p className="card-description">{description}</p> : null}
            </div>
          </div>
          {actions ? <div className="card-actions">{actions}</div> : null}
        </div>
      ) : null}
      <div className={padded ? "card-body" : "card-flush"}>{children}</div>
    </section>
  );
}

export function DescriptionList({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="dl">
      {items.map(([term, detail], index) => (
        <div className="dl-row" key={index}>
          <dt>{term}</dt>
          <dd>{detail}</dd>
        </div>
      ))}
    </dl>
  );
}

// --- Forms ----------------------------------------------------------------------

export function Field({
  label,
  hint,
  error,
  children,
  required,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  required?: boolean;
  children: (props: {
    id: string;
    "aria-describedby"?: string;
    "aria-invalid"?: boolean;
    "aria-required"?: boolean;
  }) => ReactElement;
}) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;
  return (
    <div className={`field${error ? " field-invalid" : ""}`}>
      <label htmlFor={id} className="field-label">
        {label}
        {/* Hidden from screen readers, which hear aria-required instead. */}
        {required ? <span className="required" aria-hidden="true"> *</span> : null}
      </label>
      {children({
        id,
        "aria-describedby": describedBy,
        "aria-invalid": error ? true : undefined,
        "aria-required": required || undefined,
      })}
      {hint ? (
        <p className="field-hint" id={hintId}>
          {hint}
        </p>
      ) : null}
      {error ? (
        <p className="field-error" id={errorId}>
          <Icon name="alert-circle" size={14} />
          {error}
        </p>
      ) : null}
    </div>
  );
}

// --- Tabs -------------------------------------------------------------------------

export interface TabItem {
  id: string;
  label: ReactNode;
  count?: number | null;
  icon?: IconName;
}

/**
 * WAI-ARIA tabs: arrow keys move between tabs, Home/End jump, and only the
 * selected tab is in the tab order.
 */
export function Tabs({
  tabs,
  selected,
  onSelect,
  label,
}: {
  tabs: TabItem[];
  selected: string;
  onSelect: (id: string) => void;
  label: string;
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const list = useRef<HTMLDivElement>(null);
  // More tabs than fit: the row scrolls, and fades at the edge that has more.
  const [more, setMore] = useState(false);

  useEffect(() => {
    const element = list.current;
    if (!element) return;
    const update = () =>
      setMore(element.scrollLeft + element.clientWidth < element.scrollWidth - 1);
    update();
    element.addEventListener("scroll", update, { passive: true });
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(update) : null;
    observer?.observe(element);
    return () => {
      element.removeEventListener("scroll", update);
      observer?.disconnect();
    };
  }, [tabs.length]);

  // The selected tab is never left scrolled out of sight - horizontally only,
  // so the page itself does not jump.
  useEffect(() => {
    const element = list.current;
    const tab = refs.current[selected];
    if (!element || !tab) return;
    const left = tab.offsetLeft - element.offsetLeft;
    const right = left + tab.offsetWidth;
    // Clear of the 48px fade at the edge that has more (.tabs[data-more]).
    const clearance = 56;
    if (left < element.scrollLeft) element.scrollLeft = Math.max(0, left - clearance);
    else if (right > element.scrollLeft + element.clientWidth - clearance) {
      element.scrollLeft = right - element.clientWidth + clearance;
    }
  }, [selected]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = tabs.findIndex((tab) => tab.id === selected);
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    const target = tabs[next];
    onSelect(target.id);
    refs.current[target.id]?.focus();
  };

  return (
    <div
      ref={list}
      className="tabs"
      role="tablist"
      aria-label={label}
      data-more={more || undefined}
      onKeyDown={onKeyDown}
    >
      {tabs.map((tab) => (
        <button
          key={tab.id}
          ref={(node) => {
            refs.current[tab.id] = node;
          }}
          role="tab"
          id={`tab-${tab.id}`}
          aria-selected={tab.id === selected}
          aria-controls={`panel-${tab.id}`}
          tabIndex={tab.id === selected ? 0 : -1}
          className="tab"
          onClick={() => onSelect(tab.id)}
        >
          {tab.icon ? <Icon name={tab.icon} size={15} /> : null}
          {tab.label}
          {tab.count !== undefined && tab.count !== null ? (
            <span className="tab-count">{tab.count}</span>
          ) : null}
        </button>
      ))}
    </div>
  );
}

export function TabPanel({ id, children }: { id: string; children: ReactNode }) {
  return (
    <div role="tabpanel" id={`panel-${id}`} aria-labelledby={`tab-${id}`} className="tab-panel">
      {children}
    </div>
  );
}

// --- Pagination ---------------------------------------------------------------------

export function Pagination({
  page,
  count,
  pageSize = 50,
  onPage,
}: {
  page: number;
  count: number;
  pageSize?: number;
  onPage: (page: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(count / pageSize));
  if (pages <= 1) return null;
  const first = (page - 1) * pageSize + 1;
  const last = Math.min(count, page * pageSize);
  return (
    <nav className="pagination" aria-label="Pagination">
      <span className="pagination-summary">
        {first.toLocaleString()}–{last.toLocaleString()} of {count.toLocaleString()}
      </span>
      <div className="pagination-controls">
        <Button size="sm" icon="chevron-left" onClick={() => onPage(page - 1)} disabled={page <= 1}>
          Previous
        </Button>
        <span className="pagination-page">
          Page {page} of {pages}
        </span>
        <Button size="sm" iconEnd="chevron-right" onClick={() => onPage(page + 1)} disabled={page >= pages}>
          Next
        </Button>
      </div>
    </nav>
  );
}

// --- Menus ---------------------------------------------------------------------------

const MenuClose = createContext<() => void>(() => undefined);

const ITEM_SELECTOR = '[role="menuitem"]:not(:disabled), [role="menuitemradio"]:not(:disabled)';

/**
 * A menu button (WAI-ARIA): the trigger says whether it is open, focus
 * moves into the menu when it opens, arrow keys and Home/End move between
 * items, Escape closes it and returns focus to the trigger, and a click
 * anywhere else closes it.
 */
export function Menu({
  trigger,
  triggerClassName = "btn btn-secondary",
  triggerLabel,
  align = "end",
  side = "bottom",
  className = "",
  busy = false,
  children,
}: {
  /** The trigger's content. */
  trigger: ReactNode;
  triggerClassName?: string;
  /** An accessible name, when the trigger shows only an icon. */
  triggerLabel?: string;
  align?: "start" | "end";
  side?: "bottom" | "top";
  className?: string;
  /** An action chosen from the menu is still running. */
  busy?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const menuId = useId();

  const close = useCallback(() => {
    setOpen(false);
    button.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    list.current?.querySelector<HTMLElement>(ITEM_SELECTOR)?.focus();
    const onPointer = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    // Only an Escape pressed inside the menu is the menu's: one pressed in
    // quick find, opened over it, closes quick find first.
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && root.current?.contains(document.activeElement)) {
        event.preventDefault();
        close();
      }
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = [...(list.current?.querySelectorAll<HTMLElement>(ITEM_SELECTOR) ?? [])];
    if (!items.length) return;
    const index = items.indexOf(document.activeElement as HTMLElement);
    let next: number;
    if (event.key === "ArrowDown") next = (index + 1) % items.length;
    else if (event.key === "ArrowUp") next = (index - 1 + items.length) % items.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = items.length - 1;
    else if (event.key === "Tab") {
      setOpen(false);
      return;
    } else return;
    event.preventDefault();
    items[next].focus();
  };

  return (
    <div
      className={`menu-root ${className}`.trim()}
      ref={root}
      onBlur={(event) => {
        // Focus moved somewhere else on the page (not just a click, which
        // the outside-press handler covers and Safari reports with no
        // relatedTarget at all): the menu closes behind it.
        if (event.relatedTarget && !root.current?.contains(event.relatedTarget as Node)) {
          setOpen(false);
        }
      }}
    >
      <button
        ref={button}
        type="button"
        className={triggerClassName}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={triggerLabel}
        aria-busy={busy || undefined}
        title={triggerLabel}
        onClick={() => setOpen((value) => !value)}
      >
        {trigger}
      </button>
      {open ? (
        <div
          ref={list}
          id={menuId}
          className={`menu menu-${align} menu-${side}`}
          role="menu"
          onKeyDown={onKeyDown}
        >
          <MenuClose.Provider value={close}>{children}</MenuClose.Provider>
        </div>
      ) : null}
    </div>
  );
}

export function MenuItem({
  icon,
  tone,
  onSelect,
  disabled,
  title,
  children,
}: {
  icon?: IconName;
  tone?: "danger";
  onSelect: () => void;
  disabled?: boolean;
  title?: string;
  children: ReactNode;
}) {
  const close = useContext(MenuClose);
  return (
    <button
      role="menuitem"
      type="button"
      className={`menu-item${tone ? ` menu-item-${tone}` : ""}`}
      disabled={disabled}
      title={title}
      tabIndex={-1}
      onClick={() => {
        close();
        onSelect();
      }}
    >
      {icon ? <Icon name={icon} /> : null}
      <span className="menu-item-label">{children}</span>
    </button>
  );
}

export function MenuRadioItem({
  icon,
  checked,
  onSelect,
  children,
}: {
  icon?: IconName;
  checked: boolean;
  onSelect: () => void;
  children: ReactNode;
}) {
  return (
    <button
      role="menuitemradio"
      type="button"
      aria-checked={checked}
      className="menu-item"
      tabIndex={-1}
      onClick={onSelect}
    >
      {icon ? <Icon name={icon} /> : null}
      <span className="menu-item-label">{children}</span>
      {checked ? <Icon name="check" className="menu-item-check" /> : null}
    </button>
  );
}

export function MenuSeparator() {
  return <div className="menu-separator" role="separator" />;
}

export function MenuLabel({ children, id }: { children: ReactNode; id?: string }) {
  return (
    <div className="menu-label" role="presentation" id={id}>
      {children}
    </div>
  );
}

// --- Dialogs -----------------------------------------------------------------------

/**
 * A native <dialog> opened with showModal(): the browser traps focus, makes
 * the rest of the page inert and closes on Escape, which is more reliable
 * than any hand-rolled focus trap.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = "md",
  variant = "modal",
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
  variant?: "modal" | "drawer";
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const pressedOnBackdrop = useRef(false);
  const titleId = useId();
  const descriptionId = useId();
  const wantsOpen = useRef(open);
  wantsOpen.current = open;

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    } else if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      className={`dialog dialog-${size} dialog-${variant}`}
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClose={() => {
        // The browser can close a modal dialog itself: Chromium lets a
        // second Escape through without a cancelable cancel event. While
        // the page still says open (an action is running), it reopens -
        // otherwise its state and the screen disagree and it never opens
        // again.
        const dialog = ref.current;
        if (wantsOpen.current && dialog && !dialog.open && typeof dialog.showModal === "function") {
          dialog.showModal();
        }
      }}
      onMouseDown={(event) => {
        pressedOnBackdrop.current = event.target === ref.current;
      }}
      onClick={(event) => {
        // A click on the backdrop lands on the dialog element itself - but
        // so does a text-selection drag that STARTED inside the dialog and
        // ended over the backdrop (the click goes to their common
        // ancestor). That closed the dialog and threw away what was typed.
        // Both the press and the release must be on the backdrop.
        if (event.target === ref.current && pressedOnBackdrop.current) onClose();
        pressedOnBackdrop.current = false;
      }}
    >
      {open ? (
        <div className="dialog-inner">
          <div className="dialog-header">
            <div className="dialog-heading">
              <h2 id={titleId}>{title}</h2>
              {description ? (
                <p className="dialog-description" id={descriptionId}>
                  {description}
                </p>
              ) : null}
            </div>
            <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
              <Icon name="x" size={18} />
            </button>
          </div>
          <div className="dialog-body">{children}</div>
          {footer ? <div className="dialog-footer">{footer}</div> : null}
        </div>
      ) : null}
    </dialog>
  );
}

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  tone = "danger",
  pending,
  error,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  tone?: "danger" | "primary";
  pending?: boolean;
  error?: unknown;
  onConfirm: () => void;
  onClose: () => void;
}) {
  // Not closable while its action runs: closing would look like cancelling,
  // and the action would carry on regardless.
  const close = () => {
    if (!pending) onClose();
  };
  return (
    <Dialog
      open={open}
      onClose={close}
      title={title}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={pending}>
            Cancel
          </Button>
          <Button variant={tone} onClick={onConfirm} loading={pending}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className={`confirm confirm-${tone}`}>
        <span className="confirm-icon" aria-hidden="true">
          <Icon name={tone === "danger" ? "alert-triangle" : "info"} size={20} />
        </span>
        <div className="stack confirm-body">
          {children}
          {error ? <ErrorState error={error} title="That did not work" /> : null}
        </div>
      </div>
    </Dialog>
  );
}

// --- Toasts --------------------------------------------------------------------------

interface Toast {
  id: number;
  tone: Tone;
  message: string;
}

const ToastContext = createContext<(message: string, tone?: Tone) => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);

  const dismiss = useCallback(
    (id: number) => setToasts((current) => current.filter((toast) => toast.id !== id)),
    [],
  );

  const push = useCallback(
    (message: string, tone: Tone = "success") => {
      const id = next.current++;
      setToasts((current) => [...current, { id, tone, message }]);
      window.setTimeout(() => dismiss(id), tone === "danger" ? 8000 : 4500);
    },
    [dismiss],
  );

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite" aria-relevant="additions">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast toast-${toast.tone}`} role="status">
            <Icon name={ALERT_ICON[toast.tone]} size={18} className="toast-icon" />
            <span className="toast-message">{toast.message}</span>
            <button
              type="button"
              className="toast-close"
              aria-label="Dismiss notification"
              onClick={() => dismiss(toast.id)}
            >
              <Icon name="x" size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

// --- Selection -------------------------------------------------------------------------

/** A row's checkbox for a bulk action; `indeterminate` for a "select all" box. */
export function SelectBox({
  label,
  checked,
  indeterminate = false,
  disabled,
  title,
  inputRef,
  onChange,
}: {
  label: string;
  checked: boolean;
  indeterminate?: boolean;
  disabled?: boolean;
  title?: string;
  /** For moving focus here, e.g. once the controls that had it are gone. */
  inputRef?: RefObject<HTMLInputElement | null>;
  onChange: (checked: boolean) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  // Only settable as a property - there is no indeterminate attribute.
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return (
    <input
      ref={(node) => {
        ref.current = node;
        if (inputRef) inputRef.current = node;
      }}
      type="checkbox"
      className="select-box"
      aria-label={label}
      title={title}
      checked={checked}
      disabled={disabled}
      onChange={(event) => onChange(event.target.checked)}
    />
  );
}

export function plural(count: number, word: string) {
  return count === 1 ? word : `${word}s`;
}

// --- Misc ------------------------------------------------------------------------------

export function Meter({ value, label }: { value: number; label: string }) {
  const clamped = Math.max(0, Math.min(1, value));
  return (
    <span className="meter" role="meter" aria-valuemin={0} aria-valuemax={1} aria-valuenow={clamped} aria-label={label}>
      <span className="meter-fill" style={{ width: `${clamped * 100}%` }} />
    </span>
  );
}

export function useDocumentTitle(title: string) {
  useEffect(() => {
    const previous = document.title;
    document.title = title ? `${title} · Document Intelligence` : "Document Intelligence";
    return () => {
      document.title = previous;
    };
  }, [title]);
}

/** Stable object identity for a set of query params, keyed by value. */
export function useStable<T>(value: T): T {
  const key = JSON.stringify(value);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  return useMemo(() => value, [key]);
}
