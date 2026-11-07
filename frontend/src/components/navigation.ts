import type { AttentionBadgeKey } from "../api/notifications";
import type { Capabilities } from "../api/types";
import type { IconName } from "./icons";

export interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  end?: boolean;
  /** Extra words the command palette matches, e.g. "upload" for Documents. */
  keywords?: string;
  /** Which attention count (GET /admin/attention/) the sidebar shows on it. */
  badge?: AttentionBadgeKey;
}

export const PRIMARY: NavItem[] = [
  { to: "/", label: "Dashboard", icon: "dashboard", end: true, keywords: "home overview" },
  { to: "/documents", label: "Documents", icon: "files", keywords: "library files upload" },
  { to: "/search", label: "Search", icon: "search", keywords: "find passages" },
  { to: "/ask", label: "Ask", icon: "sparkles", keywords: "chat question answer assistant" },
  { to: "/collections", label: "Collections", icon: "folder", keywords: "sets groups" },
];

/**
 * The administration screens this person may use. A convenience only: every
 * endpoint behind them enforces the same rule on the server.
 */
export function adminItems(can: (capability: keyof Capabilities) => boolean): NavItem[] {
  const items: NavItem[] = [];
  if (can("administer_users")) {
    items.push({
      to: "/admin/users",
      label: "Users",
      icon: "users",
      keywords: "accounts people sign-up signup requests approve decline",
      badge: "signups",
    });
  }
  if (can("administer_departments") || can("administer_users")) {
    items.push({ to: "/admin/departments", label: "Departments", icon: "building" });
  }
  if (can("view_operations")) {
    items.push({
      to: "/admin/processing",
      label: "Processing",
      icon: "activity",
      keywords: "jobs pipeline failed retry",
      badge: "processing",
    });
  }
  if (can("view_audit_log")) {
    items.push({ to: "/admin/audit", label: "Audit log", icon: "scroll", keywords: "history activity" });
  }
  if (can("view_system_status")) {
    items.push({
      to: "/admin/system",
      label: "System status",
      icon: "server",
      keywords: "health configuration",
    });
  }
  return items;
}

/** The navigation entry a path belongs to, for the top bar's location line. */
export function sectionFor(
  pathname: string,
  admin: NavItem[],
): { group: string; item: NavItem } | null {
  const match = (items: NavItem[]) =>
    items
      .filter((item) => (item.end ? pathname === item.to : pathname === item.to || pathname.startsWith(`${item.to}/`)))
      .sort((a, b) => b.to.length - a.to.length)[0];
  const primary = match(PRIMARY);
  if (primary) return { group: "Workspace", item: primary };
  const administration = match(admin);
  if (administration) return { group: "Administration", item: administration };
  if (pathname === "/account") {
    return { group: "Settings", item: { to: "/account", label: "Account", icon: "user" } };
  }
  return null;
}
