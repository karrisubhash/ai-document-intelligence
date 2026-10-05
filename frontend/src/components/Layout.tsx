import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";

import { useAuth, useMe } from "../auth/AuthContext";
import { displayName } from "../lib/format";
import { ROLE_LABEL } from "../lib/labels";
import { useToast } from "./ui";

interface NavItem {
  to: string;
  label: string;
  icon: string;
  end?: boolean;
}

const PRIMARY: NavItem[] = [
  { to: "/", label: "Dashboard", icon: "◧", end: true },
  { to: "/documents", label: "Documents", icon: "▤" },
  { to: "/search", label: "Search", icon: "⌕" },
  { to: "/ask", label: "Ask", icon: "✦" },
];

function adminItems(can: ReturnType<typeof useAuth>["can"]): NavItem[] {
  const items: NavItem[] = [];
  if (can("administer_users")) items.push({ to: "/admin/users", label: "Users", icon: "◉" });
  if (can("administer_departments") || can("administer_users")) {
    items.push({ to: "/admin/departments", label: "Departments", icon: "▦" });
  }
  if (can("view_operations")) {
    items.push({ to: "/admin/processing", label: "Processing", icon: "↻" });
  }
  if (can("view_audit_log")) items.push({ to: "/admin/audit", label: "Audit log", icon: "☰" });
  if (can("view_system_status")) {
    items.push({ to: "/admin/system", label: "System status", icon: "◎" });
  }
  return items;
}

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
          >
            <span className="nav-icon" aria-hidden="true">
              {item.icon}
            </span>
            {item.label}
          </NavLink>
        </li>
      ))}
    </ul>
  );
}

function UserMenu() {
  const me = useMe();
  const { logout } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent | globalThis.KeyboardEvent) => {
      if (event instanceof KeyboardEvent && event.key !== "Escape") return;
      if (event instanceof MouseEvent) {
        const target = event.target as HTMLElement;
        if (target.closest(".user-menu")) return;
      }
      setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const initials = displayName(me)
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <div className="user-menu">
      <button
        type="button"
        className="user-button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="avatar" aria-hidden="true">
          {initials}
        </span>
        <span className="user-text">
          <span className="user-name">{displayName(me)}</span>
          <span className="user-meta">
            {me.role ? ROLE_LABEL[me.role] : "No role"}
            {me.department ? ` · ${me.department}` : ""}
          </span>
        </span>
      </button>
      {open ? (
        <div className="menu" role="menu">
          <button
            role="menuitem"
            type="button"
            className="menu-item"
            onClick={() => {
              setOpen(false);
              navigate("/account");
            }}
          >
            Account and password
          </button>
          <button
            role="menuitem"
            type="button"
            className="menu-item"
            onClick={() => {
              setOpen(false);
              logout().then(
                () => navigate("/", { replace: true }),
                () =>
                  toast(
                    "Sign-out did not complete - you are still signed in. Check your connection and try again.",
                    "danger",
                  ),
              );
            }}
          >
            Sign out
          </button>
        </div>
      ) : null}
    </div>
  );
}

export default function Layout({ children }: { children: ReactNode }) {
  const { can } = useAuth();
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();
  const admin = adminItems(can);

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

  return (
    <div className={`app${navOpen ? " nav-open" : ""}`}>
      <a href="#main" className="skip-link">
        Skip to content
      </a>

      <aside className="sidebar" aria-label="Main navigation">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">Document Intelligence</span>
        </div>
        <nav>
          <NavList items={PRIMARY} onNavigate={() => setNavOpen(false)} />
          {admin.length ? (
            <>
              <p className="nav-section">Administration</p>
              <NavList items={admin} onNavigate={() => setNavOpen(false)} />
            </>
          ) : null}
        </nav>
      </aside>

      <div className="scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />

      <div className="workspace">
        <header className="topbar">
          <button
            type="button"
            className="icon-button nav-toggle"
            aria-label={navOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={navOpen}
            onClick={() => setNavOpen((value) => !value)}
          >
            ☰
          </button>
          <div className="topbar-spacer" />
          <UserMenu />
        </header>
        <main id="main" className="main" tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  );
}
