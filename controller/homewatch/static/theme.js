// Theme: "auto" follows the device, "light"/"dark" are kept per browser. Loaded in <head> so the
// page never flashes the wrong colours. The header button cycles auto → light → dark.
(function () {
  const KEY = "hw-theme", ORDER = ["auto", "light", "dark"];
  const LABEL = { auto: "Theme: follows the device", light: "Theme: light", dark: "Theme: dark" };
  const root = document.documentElement;
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function saved() { try { return localStorage.getItem(KEY) || "auto"; } catch (e) { return "auto"; } }
  function apply(mode) {
    if (mode === "light" || mode === "dark") root.dataset.theme = mode; else delete root.dataset.theme;
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
      b.dataset.mode = mode; b.title = LABEL[mode]; b.setAttribute("aria-label", LABEL[mode] + " (change)");
    });
    window.dispatchEvent(new Event("hw-themechange"));
  }

  apply(saved());
  document.addEventListener("DOMContentLoaded", function () { apply(saved()); });
  document.addEventListener("click", function (e) {
    if (!e.target.closest("[data-theme-toggle]")) return;
    const next = ORDER[(ORDER.indexOf(saved()) + 1) % ORDER.length];
    try { localStorage.setItem(KEY, next); } catch (err) { /* private mode: applies to this page only */ }
    apply(next);
  });
  media.addEventListener("change", function () { if (saved() === "auto") apply("auto"); });
})();
