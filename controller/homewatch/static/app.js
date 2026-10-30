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
    const remove = form.querySelector("[data-remove-select]");
    if (remove) {  // ISP modem: model "None" removes the modem
      const removing = remove.value === "";
      btn.textContent = removing ? "Remove modem" : "Save";
      btn.classList.toggle("danger", removing);
      if (removing) btn.dataset.confirm = "Remove this modem with its settings, password and last reading?";
      else delete btn.dataset.confirm;
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

// Status page: one module tile open at a time. It survives the 30 s refresh; loading the page closes it.
(function () {
  let openModule = null;
  document.addEventListener("click", function (e) {
    const summary = e.target.closest ? e.target.closest("details[data-module] > summary") : null;
    if (!summary) return;
    const d = summary.parentElement;
    document.querySelectorAll("details[data-module][open]").forEach(function (other) {
      if (other !== d) other.open = false;
    });
    openModule = d.open ? null : d.dataset.module;  // the click is about to toggle it
  });
  document.body.addEventListener("htmx:afterSwap", function (e) {
    e.target.querySelectorAll("details[data-module]").forEach(function (d) { d.open = d.dataset.module === openModule; });
  });
})();

// Grouped choice lists: the "All" box ticks / unticks its group and follows the group's boxes.
document.querySelectorAll("[data-choice-group]").forEach(function (d) {
  const all = d.querySelector("[data-group-all]");
  const count = d.querySelector("[data-group-count]");
  const boxes = Array.from(d.querySelectorAll('input[type="checkbox"]:not([data-group-all])'));
  function sync() {
    const n = boxes.filter(function (b) { return b.checked; }).length;
    all.checked = n === boxes.length;
    all.indeterminate = n > 0 && n < boxes.length;
    count.textContent = n;
  }
  all.addEventListener("change", function () {
    boxes.forEach(function (b) { b.checked = all.checked; });
    sync();
  });
  boxes.forEach(function (b) { b.addEventListener("change", sync); });
  sync();
});

// Narrow screens: the header menu opens and closes with the menu button.
document.addEventListener("click", function (e) {
  const b = e.target.closest("[data-menu-toggle]");
  if (!b) return;
  const open = b.closest("header").classList.toggle("open");
  b.setAttribute("aria-expanded", open ? "true" : "false");
});

// Maintenance: ticking a check (or a whole group) switches the window to "Only these".
document.addEventListener("change", function (e) {
  const box = e.target.closest("[data-scope-some], [data-group-all]");
  if (!box || !box.checked) return;
  const some = box.form && box.form.querySelector('input[name="scope"][value="some"]');
  if (some) some.checked = true;
});

// Buttons that wait for the server (data-busy): say so until the page answers. The status page's
// 30 s refresh is held meanwhile, or it would put the idle button back.
(function () {
  let busy = false;
  document.addEventListener("submit", function (e) {
    const btn = e.target.querySelector("button[data-busy]");
    if (!btn || e.defaultPrevented) return;
    busy = true;
    btn.textContent = btn.dataset.busy;
    btn.disabled = true;
  });
  document.addEventListener("htmx:beforeRequest", function (e) { if (busy) e.preventDefault(); });
  // back button: the browser may restore the page as it was left
  window.addEventListener("pageshow", function (e) { if (e.persisted) location.reload(); });
})();

// Filter forms apply as soon as a choice changes.
document.querySelectorAll("form[data-autosubmit]").forEach(function (form) {
  form.addEventListener("change", function () { form.submit(); });
});

// The Status page's 30 s refresh: not for a tab nobody sees, and not while someone is choosing in a field it would replace.
(function () {
  const body = document.getElementById("status-body");
  if (!body) return;
  const MAX_HELD = 4;  // about 2 minutes: an abandoned field must not leave the page stale for ever
  let held = 0, focusModule = null;
  document.addEventListener("htmx:beforeRequest", function (e) {
    if (e.detail.elt !== body) return;
    const a = document.activeElement;
    if (document.hidden || (body.contains(a) && a.matches("select, input, textarea") && held < MAX_HELD)) {
      if (!document.hidden) held++;
      e.preventDefault();
      return;
    }
    held = 0;
    const open = a && a.closest && body.contains(a) ? a.closest("details[data-module]") : null;
    focusModule = open && a.tagName === "SUMMARY" ? open.dataset.module : null;
  });
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.target !== body || !focusModule) return;
    const s = body.querySelector(`details[data-module="${focusModule}"] > summary`);
    if (s) s.focus({ preventScroll: true });
  });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) htmx.ajax("GET", "/partials/status", { source: body, target: body, swap: "innerHTML" });
  });
})();

