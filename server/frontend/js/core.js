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

// ------------------------- 安全工具 -------------------------
// 转义 HTML 特殊字符，防止 XSS。所有用户可控数据插入 innerHTML 前必须经过此函数。
function escapeHTML(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}


// ------------------------- 多选下拉（tag 占位） -------------------------
// 通用多选组件：一个下拉框，未选时显示占位符，选中项以可删除的 tag 形式占位在框内，
// 点击展开面板勾选。替代原生 <select multiple>（一行拉不开、不好用）。
//   createMultiSelect(container, { options:[{value,label}], values:[], placeholder, searchable, onChange })
// 返回 { getValues, setValues, destroy }。
function createMultiSelect(container, opts = {}) {
  const options = opts.options || [];
  const placeholder = opts.placeholder || "请选择";
  const searchable = opts.searchable !== false;
  let values = new Set(opts.values || []);
  const labelOf = (v) => {
    const o = options.find((x) => x.value === v);
    return o ? o.label : v;
  };

  container.classList.add("ms");
  container.innerHTML =
    '<div class="ms-control" tabindex="0">' +
      '<div class="ms-tags"></div>' +
      '<span class="ms-arrow" aria-hidden="true">▾</span>' +
    '</div>' +
    '<div class="ms-panel hidden">' +
      (searchable ? '<input class="ms-search" type="text" placeholder="搜索…">' : "") +
      '<div class="ms-options"></div>' +
    '</div>';

  const control = container.querySelector(".ms-control");
  const tagsEl = container.querySelector(".ms-tags");
  const panel = container.querySelector(".ms-panel");
  const search = container.querySelector(".ms-search");
  const optsEl = container.querySelector(".ms-options");

  function renderTags() {
    if (!values.size) {
      tagsEl.innerHTML = '<span class="ms-ph">' + escapeHTML(placeholder) + '</span>';
      return;
    }
    tagsEl.innerHTML = Array.from(values).map((v) =>
      '<span class="ms-tag"><span class="ms-tag-tx">' + escapeHTML(labelOf(v)) + '</span>' +
      '<button class="ms-tag-x" data-value="' + escapeHTML(v) + '" title="移除" type="button">×</button></span>'
    ).join("");
  }

  function renderOptions() {
    const q = ((search && search.value) || "").toLowerCase();
    const rows = options.filter((o) => !q || String(o.label).toLowerCase().includes(q));
    if (!rows.length) {
      optsEl.innerHTML = '<div class="ms-empty">无匹配项</div>';
      return;
    }
    optsEl.innerHTML = rows.map((o) => {
      const on = values.has(o.value);
      return '<div class="ms-opt ' + (on ? "on" : "") + '" data-value="' + escapeHTML(o.value) + '">' +
        '<span class="ms-check">' + (on ? "✓" : "") + '</span>' +
        '<span class="ms-opt-tx">' + escapeHTML(o.label) + '</span></div>';
    }).join("");
  }

  function open() {
    panel.classList.remove("hidden");
    container.classList.add("open");
    renderOptions();
    if (search) { search.value = ""; search.focus(); }
  }
  function close() { panel.classList.add("hidden"); container.classList.remove("open"); }
  function toggle() { panel.classList.contains("hidden") ? open() : close(); }

  control.addEventListener("click", (e) => {
    if (e.target.closest(".ms-tag-x")) return;   // 删除 tag 交给下方处理
    toggle();
  });
  control.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    else if (e.key === "Escape") close();
  });
  tagsEl.addEventListener("click", (e) => {
    const x = e.target.closest(".ms-tag-x");
    if (!x) return;
    e.stopPropagation();
    values.delete(x.dataset.value);
    renderTags(); renderOptions();
    if (opts.onChange) opts.onChange(getValues());
  });
  optsEl.addEventListener("click", (e) => {
    const opt = e.target.closest(".ms-opt");
    if (!opt) return;
    const v = opt.dataset.value;
    if (values.has(v)) values.delete(v); else values.add(v);
    renderTags(); renderOptions();
    if (opts.onChange) opts.onChange(getValues());
  });
  if (search) search.addEventListener("input", renderOptions);

  // 点击组件外部关闭面板。
  const onDocClick = (e) => { if (!container.contains(e.target)) close(); };
  document.addEventListener("click", onDocClick);

  function getValues() { return Array.from(values); }
  function setValues(vs) { values = new Set(vs || []); renderTags(); renderOptions(); }
  function destroy() { document.removeEventListener("click", onDocClick); }

  renderTags();
  return { getValues, setValues, destroy };
}
