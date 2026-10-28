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
