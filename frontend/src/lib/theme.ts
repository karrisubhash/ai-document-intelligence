import { useSyncExternalStore } from "react";

/**
 * The colour theme: light, dark, or whatever the operating system says.
 *
 * The choice is a per-browser convenience, so it lives in localStorage and
 * nowhere else. public/theme-init.js applies it before the first paint -
 * the page would otherwise flash the system theme while the bundle loads -
 * and an explicit choice is set as data-theme on <html>, which the
 * stylesheet's tokens key on. "system" removes the attribute and leaves
 * the decision to prefers-color-scheme.
 */
export type ThemePreference = "light" | "dark" | "system";

const KEY = "adi-theme";
const listeners = new Set<() => void>();

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

function apply(preference: ThemePreference) {
  const root = document.documentElement;
  if (preference === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", preference);
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

const darkQuery = () =>
  typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)")
    : null;

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
