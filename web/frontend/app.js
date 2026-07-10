"use strict";

// --- token persistence ----------------------------------------------------
const TOKEN_KEY = "tcer_token";
let token = localStorage.getItem(TOKEN_KEY) || null;

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (token) headers["Authorization"] = "Bearer " + token;
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    logout();
    throw new Error("未授权，请重新登录");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("请求失败 " + res.status));
  return data;
}

// --- auth ------------------------------------------------------------------
async function login(username, password) {
  const data = await api("/api/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  token = data.token;
  localStorage.setItem(TOKEN_KEY, token);
}

function logout() {
  token = null;
  localStorage.removeItem(TOKEN_KEY);
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
}

// --- filter state ----------------------------------------------------------
function checkedValues(containerId) {
  return Array.from(document.querySelectorAll(`#${containerId} input:checked`)).map((i) => i.value);
}

function renderMulti(containerId, values) {
  const el = $(containerId);
  el.innerHTML = "";
  if (!values.length) { el.classList.add("empty"); return; }
  el.classList.remove("empty");
  for (const v of values) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = v;
    label.appendChild(cb);
    label.appendChild(document.createTextNode(v));
    el.appendChild(label);
  }
}

async function loadFilters() {
  const f = await api("/api/filters");
  renderMulti("f-persons", f.persons || []);
  renderMulti("f-projects", f.projects || []);
  renderMulti("f-models", f.models || []);
}

function dateToTs(value, endOfDay) {
  if (!value) return null;
  const d = new Date(value + (endOfDay ? "T23:59:59" : "T00:00:00"));
  return Math.floor(d.getTime() / 1000);
}

function commonParams(metric) {
  const p = new URLSearchParams();
  p.set("metric", metric);
  const persons = checkedValues("f-persons");
  const projects = checkedValues("f-projects");
  const models = checkedValues("f-models");
  if (persons.length) p.set("persons", persons.join(","));
  if (projects.length) p.set("projects", projects.join(","));
  if (models.length) p.set("models", models.join(","));
  const start = dateToTs($("f-start").value, false);
  const end = dateToTs($("f-end").value, true);
  if (start) p.set("start", start);
  if (end) p.set("end", end);
  return p;
}

// --- chart (inline SVG line chart) -----------------------------------------
const PALETTE = ["#4ea1ff", "#ffb454", "#5ec76a", "#e06c9f", "#b48ead", "#56c7c7", "#d9a441", "#8ec07c"];

