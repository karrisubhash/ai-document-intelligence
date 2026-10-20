// Applies a saved colour theme before the first paint, so the page does not
// flash the system theme while the bundle loads. An external file because
// the Content-Security-Policy allows no inline script. See src/lib/theme.ts.
try {
  var theme = window.localStorage.getItem("adi-theme");
  if (theme === "light" || theme === "dark") {
    document.documentElement.setAttribute("data-theme", theme);
  }
} catch (error) {
  // Storage blocked: the operating system's preference applies.
}
