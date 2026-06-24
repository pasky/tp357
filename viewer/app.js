"use strict";

// Display order + distinct colors per location.
const LOCATIONS = [
  ["outside",    "#e6194B"],
  ["livingroom", "#f58231"],
  ["pasky",      "#3cb44b"],
  ["kitchen",    "#4363d8"],
  ["bedroom",    "#911eb4"],
  ["storage",    "#1ca7c0"],
  ["chido",      "#f032e6"],
  ["bathroomc",  "#9A6324"],
  ["bathroomp",  "#808000"],
];

const METRICS = {
  temp:  { label: "Temperature", unit: "\u00B0C", fmt: v => v == null ? "--" : v.toFixed(1) + "\u00B0C" },
  humid: { label: "Humidity",    unit: "%",       fmt: v => v == null ? "--" : v.toFixed(0) + "%" },
};

const WINDOWS = [
  ["6h",  6 * 3600],
  ["24h", 24 * 3600],
  ["3d",  3 * 86400],
  ["7d",  7 * 86400],
  ["30d", 30 * 86400],
  ["1y",  365 * 86400],
];

// window <= this many seconds -> use the fine (5-min) dataset
const FINE_MAX = 8 * 86400;

let datasets = {};       // {fine:{...}, coarse:{...}} each {start, step, rows, legend, cols}
let state = { metric: "temp", window: 24 * 3600 };
let shown = {};          // location -> bool (persists across rebuilds)
let plot = null;

LOCATIONS.forEach(([name]) => { shown[name] = true; });

function parseDataset(d) {
  const m = d.meta;
  const rows = d.data.length;
  const xs = new Array(rows);
  for (let i = 0; i < rows; i++) xs[i] = m.start + i * m.step;
  // legend index lookup: "loc.metric" -> column
  const idx = {};
  m.legend.forEach((name, i) => { idx[name] = i; });
  // pre-extract a column array per legend key
  const cols = {};
  for (const name of m.legend) {
    const c = idx[name];
    const arr = new Array(rows);
    for (let i = 0; i < rows; i++) arr[i] = d.data[i][c];
    cols[name] = arr;
  }
  return { start: m.start, step: m.step, rows, xs, cols };
}

function chooseRes() {
  return state.window <= FINE_MAX ? "fine" : "coarse";
}

function buildSeriesData(ds) {
  // uPlot data: [xs, s1, s2, ...] in LOCATIONS order, for current metric
  const data = [ds.xs];
  for (const [name] of LOCATIONS) {
    const key = name + "." + state.metric;
    data.push(ds.cols[key] || new Array(ds.rows).fill(null));
  }
  return data;
}

function seriesOpts() {
  const series = [{}]; // x
  LOCATIONS.forEach(([name, color], i) => {
    series.push({
      label: name,
      stroke: color,
      width: 1.5,
      spanGaps: false,
      show: shown[name],
      points: { show: false },
      value: (u, v) => METRICS[state.metric].fmt(v),
    });
  });
  return series;
}

function chartSize() {
  const w = document.getElementById("chart").clientWidth || window.innerWidth - 16;
  const h = Math.max(260, Math.min(520, Math.round(window.innerHeight * 0.55)));
  return { width: w, height: h };
}

function makePlot() {
  if (plot) { plot.destroy(); plot = null; }
  const ds = datasets[chooseRes()];
  const data = buildSeriesData(ds);
  const sz = chartSize();
  const now = Math.floor(Date.now() / 1000);
  const opts = {
    ...sz,
    scales: { x: { time: true, min: now - state.window, max: now } },
    axes: [
      {},
      { label: METRICS[state.metric].label + " (" + METRICS[state.metric].unit + ")" },
    ],
    series: seriesOpts(),
    legend: { live: true },
    cursor: { drag: { x: true, y: false } },
  };
  plot = new uPlot(opts, data, document.getElementById("chart"));

  // capture legend toggles into `shown`
  plot.root.querySelectorAll(".u-series").forEach((el, i) => {
    if (i === 0) return; // skip x
    el.addEventListener("click", () => {
      const name = LOCATIONS[i - 1][0];
      shown[name] = !!(plot.series[i].show);
    });
  });
}

function setWindowRange() {
  if (!plot) return;
  const now = Math.floor(Date.now() / 1000);
  plot.setScale("x", { min: now - state.window, max: now });
}

function buttonGroup(containerId, items, isActive, onPick) {
  const c = document.getElementById(containerId);
  c.innerHTML = "";
  items.forEach(([label, val]) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (isActive(val)) b.classList.add("active");
    b.addEventListener("click", () => {
      onPick(val);
      buttonGroup(containerId, items, isActive, onPick);
    });
    c.appendChild(b);
  });
}

function renderControls() {
  buttonGroup("metric",
    Object.entries(METRICS).map(([k, m]) => [m.label, k]),
    v => state.metric === v,
    v => { state.metric = v; makePlot(); });
  buttonGroup("window", WINDOWS,
    v => state.window === v,
    v => {
      const prevRes = chooseRes();
      state.window = v;
      if (chooseRes() !== prevRes) makePlot(); else setWindowRange();
    });
}

function setAll(val) {
  LOCATIONS.forEach(([name]) => { shown[name] = val; });
  makePlot();
}

async function main() {
  const [fine, coarse] = await Promise.all([
    fetch("viewer-fine.json").then(r => r.json()),
    fetch("viewer-coarse.json").then(r => r.json()),
  ]);
  datasets.fine = parseDataset(fine);
  datasets.coarse = parseDataset(coarse);
  renderControls();
  makePlot();
  document.getElementById("all").addEventListener("click", () => setAll(true));
  document.getElementById("none").addEventListener("click", () => setAll(false));
  let rt;
  window.addEventListener("resize", () => {
    clearTimeout(rt);
    rt = setTimeout(() => { if (plot) plot.setSize(chartSize()); }, 150);
  });
}

main();
