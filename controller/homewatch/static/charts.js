// Status page charts: uPlot, data from /api/series. No external requests.
(function () {
  const el = document.getElementById("chart");
  if (!el) return;
  const note = document.getElementById("chart-note");
  let chart = "latency", range = "24h", plot = null;
  const colors = ["#2457c5", "#c0392b", "#1a7f37", "#b26b00"];

  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

  async function load() {
    document.querySelectorAll("[data-chart]").forEach(b => b.classList.toggle("secondary", b.dataset.chart !== chart));
    document.querySelectorAll("[data-range]").forEach(b => b.classList.toggle("secondary", b.dataset.range !== range));
    note.textContent = "Loading…";
    let d;
    try {
      const r = await fetch(`/api/series?chart=${chart}&range=${range}`, { credentials: "same-origin" });
      d = await r.json();
      if (!r.ok) throw new Error(d.error || r.status);
    } catch (e) { note.textContent = "Chart unavailable: " + e.message; return; }
    if (plot) { plot.destroy(); plot = null; }
    if (!d.x.length) { note.textContent = "No data in this period yet."; return; }
    note.textContent = "";
    const ink = cssVar("--quiet"), grid = cssVar("--line");
    const axis = { stroke: ink, grid: { stroke: grid }, ticks: { stroke: grid } };
    const opts = {
      width: el.clientWidth, height: 240,
      series: [{}].concat(d.series.map((s, i) => ({ label: s.name, stroke: colors[i % colors.length], width: 1.5, spanGaps: false,
        value: (u, v) => v == null ? "–" : v + " " + d.unit }))),
      axes: [axis, Object.assign({ label: d.unit, labelSize: 20 }, axis)],
      scales: { y: { range: (u, min, max) => [0, Math.max(max * 1.1, 1)] } },
    };
    plot = new uPlot(opts, [d.x].concat(d.series.map(s => s.data)), el);
  }

  document.querySelectorAll("[data-chart]").forEach(b => b.addEventListener("click", () => { chart = b.dataset.chart; load(); }));
  document.querySelectorAll("[data-range]").forEach(b => b.addEventListener("click", () => { range = b.dataset.range; load(); }));
  window.addEventListener("resize", () => { if (plot) plot.setSize({ width: el.clientWidth, height: 240 }); });
  load();
})();
