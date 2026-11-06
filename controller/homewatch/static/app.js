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

const dirtyForms = [];  // one function per form with a Save button: true while it has unsaved changes

// Save buttons stay greyed out until something in their form changes.
// Internet module: the Ookla license only shows for the Ookla engine, is required for it, and the
// button says it will install the CLI when it isn't there yet.
document.querySelectorAll("button[data-save]").forEach(function (btn) {
  const form = btn.form;
  const engine = form.querySelector("[data-engine-select]");
  const eula = form.querySelector("[data-ookla-eula]");
  // what the form would send, in a fixed order: rows moved around (grouping) are not a change
  const snap = function () {
    return Array.from(new FormData(form).entries()).map(function (kv) { return kv[0] + "=" + kv[1]; }).sort().join("&");
  };
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
  form.addEventListener("submit", function () { form.dataset.submitting = "1"; });
  dirtyForms.push(function () { return !form.dataset.submitting && snap() !== initial; });
  update();
});
// Leaving a page with unsaved changes in a form that has a Save button: the browser asks first.
window.addEventListener("beforeunload", function (e) {
  if (dirtyForms.some(function (dirty) { return dirty(); })) {
    e.preventDefault();
    e.returnValue = "";  // older browsers need it set
  }
});
// The setup wizard uses the same Internet form without a Save button: just show / hide the license.
document.querySelectorAll("[data-engine-select]").forEach(function (sel) {
  if (sel.form.querySelector("button[data-save]")) return;
  function show() { sel.form.querySelectorAll("[data-ookla-only]").forEach(function (el) { el.hidden = sel.value !== "ookla"; }); }
  sel.addEventListener("change", show);
  show();
});

