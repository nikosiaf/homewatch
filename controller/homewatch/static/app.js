// Confirm before destructive buttons (inline handlers are blocked by the Content-Security-Policy).
document.addEventListener("click", function (e) {
  const b = e.target.closest("[data-confirm]");
  if (b && !confirm(b.dataset.confirm)) e.preventDefault();
});

// "Add a provider": show only the chosen provider's description and fields.
document.querySelectorAll("[data-provider-select]").forEach(function (sel) {
  const form = sel.closest("form");
  function show() {
    form.querySelectorAll("[data-provider]").forEach(function (d) { d.hidden = d.dataset.provider !== sel.value; });
  }
  sel.addEventListener("change", show);
  show();
});

// Messages: success fades out after 6 s, errors stay until closed; neither comes back on reload.
(function () {
  document.addEventListener("click", function (e) {
    const x = e.target.closest("[data-dismiss]");
    if (x) x.parentElement.remove();
  });
  function arm(root) {
    root.querySelectorAll("[data-autohide]").forEach(function (el) {
      setTimeout(function () { el.style.transition = "opacity .6s"; el.style.opacity = "0";
        setTimeout(function () { el.remove(); }, 700); }, 6000);
    });
  }
  arm(document);
  document.body.addEventListener("htmx:afterSwap", function (e) { arm(e.target); });
  const url = new URL(location.href);
  if (["msg", "err", "box"].some(function (k) { return url.searchParams.has(k); })) {
    ["msg", "err", "box"].forEach(function (k) { url.searchParams.delete(k); });
    history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
})();

// Pick a modem model: show only that model's description, warning and fields.
document.querySelectorAll("[data-model-select]").forEach(function (sel) {
  const form = sel.closest("form");
  function show() { form.querySelectorAll("[data-model]").forEach(function (d) { d.hidden = d.dataset.model !== sel.value; }); }
  sel.addEventListener("change", show);
  show();
});

// "Enabled" switch on a module card: grey out its fields while off; send them anyway on save.
document.querySelectorAll("[data-enable-toggle]").forEach(function (box) {
  const form = box.closest("form");
  const target = form.querySelector("[data-enable-target]");
  box.addEventListener("change", function () {
    target.disabled = !box.checked;
    form.classList.toggle("off", !box.checked);
  });
  form.addEventListener("submit", function () { target.disabled = false; });
});

// Save buttons stay greyed out until something in their form changes.
// Internet module: the Ookla license only shows for the Ookla engine, is required for it, and the
// button says it will install the CLI when it isn't there yet.
document.querySelectorAll("button[data-save]").forEach(function (btn) {
  const form = btn.form;
  const engine = form.querySelector("[data-engine-select]");
  const eula = form.querySelector("[data-ookla-eula]");
  const snap = function () { return new URLSearchParams(new FormData(form)).toString(); };
  const initial = snap();
  function update() {
    let ok = snap() !== initial;
    if (engine) {
      const ookla = engine.value === "ookla";
      form.querySelectorAll("[data-ookla-only]").forEach(function (el) { el.hidden = !ookla; });
      if (ookla && eula && !eula.checked) ok = false;
      btn.textContent = ookla && !btn.dataset.ooklaInstalled ? "Save and install Ookla CLI" : "Save";
    }
    btn.disabled = !ok;
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
});
// The setup wizard uses the same Internet form without a Save button: just show / hide the license.
document.querySelectorAll("[data-engine-select]").forEach(function (sel) {
  if (sel.form.querySelector("button[data-save]")) return;
  function show() { sel.form.querySelectorAll("[data-ookla-only]").forEach(function (el) { el.hidden = sel.value !== "ookla"; }); }
  sel.addEventListener("change", show);
  show();
});

// Status page: remember which module lines are expanded, across the 30 s refresh.
(function () {
  const KEY = "hw-open-modules";
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return []; } }
  function restore(root) {
    const open = load();
    root.querySelectorAll("details[data-module]").forEach(function (d) { d.open = open.includes(d.dataset.module); });
  }
  document.addEventListener("toggle", function (e) {
    const d = e.target;
    if (!d.matches || !d.matches("details[data-module]")) return;
    const open = load().filter(function (m) { return m !== d.dataset.module; });
    if (d.open) open.push(d.dataset.module);
    try { localStorage.setItem(KEY, JSON.stringify(open)); } catch (e) { /* private mode: not remembered */ }
  }, true);
  restore(document);
  document.body.addEventListener("htmx:afterSwap", function (e) { restore(e.target); });
})();
