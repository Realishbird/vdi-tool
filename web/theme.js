// Light and dark mode. "auto" follows the device; the choice is kept in this browser.
// Loaded in the page head so the theme is set before anything is drawn.
(function () {
  const root = document.documentElement;
  const read = () => { try { return localStorage.getItem("vdi-theme") || "auto"; } catch (_) { return "auto"; } };
  const apply = (mode) => { if (mode === "light" || mode === "dark") root.dataset.theme = mode; else delete root.dataset.theme; };
  const label = (mode) => ({ auto: "Theme: auto", light: "Theme: light", dark: "Theme: dark" })[mode];
  apply(read());
  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("theme-btn");
    if (!btn) return;
    btn.textContent = label(read());
    btn.addEventListener("click", () => {
      const next = { auto: "light", light: "dark", dark: "auto" }[read()];
      try { localStorage.setItem("vdi-theme", next); } catch (_) { /* private window: the choice lasts until reload */ }
      apply(next);
      btn.textContent = label(next);
    });
  });
})();
