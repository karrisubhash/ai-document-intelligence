// Applies the colour theme before the first paint, so the page does not
// flash the wrong one while the bundle loads. An external file because the
// Content-Security-Policy allows no inline script. src/lib/theme.ts keeps
// both in step afterwards.
//
// data-theme on <html> always names the theme on screen - the saved choice,
// or the operating system's answer when there is none - so the stylesheet
// declares its dark tokens once, under :root[data-theme="dark"]. The browser
// chrome (the theme-color metas) follows the same answer: an explicit dark
// choice on a light system no longer leaves a light address bar over a dark
// page. The colours are the two themes' --bg.
(function () {
  var theme = null;
  try {
    theme = window.localStorage.getItem("adi-theme");
  } catch (error) {
    // Storage blocked: the operating system's preference applies.
  }
  if (theme !== "light" && theme !== "dark") {
    var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    theme = dark ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", theme);
  var metas = document.querySelectorAll('meta[name="theme-color"]');
  for (var i = 0; i < metas.length; i += 1) {
    metas[i].setAttribute("content", theme === "dark" ? "#0e0e10" : "#f6f7f9");
  }
})();
