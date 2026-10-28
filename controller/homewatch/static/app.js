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
