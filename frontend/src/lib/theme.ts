import { useSyncExternalStore } from "react";

/**
 * The colour theme: light, dark, or whatever the operating system says.
 *
 * The choice is a per-browser convenience, so it lives in localStorage and
 * nowhere else. public/theme-init.js applies it before the first paint -
 * the page would otherwise flash the wrong theme while the bundle loads -
 * and this module keeps it applied afterwards.
 *
 * data-theme on <html> always names the theme ON SCREEN, "light" or
 * "dark": the explicit choice, or for "system" the operating system's
 * current answer, followed live as it changes. So the stylesheet declares
 * its dark tokens once (:root[data-theme="dark"]) instead of a second time
 * inside a prefers-color-scheme query, and the browser's theme-color - its
 * address bar on a phone - matches the page whichever way it was chosen.
 */
export type ThemePreference = "light" | "dark" | "system";

const KEY = "adi-theme";
const listeners = new Set<() => void>();

/** Each theme's page background (--bg in styles.css), for the browser chrome. */
export const THEME_COLOR = { light: "#f6f7f9", dark: "#0e0e10" } as const;

function read(): ThemePreference {
  try {
    const value = window.localStorage.getItem(KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    // Storage blocked (private window, site data off): the system decides.
    return "system";
  }
}

let current: ThemePreference = typeof window === "undefined" ? "system" : read();

const darkQuery = () =>
  typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)")
    : null;

/** The theme a preference puts on screen right now. */
export function resolveTheme(preference: ThemePreference): "light" | "dark" {
  if (preference !== "system") return preference;
  return darkQuery()?.matches ? "dark" : "light";
}

let watching = false;

/** Under "system", a change of the operating system's setting is followed live. */
function watchSystem() {
  const query = darkQuery();
  if (watching || !query?.addEventListener) return;
  watching = true;
  query.addEventListener("change", () => {
    if (current === "system") apply(current);
  });
}

function apply(preference: ThemePreference) {
  const theme = resolveTheme(preference);
  document.documentElement.setAttribute("data-theme", theme);
  for (const meta of document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')) {
    meta.setAttribute("content", THEME_COLOR[theme]);
  }
  watchSystem();
}

if (typeof window !== "undefined") {
  // theme-init.js normally did this before the first paint; this covers a
  // page where it did not run.
  apply(current);
  // A choice made in another tab of the app applies here too.
  window.addEventListener("storage", (event) => {
    if (event.key !== KEY && event.key !== null) return;
    current = read();
    apply(current);
    listeners.forEach((listener) => listener());
  });
}

export function setTheme(preference: ThemePreference) {
  current = preference;
  try {
    if (preference === "system") window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, preference);
  } catch {
    // Not remembered, but still applied for this visit.
  }
  apply(preference);
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function subscribeSystem(listener: () => void) {
  const query = darkQuery();
  query?.addEventListener?.("change", listener);
  return () => query?.removeEventListener?.("change", listener);
}

/** The saved preference, and a setter shared by every control that changes it. */
export function useTheme(): [ThemePreference, (preference: ThemePreference) => void] {
  const preference = useSyncExternalStore(subscribe, () => current, () => "system" as const);
  return [preference, setTheme];
}

/** The theme actually on screen: the preference, or the system's answer to "system". */
export function useResolvedTheme(): "light" | "dark" {
  const [preference] = useTheme();
  const systemDark = useSyncExternalStore(
    subscribeSystem,
    () => Boolean(darkQuery()?.matches),
    () => false,
  );
  if (preference === "system") return systemDark ? "dark" : "light";
  return preference;
}
