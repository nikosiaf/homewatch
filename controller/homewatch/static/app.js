// Confirm before destructive buttons (inline handlers are blocked by the Content-Security-Policy).
document.addEventListener("click", function (e) {
  const b = e.target.closest("[data-confirm]");
  if (b && !confirm(b.dataset.confirm)) e.preventDefault();
});
