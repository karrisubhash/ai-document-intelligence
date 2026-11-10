import {
  Fragment,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";

import { attentionBadge, useAttention, type Attention } from "../api/notifications";
import { useAuth, useMe } from "../auth/AuthContext";
import { displayName } from "../lib/format";
import { ROLE_LABEL } from "../lib/labels";
import { prefetchRoute } from "../lib/lazy";
import { setTheme, useTheme } from "../lib/theme";
import CommandPalette from "./CommandPalette";
import { RouteErrorBoundary } from "./ErrorBoundary";
import { BrandMark, Icon } from "./icons";
import ShortcutsDialog, { IS_MAC } from "./ShortcutsDialog";
import { PRIMARY, adminItems, sectionFor, type NavItem } from "./navigation";
import NotificationBell from "./NotificationBell";
import {
  Avatar,
  Kbd,
  Menu,
  MenuItem,
  MenuLabel,
  MenuRadioItem,
  MenuSeparator,
  useToast,
} from "./ui";

const COLLAPSED_KEY = "adi-sidebar";

export interface Crumb {
  label: string;
  /** Where it leads; the last crumb, the page itself, has none. */
  to?: string;
}

const TrailContext = createContext<(trail: Crumb[] | null) => void>(() => undefined);

/**
 * A page deeper than its navigation entry (a document) names itself in the
 * top bar - "Documents › Academic Regulations R20" - instead of a second
 * breadcrumb line of its own. Cleared when the page goes.
 */
export function usePageTrail(trail: Crumb[] | null) {
  const setTrail = useContext(TrailContext);
  const key = trail ? JSON.stringify(trail) : "";
  useEffect(() => {
    setTrail(trail);
    return () => setTrail(null);
    // The trail is compared by value (key): a new array each render is the same trail.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, setTrail]);
}

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === "collapsed";
  } catch {
    return false;
  }
}

/** Typing goes to the field, not to a page-wide shortcut. */
function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLInputElement) {
    return !["checkbox", "radio", "button", "submit", "reset", "range", "color", "file"].includes(
      target.type,
    );
  }
  return false;
}

/**
 * The field "/" goes to: the page's search or question box. A page marks it
 * with data-primary-search; until it does, its first search input, or Ask's
 * question box.
 */
function primaryField(): HTMLInputElement | HTMLTextAreaElement | null {
  const main = document.getElementById("main");
  // In order of preference, not of position on the page.
  for (const selector of ["[data-primary-search]", 'input[type="search"]', "textarea#composer"]) {
    const field = main?.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector);
    if (field && !field.disabled && !field.closest("[hidden], [inert]")) return field;
  }
  return null;
}

