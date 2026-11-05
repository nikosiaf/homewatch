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
    if (all) {
      all.checked = n === boxes.length;
      all.indeterminate = n > 0 && n < boxes.length;
    }
    count.textContent = (n === boxes.length ? "all " + n : n + " of " + boxes.length) + " " + count.dataset.word;
  }
  boxes.forEach(function (b) { b.addEventListener("change", sync); });
  sync();
  if (!all) return;  // a group of one has no "All" box
  all.addEventListener("change", function () {
    // while the list is filtered, only what is shown: nothing out of sight changes
    boxes.filter(function (b) { return !b.closest("label").classList.contains("filtered-out"); })
      .forEach(function (b) { b.checked = all.checked; });
    sync();
  });
});

// Fields that show only while a box is ticked (data-when="key [key]"): hidden, they keep their place in the grid.
(function () {
  const cells = Array.from(document.querySelectorAll("[data-when]"));
  if (!cells.length) return;
  function update() {
    cells.forEach(function (c) {
      const form = c.closest("form");
      const on = c.dataset.when.split(" ").every(function (k) {
        const box = form.querySelector('input[type="checkbox"][name="' + k + '"]');
        return box && box.checked;
      });
      c.classList.toggle("when-off", !on);
    });
  }
  document.addEventListener("change", function (e) { if (e.target.type === "checkbox") update(); });
  update();
})();

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

// Long tick lists: search and "only ticked". Rows are only hidden, so every tick is still sent when saving.
document.querySelectorAll("[data-choice-search]").forEach(function (search) {
  const list = search.closest("[data-choice-list]");
  const only = list.querySelector("[data-choice-selected]");
  const count = list.querySelector("[data-choice-count]");
  const rows = Array.from(list.querySelectorAll("label.check:not(.all)")).filter(function (l) { return !l.closest(".choice-tools"); });
  const groups = Array.from(list.querySelectorAll("[data-choice-group]"));
  function apply() {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    const filtering = words.length > 0 || only.checked;
    let shown = 0, ticked = 0;
    rows.forEach(function (l) {
      const box = l.querySelector("input");
      const text = (l.textContent + " " + box.name).toLowerCase();
      const show = words.every(function (w) { return text.includes(w); }) && (!only.checked || box.checked);
      l.classList.toggle("filtered-out", !show);
      if (show) shown++;
      if (box.checked) ticked++;
    });
    groups.forEach(function (d) {
      const inGroup = Array.from(d.querySelectorAll("label.check:not(.all)"));
      const visible = inGroup.filter(function (l) { return !l.classList.contains("filtered-out"); }).length;
      d.hidden = filtering && visible === 0;
      if (filtering) d.open = visible > 0;
      else if (d.dataset.wasFiltering) d.open = d.hasAttribute("data-open-default");
      d.dataset.wasFiltering = filtering ? "1" : "";
      const label = d.querySelector("[data-group-all-label]");
      if (label) label.textContent = filtering ? "All " + visible + " shown" : "All " + inGroup.length;
    });
    count.textContent = shown + " of " + rows.length + " shown, " + ticked + " ticked";
  }
  search.addEventListener("input", apply);
  search.addEventListener("keydown", function (e) { if (e.key === "Enter") e.preventDefault(); });  // not a save
  only.addEventListener("change", apply);
  list.addEventListener("change", function (e) { if (e.target !== only) apply(); });
  apply();
});


// Long texts opened with "more" stay open when the Status page refreshes.
(function () {
  let open = [];
  document.body.addEventListener("htmx:beforeSwap", function () {
    open = Array.from(document.querySelectorAll("details.more[open][data-more]")).map(function (d) { return d.dataset.more; });
  });
  document.body.addEventListener("htmx:afterSwap", function () {
    open.forEach(function (k) {
      const d = document.querySelector('details.more[data-more="' + CSS.escape(k) + '"]');
      if (d) d.open = true;
    });
  });
})();

// Long tables (data-page="20"): the first rows, then "Show 20 more" until all are shown. The header row stays.
document.querySelectorAll("table[data-page]").forEach(function (table) {
  const step = parseInt(table.dataset.page, 10) || 20;
  const rows = Array.from(table.querySelectorAll("tr")).filter(function (r) { return !r.querySelector("th"); });
  if (rows.length <= step) return;
  let shown = 0;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "secondary more-rows";
  function more() {
    shown = Math.min(rows.length, shown + step);
    rows.forEach(function (r, i) { r.classList.toggle("page-hidden", i >= shown); });
    const left = rows.length - shown;
    btn.textContent = "Show " + Math.min(step, left) + " more (" + left + " left)";
    btn.hidden = left === 0;
  }
  btn.addEventListener("click", more);
  table.after(btn);
  more();
});

// Forms that need the user's password (data-ask-password="question"): it is asked in a window when the form is sent.
(function () {
  const dlg = document.getElementById("ask-password");
  if (!dlg) return;
  const title = dlg.querySelector("[data-ask-title]");
  const input = dlg.querySelector("input[type=password]");
  const ok = dlg.querySelector("[data-ask-ok]");
  let pending = null;
  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (!form.matches("form[data-ask-password]") || form.dataset.asked) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    pending = form;
    title.textContent = form.dataset.askPassword;
    const action = form.dataset.askAction || "Confirm";
    ok.textContent = action;
    ok.classList.toggle("danger", form.hasAttribute("data-ask-danger"));
    input.value = "";
    dlg.returnValue = "";  // Escape keeps the last value otherwise
    dlg.showModal();
    input.focus();
  }, true);
  dlg.addEventListener("close", function () {
    const form = pending;
    pending = null;
    if (!form || dlg.returnValue !== "ok" || !input.value) return;
    form.querySelector('input[name="current"]').value = input.value;
    input.value = "";
    form.dataset.asked = "1";
    form.requestSubmit();
  });
  dlg.querySelector("[data-ask-cancel]").addEventListener("click", function () { dlg.close("cancel"); });
})();

// System page, after Install: a window that waits until the new release answers, then reloads the page.
(function () {
  const dlg = document.getElementById("updating");
  if (!dlg) return;
  const want = dlg.dataset.version;
  const text = dlg.querySelector("[data-updating-text]");
  const reload = dlg.querySelector("[data-updating-reload]");
  const started = Date.now();
  dlg.showModal();
  reload.addEventListener("click", function () { location.reload(); });
  function poll() {
    fetch("/healthz", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; })
      .then(function (h) {
        if (h && String(h.version).replace(/-dev$/, "") === want) {
          text.textContent = "homewatch " + want + " is running. Reloading…";
          setTimeout(function () { location.replace("/system"); }, 1500);
          return;
        }
        if (Date.now() - started > 5 * 60 * 1000) {
          text.textContent = "No answer from " + want + " after 5 minutes. Reload the page to see what the updater reports.";
          reload.hidden = false;
          return;
        }
        setTimeout(poll, 3000);
      });
  }
  setTimeout(poll, 3000);
})();