// Status page: one module tile open at a time. It survives the 30 s refresh; loading the page closes it, except
// /?open=<module> (Grafana's links), which opens that one.
(function () {
  let openModule = null;
  const asked = new URLSearchParams(location.search).get("open");
  const first = asked && document.querySelector(`details[data-module="${CSS.escape(asked)}"]`);
  if (first) {
    first.open = true;
    openModule = asked;
    first.scrollIntoView({block: "start"});
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("details[data-module] > summary a")) return;  // a link, not the tile
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

// The update progress page: follows the host's updater and the restart, then goes to Status; gives up after 5 minutes.
(function () {
  const box = document.getElementById("update-progress");
  if (!box) return;
  const to = box.dataset.to, started = Date.now(), LIMIT = 5 * 60 * 1000;
  const steps = Array.from(box.querySelectorAll(".steps li"));
  const bar = box.querySelector(".progress"), pct = box.querySelector("[data-u-pct]");
  const title = box.querySelector("[data-u-title]"), result = box.querySelector("[data-u-result]"), note = box.querySelector("[data-u-note]");
  let step = 0, shown = 5, wentAway = false, over = false;
  function setStep(n) {
    step = Math.max(step, n);
    steps.forEach(function (li, i) {
      li.className = i < step ? "done" : i === step ? "now" : "";
      li.querySelector("i").textContent = i < step ? "✓" : "";
    });
  }
  function setPct(p) {
    shown = Math.max(shown, Math.min(100, Math.round(p)));
    bar.firstElementChild.style.width = shown + "%";
    bar.setAttribute("aria-valuenow", shown);
    pct.textContent = shown + " %";
  }
  function finish(ok, text) {
    over = true;
    note.hidden = true;
    if (ok) {
      setStep(4); setPct(100);
      title.textContent = "Update OK";
      result.innerHTML = '<div class="result ok" role="status"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9.5"/><path d="M7.5 12.5l3 3 6-6.5"/></svg><div><b></b><p>Taking you to the Status page in <span class="num">3</span> s…</p></div></div>';
      result.querySelector("b").textContent = "homewatch " + to + " is running.";
      let n = 3;
      const t = setInterval(function () {
        n--; result.querySelector(".num").textContent = n;
        if (n <= 0) { clearInterval(t); location.replace("/"); }
      }, 1000);
    } else {
      steps.forEach(function (li, i) { if (i === step) { li.className = "bad"; li.querySelector("i").textContent = "!"; } });
      title.textContent = "Update failed";
      result.innerHTML = '<div class="result bad" role="alert"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9.5"/><path d="M12 7.5v5.5M12 16.5v.5"/></svg><div><b></b><p></p><p><a class="button" href="/system">Back to System</a></p></div></div>';
      result.querySelector("b").textContent = text;
      result.querySelector("p").textContent = "On the host, journalctl -u homewatch-update shows what the updater did.";
    }
  }
  function get(url) {
    return fetch(url, { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }
  function tick() {
    if (over) return;
    const elapsed = Date.now() - started;
    Promise.all([get("/healthz"), get("/system/updates/state")]).then(function (res) {
      if (over) return;
      const health = res[0], st = res[1];
      if (health && String(health.version).replace(/-dev$/, "") === to) { finish(true); return; }
      if (st && st.state === "failed") { finish(false, "The update to " + to + " failed: " + (st.summary || "see the updater's log") + "."); return; }
      if (!health) { wentAway = true; setStep(2); setPct(Math.min(80, 62 + elapsed / 10000)); }
      else if (wentAway) { setStep(3); setPct(88); }
      else if (st && !st.asked && st.state === "running") { setStep(1); setPct(Math.min(58, 18 + elapsed / 2500)); }
      else { setPct(Math.min(16, 5 + elapsed / 1000)); }
      if (elapsed > LIMIT) {
        finish(false, to + " didn't answer within 5 minutes. homewatch " + (health ? "is still on " + health.version : "isn't answering") + ".");
        return;
      }
      setTimeout(tick, 2500);
    });
  }
  setTimeout(tick, 1500);
})();

// Home Assistant entities: one list of rows, grouped here by integration › device, area › device or domain,
// with counts, tick-all per group and per device, search and a "show" filter. Rows are only moved and hidden,
// so every tick and rule is still sent when saving. Without this script the list is one long table.
document.querySelectorAll("[data-entity-table]").forEach(function (box) {
  const search = box.querySelector("[data-et-search]");
  const show = box.querySelector("[data-et-show]");
  const viewSel = box.querySelector("[data-et-view]");
  const total = box.querySelector("[data-et-total]");
  const holder = box.querySelector("[data-et-groups]");
  const source = box.querySelector("[data-et-source]");
  const head = source.querySelector("tr");
  const rows = Array.from(source.querySelectorAll("tr[data-e]"));
  const grafana = box.dataset.grafana === "1";
  const BIG = 50;  // ticking more than this at once asks first
  let groups = [];  // [{el, rows, subs: [{el, rows}]}]

  function ticked(r) {  // without the Grafana column (Grafana off), Show in Grafana stays as saved
    const w = r.querySelector("[data-w]"), g = r.querySelector("[data-g]");
    return (w && w.checked) || (g ? g.checked : r.dataset.gSaved === "1");
  }
  function count(rs, k) {
    return rs.filter(function (r) { const b = r.querySelector("[data-" + k + "]"); return b && b.checked; }).length;
  }
  function ruleState(r) {
    const w = r.querySelector("[data-w]"), rule = r.querySelector("[data-r]");
    rule.readOnly = !w.checked;
    rule.placeholder = w.checked ? "e.g. below 200 warning" : "tick Watch first";
  }
  function el(tag, cls, text) {
    const x = document.createElement(tag);
    if (cls) x.className = cls;
    if (text !== undefined) x.textContent = text;
    return x;
  }
  function allBox(k, label, rs, scope) {  // a tick-all inside a summary: it must not open / close the group
    const l = el("label", "check et-all");
    const b = el("input");
    b.type = "checkbox";
    b.dataset.etAll = k;
    b.setAttribute("aria-label", label + " " + scope);
    l.append(b, document.createTextNode(" " + label));
    l.addEventListener("click", function (e) { e.stopPropagation(); });
    b.addEventListener("click", function (e) {
      e.stopPropagation();
      const target = rs.filter(function (r) { return !r.hidden; });
      if (b.checked && target.length > BIG && !confirm(label + ": all " + target.length + " entities of " + scope + "?")) {
        e.preventDefault();
        return;
      }
      target.forEach(function (r) { const x = r.querySelector("[data-" + k + "]"); if (x) { x.checked = b.checked; ruleState(r); } });
      apply();
    });
    return l;
  }
  function table(rs) {
    const wrap = el("div", "scroll");
    const tb = el("table", "entities");
    tb.appendChild(head.cloneNode(true));
    rs.forEach(function (r) { tb.appendChild(r); });
    wrap.appendChild(tb);
    return wrap;
  }
  function order(a, b) {  // the kinds that usually matter first, then by name
    return (b.dataset.first === "1") - (a.dataset.first === "1") || a.dataset.label.localeCompare(b.dataset.label);
  }
  function build(view) {
    holder.textContent = "";
    groups = [];
    const outer = {};
    rows.forEach(function (r) {
      const k = view === "domain" ? r.dataset.domain : view === "area" ? (r.dataset.area || "No area")
        : (r.dataset.integration || "Other");
      (outer[k] = outer[k] || []).push(r);
    });
    Object.keys(outer).sort(function (a, b) {
      const last = function (x) { return x === "No area" || x === "Other"; };
      return last(a) - last(b) || a.localeCompare(b);
    }).forEach(function (name) {
      const rs = outer[name];
      const d = el("details", "choices et-group");
      const s = el("summary");
      s.append(el("b", "", name), document.createTextNode(" "), el("span", "quiet small et-count"));
      s.append(allBox("w", "Watch", rs, name));
      if (grafana) s.append(allBox("g", "Show in Grafana", rs, name));
      d.appendChild(s);
      const g = {el: d, rows: rs, subs: []};
      if (view === "domain") {
        d.appendChild(table(rs.sort(order)));
      } else {
        const inner = {};
        rs.forEach(function (r) { const k = r.dataset.device || "No device"; (inner[k] = inner[k] || []).push(r); });
        Object.keys(inner).sort(function (a, b) {
          return (a === "No device") - (b === "No device") || a.localeCompare(b);
        }).forEach(function (dev) {
          const drs = inner[dev].sort(order);
          const dd = el("details", "et-dev");
          const ds = el("summary");
          ds.append(el("span", "et-dev-name", dev), document.createTextNode(" "), el("span", "quiet small et-count"));
          ds.append(allBox("w", "Watch", drs, dev));
          if (grafana) ds.append(allBox("g", "Show in Grafana", drs, dev));
          dd.append(ds, table(drs));
          d.appendChild(dd);
          g.subs.push({el: dd, rows: drs});
        });
      }
      holder.appendChild(d);
      groups.push(g);
    });
    source.hidden = true;
    apply();
  }
  function label(rs, visible, filtering) {
    const nw = count(rs, "w"), ng = count(rs, "g");
    const nn = rs.filter(function (r) { return r.dataset.new === "1"; }).length;
    return rs.length + " · " + nw + " watched" + (grafana ? " · " + ng + " in Grafana" : "")
      + (nn ? " · " + nn + " new" : "") + (filtering ? " · " + visible + " shown" : "");
  }
  function syncAll(summary, rs) {
    summary.querySelectorAll(":scope > .et-all > [data-et-all]").forEach(function (b) {
      const vis = rs.filter(function (r) { return !r.hidden; });
      const on = count(vis, b.dataset.etAll);
      b.checked = vis.length > 0 && on === vis.length;
      b.indeterminate = on > 0 && on < vis.length;
    });
  }
  function apply() {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    const f = show.value, filtering = words.length > 0 || f !== "";
    rows.forEach(function (r) {
      r.hidden = !(words.every(function (x) { return r.dataset.text.includes(x); })
        && (f === "" || (f === "t" && ticked(r)) || (f === "u" && r.dataset.unavailable === "1")
            || (f === "n" && r.dataset.new === "1")));
    });
    let shown = 0;
    groups.forEach(function (g) {
      const visible = g.rows.filter(function (r) { return !r.hidden; }).length;
      const s = g.el.querySelector(":scope > summary");
      s.querySelector(".et-count").textContent = label(g.rows, visible, filtering);
      syncAll(s, g.rows);
      g.el.hidden = filtering && visible === 0;
      if (filtering) g.el.open = visible > 0 && visible <= 200;
      g.subs.forEach(function (sub) {
        const v = sub.rows.filter(function (r) { return !r.hidden; }).length;
        const ss = sub.el.querySelector(":scope > summary");
        ss.querySelector(".et-count").textContent = label(sub.rows, v, filtering);
        syncAll(ss, sub.rows);
        sub.el.hidden = filtering && v === 0;
        if (filtering) sub.el.open = v > 0 && v <= 50;
      });
      shown += visible;
    });
    total.textContent = count(rows, "w") + " watched" + (grafana ? " · " + count(rows, "g") + " shown in Grafana" : "")
      + (filtering ? " · " + shown + " of " + rows.length + " match" : "");
  }
  box.addEventListener("change", function (e) {
    if (e.target.matches("[data-w]")) ruleState(e.target.closest("tr"));
    if (!e.target.dataset.etAll) apply();
  });
  search.addEventListener("input", apply);
  search.addEventListener("keydown", function (e) { if (e.key === "Enter") e.preventDefault(); });  // not a save
  show.addEventListener("change", apply);
  rows.forEach(function (r) {
    r.querySelector("input.et-rule").addEventListener("keydown", function (e) { if (e.key === "Enter") e.preventDefault(); });
  });
  let view = "domain";
  if (viewSel) {
    try { view = localStorage.getItem("hw.ha.view") || "integration"; } catch (e) { view = "integration"; }
    viewSel.value = view;
    viewSel.addEventListener("change", function () {
      try { localStorage.setItem("hw.ha.view", viewSel.value); } catch (e) { /* private window */ }
      build(viewSel.value);
    });
  }
  build(viewSel ? viewSel.value : "domain");

  // Saving: entities that had a tick and now have none -> keep or delete their history
  const form = box.closest("form");
  const dialog = form.querySelector("[data-et-dialog]");
  const history = form.querySelector("[data-et-history]");
  let answered = false;
  form.addEventListener("submit", function (e) {
    if (answered || !dialog || typeof dialog.showModal !== "function") return;
    const dropped = Array.from(box.querySelectorAll('tr[data-e][data-was="1"]')).filter(function (r) { return !ticked(r); });
    if (!dropped.length) return;
    e.preventDefault();
    const list = dialog.querySelector("[data-et-dropped]");
    list.textContent = "";
    dropped.slice(0, 30).forEach(function (r) {
      const li = document.createElement("li");
      li.textContent = r.dataset.label;
      list.appendChild(li);
    });
    if (dropped.length > 30) {
      const li = document.createElement("li");
      li.textContent = "and " + (dropped.length - 30) + " more";
      list.appendChild(li);
    }
    dialog.showModal();
  });
  if (dialog) dialog.querySelectorAll("[data-et-choice]").forEach(function (b) {
    b.addEventListener("click", function () {
      dialog.close();
      if (!b.dataset.etChoice) return;
      history.value = b.dataset.etChoice;
      answered = true;
      form.requestSubmit ? form.requestSubmit() : form.submit();
    });
  });
});
