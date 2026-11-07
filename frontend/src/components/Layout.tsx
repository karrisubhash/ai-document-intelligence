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

import { useAuth, useMe } from "../auth/AuthContext";
import { displayName } from "../lib/format";
import { ROLE_LABEL } from "../lib/labels";
import { setTheme, useResolvedTheme, useTheme } from "../lib/theme";
import CommandPalette from "./CommandPalette";
import GuestBanner from "./GuestBanner";
import { BrandMark, Icon } from "./icons";
import { PRIMARY, adminItems, sectionFor, type NavItem } from "./navigation";
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

const isMac =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

function NavList({ items, onNavigate }: { items: NavItem[]; onNavigate: () => void }) {
  return (
    <ul className="nav-list">
      {items.map((item) => (
        <li key={item.to}>
          <NavLink
            to={item.to}
            end={item.end}
            className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
            onClick={onNavigate}
            title={item.label}
          >
            <Icon name={item.icon} size={18} className="nav-icon" />
            <span className="nav-label">{item.label}</span>
          </NavLink>
        </li>
      ))}
    </ul>
  );
}

function UserMenu({ onSignOut }: { onSignOut: () => void }) {
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
  const [trail, setTrail] = useState<Crumb[] | null>(null);
  const navToggle = useRef<HTMLButtonElement>(null);
  const resolvedTheme = useResolvedTheme();
  const location = useLocation();
  const admin = adminItems(can);
  const section = sectionFor(location.pathname, admin);

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
              <NavList items={admin} onNavigate={() => setNavOpen(false)} />
            </>
          ) : null}
        </nav>
        <div className="sidebar-footer">
          <UserMenu onSignOut={signOut} />
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
            <div className="topbar-location">
              <span className="topbar-group">{section.group}</span>
              <Icon name="chevron-right" size={14} className="topbar-separator" />
              <span className="topbar-page">{section.item.label}</span>
            </div>
          ) : null}
          <div className="topbar-spacer" />
          <button
            type="button"
            className="quick-find"
            onClick={() => setPaletteOpen(true)}
            aria-keyshortcuts={isMac ? "Meta+K" : "Control+K"}
          >
            <Icon name="search" />
            <span className="quick-find-label">Search or jump to…</span>
            <span className="quick-find-keys">
              <Kbd>{isMac ? "⌘" : "Ctrl"}</Kbd>
              <Kbd>K</Kbd>
            </span>
          </button>
          <button
            type="button"
            className="icon-button"
            aria-label={resolvedTheme === "dark" ? "Switch to the light theme" : "Switch to the dark theme"}
            title={resolvedTheme === "dark" ? "Switch to the light theme" : "Switch to the dark theme"}
            onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
          >
            <Icon name={resolvedTheme === "dark" ? "sun" : "moon"} size={18} />
          </button>
        </header>
        <GuestBanner />
        <main id="main" className="main" tabIndex={-1}>
          <TrailContext.Provider value={setTrail}>{children}</TrailContext.Provider>
        </main>
      </div>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} onSignOut={signOut} />
    </div>
  );
}
