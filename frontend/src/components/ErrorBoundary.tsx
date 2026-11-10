import { Component, useEffect, useRef, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { isChunkLoadError } from "../lib/lazy";
import { BrandMark, Icon } from "./icons";

/*
 * What a person sees when a screen throws while rendering, instead of a
 * blank page. Two boundaries:
 *
 *   AppErrorBoundary    around everything (main.tsx). Its fallback uses no
 *                       context - no router, session or toasts - because
 *                       any of them may be what broke.
 *   RouteErrorBoundary  around the page inside the shell (Layout). The
 *                       sidebar and top bar keep working, so the way out is
 *                       one click away; moving to another screen clears it.
 *
 * Errors from API calls do not come here: the hooks hand them to the page,
 * which shows them in place (ErrorState). This is for bugs, and for a chunk
 * that would not load even after lib/lazy.ts's one reload.
 */

interface BoundaryProps {
  children: ReactNode;
  /** A change clears a fallback that is showing: the new screen gets a fresh try. */
  resetKey?: unknown;
  fallback: (props: { error: unknown; reset: () => void }) => ReactNode;
}

interface BoundaryState {
  failed: boolean;
  error: unknown;
}

export class ErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { failed: false, error: null };

  static getDerivedStateFromError(error: unknown): BoundaryState {
    return { failed: true, error };
  }

  componentDidUpdate(previous: BoundaryProps) {
    if (this.state.failed && !Object.is(previous.resetKey, this.props.resetKey)) this.reset();
  }

  // No componentDidCatch: React itself reports a caught error, with its
  // component stack in development, to the console.

  reset = () => this.setState({ failed: false, error: null });

  render() {
    if (this.state.failed) return this.props.fallback({ error: this.state.error, reset: this.reset });
    return this.props.children;
  }
}

/** The reference a person can quote: the request id of a failed API call. */
function referenceOf(error: unknown): string | null {
  return error instanceof ApiError ? error.requestId : null;
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  return "";
}

/**
 * The fallback's content: what happened in plain words, the two ways out,
 * and - folded away - the technical message and the reference to report.
 */
export function ErrorFallback({
  error,
  variant,
  onHome,
}: {
  error: unknown;
  variant: "app" | "route";
  /** Leaves for the dashboard without a page load; the app variant uses a plain link. */
  onHome?: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const chunk = isChunkLoadError(error);
  const reference = referenceOf(error);
  const message = messageOf(error);

  // The fallback replaced what the person was looking at: a screen reader
  // starts reading from here rather than from wherever focus was lost.
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
  }, []);

  return (
    <div className={`crash crash-${variant}`}>
      {variant === "app" ? <BrandMark size="lg" /> : null}
      <span className="crash-icon" aria-hidden="true">
        <Icon name={chunk ? "wifi-off" : "alert-triangle"} size={22} />
      </span>
      {/* The page's heading: the fallback stands in for the screen's own h1. */}
      <h1 className="crash-title" ref={heading} tabIndex={-1}>
        Something went wrong
      </h1>
      <p className="crash-text">
        {chunk
          ? "This screen could not be loaded. The app may have been updated since this tab was opened, or the connection dropped. Reloading fetches the current version."
          : "This screen stopped because of an unexpected error. Your documents and conversations are not affected. Reload to try again, or go back to the dashboard."}
      </p>
      <div className="crash-actions">
        <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>
          <Icon name="refresh" />
          Reload
        </button>
        {variant === "route" && onHome ? (
          <button type="button" className="btn btn-secondary" onClick={onHome}>
            <Icon name="dashboard" />
            Go to dashboard
          </button>
        ) : (
          <a className="btn btn-secondary" href="/">
            <Icon name="dashboard" />
            Go to dashboard
          </a>
        )}
      </div>
      {reference ? (
        <p className="crash-reference">
          Reference <code className="crash-code">{reference}</code> - quote it if you report this.
        </p>
      ) : null}
      {message && !chunk ? (
        <details className="crash-details">
          <summary>Technical details</summary>
          <code className="crash-code">{message}</code>
        </details>
      ) : null}
    </div>
  );
}

/** Around the whole application: nothing it renders depends on a context. */
export function AppErrorBoundary({ children }: { children: ReactNode }) {
  return (
    <ErrorBoundary
      fallback={({ error }) => (
        <div className="centered">
          <div className="crash-card">
            <ErrorFallback error={error} variant="app" />
          </div>
        </div>
      )}
    >
      {children}
    </ErrorBoundary>
  );
}

/**
 * Around the routed page, inside the shell. Keyed on the path only to
 * RESET a fallback that is showing - never to remount a working page: Ask
 * rewrites /ask to /ask/12 under a conversation in progress.
 */
export function RouteErrorBoundary({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <ErrorBoundary
      resetKey={location.pathname}
      fallback={({ error, reset }) => (
        <ErrorFallback
          error={error}
          variant="route"
          // Moving to "/" resets the boundary through resetKey. Resetting
          // first would render the failed page once more, at its own path.
          // On the dashboard already, there is no move: try it again.
          onHome={() => (location.pathname === "/" ? reset() : navigate("/"))}
        />
      )}
    >
      {children}
    </ErrorBoundary>
  );
}
