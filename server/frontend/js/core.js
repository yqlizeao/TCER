"use strict";
/* TCER 前端 基础层：全局状态 / API / 格式化 / 图表助手。经典脚本按序加载，共享全局作用域，打包时按序拼接即可。 */
"use strict";
/* TCER 效率分析 — 前端控制器
   四个页面：成本与产出 / 工程效能 / 问题诊断 / 会话明细。
   数据全部来自后端聚合端点，前端只呈现、不重算指标。 */

// ------------------------- 状态 -------------------------
const S = {
  token: localStorage.getItem("tcer_token") || "",
  user: localStorage.getItem("tcer_user") || "",
  name: localStorage.getItem("tcer_name") || "",
  avatar: localStorage.getItem("tcer_avatar") || "",
  view: "projects",
  days: 7,
  persons: [],
  projects: [],
  charts: [],
};

// ------------------------- API -------------------------
async function api(path, opts = {}) {
  const headers = Object.assign(
    { "Content-Type": "application/json" },
    S.token ? { Authorization: "Bearer " + S.token } : {},
    opts.headers || {}
  );
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) { logout(); throw new Error("未登录或登录已过期"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function windowParams() {
  let q = "";
  if (S.days) {
    const end = Math.floor(Date.now() / 1000);
    const start = end - S.days * 86400;
    q = `start=${start}&end=${end}`;
  }
  if (S.persons.length) q += (q ? "&" : "") + `persons=${encodeURIComponent(S.persons.join(","))}`;
  if (S.projects.length) q += (q ? "&" : "") + `projects=${encodeURIComponent(S.projects.join(","))}`;
  return q;
}

// ------------------------- 格式化 -------------------------
const fmt = {
  int: (v) => v == null ? "—" : Math.round(v).toLocaleString("en-US"),
  money: (v) => v == null ? "—" : "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  pct: (v) => v == null ? "—" : (v * 100).toFixed(1) + "%",
  f2: (v) => v == null ? "—" : v.toFixed(2),
  tokens: (v) => {
    if (v == null) return "—";
    if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return String(v);
  },
  date: (ts) => {
    const d = new Date(ts * 1000);
    return `${d.getMonth() + 1}/${d.getDate()}`;
  },
};

// delta 方向感知：部分指标越低越好（成本、返工率、错误率、CPE）
function deltaHTML(d, lowerBetter = false) {
  if (!d || d.rel == null) return `<span class="kpi-delta delta-flat">— 无环比对照</span>`;
  const up = d.rel > 0;
  const good = lowerBetter ? !up : up;
  const flat = Math.abs(d.rel) < 0.005;
  const cls = flat ? "delta-flat" : (good ? "delta-up" : "delta-down");
  const arrow = flat ? "→" : (up ? "▲" : "▼");
  return `<span class="kpi-delta ${cls}">${arrow} ${(Math.abs(d.rel) * 100).toFixed(1)}% 环比</span>`;
}

function kpiCard(label, valueHTML, delta, lowerBetter, sub) {
  return `<div class="kpi">
    <div class="kpi-label">${label}</div>
    <div class="kpi-value">${valueHTML}</div>
    ${deltaHTML(delta, lowerBetter)}
    ${sub ? `<div class="kpi-sub">${sub}</div>` : ""}
  </div>`;
}

function tierChip(tier) {
  if (!tier) return "—";
  const good = ["优秀", "良好"].includes(tier);
  const bad = ["较差", "很差", "差"].includes(tier);
  const cls = good ? "tier-good" : bad ? "tier-bad" : "tier-mid";
  return `<span class="tier ${cls}">${tier}</span>`;
}

// ------------------------- 图表 -------------------------
const AX = { color: "#6b7885", line: "#2a323d" };
const PALETTE = ["#4c8dff", "#3fb950", "#d29922", "#f85149", "#a371f7", "#39c5cf"];

function baseChart(el) {
  const c = echarts.init(el, null, { renderer: "canvas" });
  S.charts.push(c);
  return c;
}
function disposeCharts() { S.charts.forEach((c) => c.dispose()); S.charts = []; }

function lineChart(el, series, opts = {}) {
  const c = baseChart(el);
  c.setOption({
    backgroundColor: "transparent",
    grid: { left: 48, right: 18, top: 26, bottom: 28 },
    tooltip: { trigger: "axis", backgroundColor: "#1c232d", borderColor: "#2a323d",
      textStyle: { color: "#e6edf3", fontSize: 12 } },
    legend: { show: !!opts.legend, top: 0, right: 0, textStyle: { color: AX.color, fontSize: 11 },
      itemWidth: 10, itemHeight: 10 },
    xAxis: { type: "category", data: opts.x, boundaryGap: series.some((s) => s.type === "bar"),
      axisLine: { lineStyle: { color: AX.line } }, axisLabel: { color: AX.color, fontSize: 11 } },
    yAxis: (opts.yAxis || [{}]).map((y) => Object.assign({
      type: "value", splitLine: { lineStyle: { color: AX.line, type: "dashed" } },
      axisLabel: { color: AX.color, fontSize: 11, formatter: y.fmt }, scale: true,
    }, y)),
    series: series,
  });
  return c;
}
function pctv(v) { return v == null ? null : +(v * 100).toFixed(1); }