// Smooth path through points via Catmull-Rom → cubic Bézier. `pts` is [[x,y],…]
// already in pixel space. Tension 0 = standard Catmull-Rom.
function smoothPath(pts) {
  if (pts.length < 2) return pts.length ? `M${pts[0][0]} ${pts[0][1]}` : "";
  if (pts.length === 2) return `M${pts[0][0]} ${pts[0][1]}L${pts[1][0]} ${pts[1][1]}`;
  let d = `M${pts[0][0]} ${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] || p2;
    const c1x = p1[0] + (p2[0] - p0[0]) / 6;
    const c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6;
    const c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += `C${c1x} ${c1y} ${c2x} ${c2y} ${p2[0]} ${p2[1]}`;
  }
  return d;
}

function drawChart(containerId, result) {
  const el = $(containerId);
  el.innerHTML = "";
  const series = result.series || {};
  const groups = Object.keys(series);
  if (!groups.length) {
    el.innerHTML = '<div class="empty-note">该筛选条件下暂无数据</div>';
    return;
  }

  const W = Math.max(680, el.clientWidth || 680), H = 300;
  const m = { top: 16, right: 16, bottom: 34, left: 54 };
  const iw = W - m.left - m.right, ih = H - m.top - m.bottom;

  let tMin = Infinity, tMax = -Infinity, vMin = Infinity, vMax = -Infinity;
  for (const g of groups) {
    for (const [t, v] of series[g]) {
      if (t < tMin) tMin = t; if (t > tMax) tMax = t;
      if (v < vMin) vMin = v; if (v > vMax) vMax = v;
    }
  }
  if (tMin === tMax) { tMax = tMin + 1; }
  if (vMin === vMax) { vMax = vMin + 1; vMin = Math.min(vMin, 0); }
  if (vMin > 0) vMin = 0;

  const sx = (t) => m.left + ((t - tMin) / (tMax - tMin)) * iw;
  const sy = (v) => m.top + ih - ((v - vMin) / (vMax - vMin)) * ih;

  const svgns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgns, "svg");
  svg.setAttribute("width", W);
  svg.setAttribute("height", H);

  const mk = (tag, attrs) => {
    const e = document.createElementNS(svgns, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };

  // gridlines + y labels
  for (let i = 0; i <= 4; i++) {
    const val = vMin + ((vMax - vMin) * i) / 4;
    const y = sy(val);
    svg.appendChild(mk("line", { x1: m.left, y1: y, x2: W - m.right, y2: y, stroke: "#3e3e42", "stroke-width": 1 }));
    const txt = mk("text", { x: m.left - 8, y: y + 4, fill: "#9a9a9a", "font-size": 11, "text-anchor": "end" });
    txt.textContent = fmtNum(val);
    svg.appendChild(txt);
  }
  // x labels (start / mid / end)
  for (const frac of [0, 0.5, 1]) {
    const t = tMin + (tMax - tMin) * frac;
    const x = sx(t);
    const txt = mk("text", { x, y: H - 12, fill: "#9a9a9a", "font-size": 11, "text-anchor": "middle" });
    txt.textContent = fmtDate(t);
    svg.appendChild(txt);
  }

  groups.forEach((g, gi) => {
    const color = PALETTE[gi % PALETTE.length];
    const pts = series[g].slice().sort((a, b) => a[0] - b[0]);
    const px = pts.map((p) => [sx(p[0]), sy(p[1])]);
    svg.appendChild(mk("path", { d: smoothPath(px), fill: "none", stroke: color, "stroke-width": 2 }));
    for (const p of pts) {
      const c = mk("circle", { cx: sx(p[0]), cy: sy(p[1]), r: 3, fill: color });
      const title = document.createElementNS(svgns, "title");
      title.textContent = `${g}\n${fmtDate(p[0])}  ${fmtNum(p[1])}`;
      c.appendChild(title);
      svg.appendChild(c);
    }
  });

  el.appendChild(svg);

  const legend = document.createElement("div");
  legend.className = "legend";
  groups.forEach((g, gi) => {
    const span = document.createElement("span");
    const i = document.createElement("i");
    i.style.background = PALETTE[gi % PALETTE.length];
    span.appendChild(i);
    span.appendChild(document.createTextNode(g));
    legend.appendChild(span);
  });
  el.appendChild(legend);
}

function fmtNum(v) {
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + "K";
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(2);
}
function fmtDate(ts) {
  const d = new Date(ts * 1000);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

// --- refresh ---------------------------------------------------------------
async function refresh() {
  const metric = $("f-metric").value;
  const dims = [
    ["person", "chart-person"],
    ["project", "chart-project"],
    ["model", "chart-model"],
  ];
  const metricLabel = $("f-metric").selectedOptions[0].textContent;
  $("t-person").textContent = `按人员 · ${metricLabel}`;
  $("t-project").textContent = `按项目 · ${metricLabel}`;
  $("t-model").textContent = `按模型 · ${metricLabel}`;
  await Promise.all(dims.map(async ([dim, cid]) => {
    const p = commonParams(metric);
    p.set("dimension", dim);
    try {
      const result = await api("/api/series?" + p.toString());
      drawChart(cid, result);
    } catch (e) {
      $(cid).innerHTML = `<div class="empty-note">加载失败：${e.message}</div>`;
    }
  }));
}

async function enterApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  await loadFilters();
  await refresh();
}

// --- wire up ---------------------------------------------------------------
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-err").textContent = "";
  try {
    await login($("username").value, $("password").value);
    await enterApp();
  } catch (err) {
    $("login-err").textContent = err.message;
  }
});
$("apply").addEventListener("click", refresh);
$("logout").addEventListener("click", logout);

// auto-enter if we already hold a token
(async () => {
  if (token) {
    try { await enterApp(); }
    catch { logout(); }
  }
})();