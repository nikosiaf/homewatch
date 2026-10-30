// Status page charts: uPlot, data from /api/series. No external requests.
(function () {
  const el = document.getElementById("chart");
  if (!el) return;
  const note = document.getElementById("chart-note");
  let chart = "latency", range = "24h", plot = null, pending = null;
  const colors = ["#2457c5", "#c0392b", "#1a7f37", "#b26b00"];

  // series written every few minutes or hours: join the dots instead of showing gaps
  function sparse() { return ["speed", "sync", "snr"].includes(chart); }
  function clear() { if (plot) { plot.destroy(); plot = null; } }
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

  async function load() {
    document.querySelectorAll("[data-chart]").forEach(b => b.classList.toggle("secondary", b.dataset.chart !== chart));
    document.querySelectorAll("[data-range]").forEach(b => b.classList.toggle("secondary", b.dataset.range !== range));
    if (pending) pending.abort();  // only the newest choice may draw
    const mine = pending = new AbortController();
    clear();  // the old chart isn't what was asked for any more
    note.textContent = "Loading…";
    let d;
    try {
      const r = await fetch(`/api/series?chart=${chart}&range=${range}`, { credentials: "same-origin", signal: mine.signal });
      d = await r.json();
      if (!r.ok) throw new Error(d.error || r.status);
    } catch (e) {
      if (mine === pending) note.textContent = "Chart unavailable: " + e.message;
      return;
    }
    if (mine !== pending) return;
    const last = d.x.filter((t, i) => d.series.some(s => s.data[i] != null)).pop();
    if (last === undefined) { note.textContent = "No data in this period yet."; return; }
    const age = Date.now() / 1000 - last;
    note.textContent = !sparse() && age > 900 ? `Stale: the newest data is ${Math.round(age / 60)} min old.` : "";
    const ink = cssVar("--quiet"), grid = cssVar("--line");
    const axis = { stroke: ink, grid: { stroke: grid }, ticks: { stroke: grid } };
    const opts = {
      width: el.clientWidth, height: 240,
      series: [{}].concat(d.series.map((s, i) => ({ label: s.name, stroke: colors[i % colors.length], width: 1.5,
        spanGaps: sparse(), points: { show: chart === "speed", size: 6 },
        value: (u, v) => v == null ? "–" : v + " " + d.unit }))),
      axes: [axis, Object.assign({ label: d.unit, labelSize: 20 }, axis)],
      scales: { y: { range: (u, min, max) => [0, Math.max(max * 1.1, 1)] } },
    };
    plot = new uPlot(opts, [d.x].concat(d.series.map(s => s.data)), el);
  }

  document.querySelectorAll("[data-chart]").forEach(b => b.addEventListener("click", () => { chart = b.dataset.chart; load(); }));
  document.querySelectorAll("[data-range]").forEach(b => b.addEventListener("click", () => { range = b.dataset.range; load(); }));
  window.addEventListener("resize", () => { if (plot) plot.setSize({ width: el.clientWidth, height: 240 }); });
  window.addEventListener("hw-themechange", () => { if (plot) load(); });  // axis colours come from the theme
  load();
})();