function NavList({
  items,
  onNavigate,
  attention,
}: {
  items: NavItem[];
  onNavigate: () => void;
  /** Counts for the items that carry a badge (administrators only). */
  attention?: Attention | null;
}) {
  return (
    <ul className="nav-list">
      {items.map((item) => {
        // The count is a description, not part of the name: the link stays
        // "Users" to everyone (and to the e2e specs), and a screen reader
        // hears "3 sign-up requests waiting" after it.
        const badge = item.badge ? attentionBadge(item.badge, attention) : null;
        const describedBy = badge ? `nav-badge-${item.badge}` : undefined;
        return (
          <li key={item.to}>
            <NavLink
              to={item.to}
              end={item.end}
              className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
              onClick={onNavigate}
              // The screen's code is fetched while the pointer is on its way,
              // so the click does not wait for it.
              onPointerEnter={() => prefetchRoute(item.to)}
              onFocus={() => prefetchRoute(item.to)}
              title={item.label}
              aria-describedby={describedBy}
            >
              <Icon name={item.icon} size={18} className="nav-icon" />
              <span className="nav-label">{item.label}</span>
              {badge ? (
                <span className={`nav-badge nav-badge-${item.badge}`} aria-hidden="true">
                  {badge.count > 99 ? "99+" : badge.count}
                </span>
              ) : null}
            </NavLink>
            {badge ? (
              <span id={describedBy} hidden>
                {badge.description}
              </span>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * Who is signed in, their account, the theme - the one place it is chosen
 * in the shell; quick find has the same commands - and signing out.
 */
function UserMenu({ onSignOut, onShortcuts }: { onSignOut: () => void; onShortcuts: () => void }) {
  const me = useMe();
  const navigate = useNavigate();
  const [theme] = useTheme();
  const themeLabel = useId();
  const name = displayName(me);
  const role = me.role ? ROLE_LABEL[me.role] : "No role";

  return (
    <Menu
      className="user-menu"
      triggerClassName="user-button"
      side="top"
      align="start"
      trigger={
        <>
          <Avatar name={name} />
          <span className="user-text">
            <span className="user-name">{name}</span>
            <span className="user-meta">{role}</span>
          </span>
          <Icon name="chevrons-up-down" className="user-chevron" />
        </>
      }
    >
      <MenuLabel>
        Signed in as <strong>{me.username}</strong>
        {me.department ? <span className="menu-label-line">{me.department}</span> : null}
      </MenuLabel>
      <MenuItem icon="key" onSelect={() => navigate("/account")}>
        Account and password
      </MenuItem>
      <MenuItem icon="keyboard" onSelect={onShortcuts}>
        Keyboard shortcuts
      </MenuItem>
      <MenuSeparator />
      <div role="group" aria-labelledby={themeLabel}>
        <MenuLabel id={themeLabel}>Theme</MenuLabel>
        <MenuRadioItem icon="sun" checked={theme === "light"} onSelect={() => setTheme("light")}>
          Light
        </MenuRadioItem>
        <MenuRadioItem icon="moon" checked={theme === "dark"} onSelect={() => setTheme("dark")}>
          Dark
        </MenuRadioItem>
        <MenuRadioItem icon="monitor" checked={theme === "system"} onSelect={() => setTheme("system")}>
          System
        </MenuRadioItem>
      </div>
      <MenuSeparator />
      <MenuItem icon="log-out" onSelect={onSignOut}>
        Sign out
      </MenuItem>
    </Menu>
  );
}

export default function Layout({ children }: { children: ReactNode }) {
  const { can, logout } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const [navOpen, setNavOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [trail, setTrail] = useState<Crumb[] | null>(null);
  const navToggle = useRef<HTMLButtonElement>(null);
  const location = useLocation();
  const admin = adminItems(can);
  const section = sectionFor(location.pathname, admin);
  // Sidebar badges for administrators; checked again on each new screen.
  const attention = useAttention(admin.some((item) => item.badge), location.pathname);

  // Moving to a new screen closes the mobile menu and puts focus where a
  // screen-reader user expects it: the start of the new content.
  useEffect(() => {
    setNavOpen(false);
    // Not when the page only rewrote its own URL (the first answer of a new
    // conversation moves /ask to /ask/12): focus was pulled out of the
    // composer the user was typing in.
    if ((location.state as { keepFocus?: boolean } | null)?.keepFocus) return;
    document.getElementById("main")?.focus({ preventScroll: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  // The drawer exists on narrow screens only. Widening the window closes it,
  // or it would reappear already open the next time the window narrows.
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const wide = window.matchMedia("(min-width: 861px)");
    const onChange = () => {
      if (wide.matches) setNavOpen(false);
    };
    wide.addEventListener?.("change", onChange);
    return () => wide.removeEventListener?.("change", onChange);
  }, []);

  // While the drawer is open, focus moves into it, the page behind it is
  // inert (see the workspace below), and Escape closes it and returns focus
  // to the button that opened it.
  useEffect(() => {
    if (!navOpen) return;
    document.querySelector<HTMLElement>(".sidebar .nav-link")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (document.querySelector("dialog[open]")) return;
      setNavOpen(false);
      navToggle.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);

  // Ctrl+K (Cmd+K on a Mac) opens quick find from anywhere.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // "?" shows the shortcuts and "/" goes to the page's search or question
  // box - only while nothing is being typed and no dialog is open, so the
  // keys still type themselves in every field.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (isTyping(event.target) || document.querySelector("dialog[open]")) return;
      if (event.key === "?") {
        event.preventDefault();
        setShortcutsOpen(true);
      } else if (event.key === "/") {
        const field = primaryField();
        if (!field) return;
        event.preventDefault();
        field.focus();
        field.select?.();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const toggleCollapsed = () => {
    setCollapsed((value) => {
      try {
        if (value) window.localStorage.removeItem(COLLAPSED_KEY);
        else window.localStorage.setItem(COLLAPSED_KEY, "collapsed");
      } catch {
        // Remembered for this visit only.
      }
      return !value;
    });
  };

  const signOut = useCallback(() => {
    logout().then(
      () => navigate("/", { replace: true }),
      () =>
        toast(
          "Sign-out did not complete - you are still signed in. Check your connection and try again.",
          "danger",
        ),
    );
  }, [logout, navigate, toast]);

  return (
    <div className={`app${navOpen ? " nav-open" : ""}${collapsed ? " nav-collapsed" : ""}`}>
      <a href="#main" className="skip-link">
        Skip to content
      </a>

      <aside className="sidebar" aria-label="Main navigation">
        <div className="sidebar-header">
          <div className="brand">
            <BrandMark />
            <span className="brand-name">Document Intelligence</span>
          </div>
        </div>
        <nav className="sidebar-nav">
          <p className="nav-section">Workspace</p>
          <NavList items={PRIMARY} onNavigate={() => setNavOpen(false)} />
          {admin.length ? (
            <>
              <p className="nav-section">Administration</p>
              <NavList items={admin} onNavigate={() => setNavOpen(false)} attention={attention} />
            </>
          ) : null}
        </nav>
        <div className="sidebar-footer">
          <UserMenu onSignOut={signOut} onShortcuts={() => setShortcutsOpen(true)} />
        </div>
      </aside>

      <div className="scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />

      <div className="workspace" inert={navOpen || undefined}>
        <header className="topbar">
          <button
            ref={navToggle}
            type="button"
            className="icon-button nav-toggle"
            aria-label={navOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={navOpen}
            onClick={() => setNavOpen((value) => !value)}
          >
            <Icon name="menu" size={18} />
          </button>
          <button
            type="button"
            className="icon-button sidebar-toggle"
            aria-label={collapsed ? "Expand the sidebar" : "Collapse the sidebar"}
            aria-pressed={collapsed}
            title={collapsed ? "Expand the sidebar" : "Collapse the sidebar"}
            onClick={toggleCollapsed}
          >
            <Icon name="panel-left" size={18} />
          </button>
          {trail ? (
            <nav className="topbar-location" aria-label="Breadcrumb">
              {trail.map((crumb, index) =>
                crumb.to && index < trail.length - 1 ? (
                  <Fragment key={index}>
                    <Link to={crumb.to} className="topbar-group topbar-link">
                      {crumb.label}
                    </Link>
                    <Icon name="chevron-right" size={14} className="topbar-separator" />
                  </Fragment>
                ) : (
                  <span key={index} className="topbar-page" aria-current="page">
                    {crumb.label}
                  </span>
                ),
              )}
            </nav>
          ) : section ? (
            // A top-level screen: just its name. Its group ("Workspace",
            // "Administration") is a sidebar heading, not a place.
            <div className="topbar-location">
              <span className="topbar-page">{section.item.label}</span>
            </div>
          ) : null}
          <div className="topbar-spacer" />
          {/* The top bar's controls. The theme is chosen in the user menu
              and in quick find, not here: a rare setting does not need the
              prime spot. */}
          <button
            type="button"
            className="quick-find"
            onClick={() => setPaletteOpen(true)}
            aria-keyshortcuts={IS_MAC ? "Meta+K" : "Control+K"}
          >
            <Icon name="search" />
            <span className="quick-find-label">Search or jump to…</span>
            <span className="quick-find-keys">
              <Kbd>{IS_MAC ? "⌘" : "Ctrl"}</Kbd>
              <Kbd>K</Kbd>
            </span>
          </button>
          <NotificationBell />
        </header>
        <main id="main" className="main" tabIndex={-1}>
          <TrailContext.Provider value={setTrail}>
            {/* A page that throws is replaced by an explanation; the shell
                around it, and the way to another screen, keep working. */}
            <RouteErrorBoundary>{children}</RouteErrorBoundary>
          </TrailContext.Provider>
        </main>
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onSignOut={signOut}
        onShortcuts={() => setShortcutsOpen(true)}
      />
      <ShortcutsDialog open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
    </div>
  );
}
