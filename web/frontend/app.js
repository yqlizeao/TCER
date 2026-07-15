"use strict";

/* ==========================================================================
 * TCER 仪表盘 SPA
 * 视图：总览 / 项目聚合 / 人员聚合 / 模型聚合 / 会话详情
 * 图表：ECharts（vendor 本地引入，运行时零联网）
 * ======================================================================== */

const TOKEN_KEY = "tcer_token";
let token = localStorage.getItem(TOKEN_KEY) || null;
const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ---- API 封装 -------------------------------------------------------------
async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (token) headers["Authorization"] = "Bearer " + token;
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { logout(); throw new Error("未授权，请重新登录"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("请求失败 " + res.status));
  return data;
}

// ---- 指标定义（前端唯一展示源，与后端字段名对齐）-------------------------
const METRICS = [
  { key: "tcer", name: "TCER 效率", unit: "行/百万Token", fmt: "float2" },
  { key: "ctei", name: "CTEI 综合分", unit: "", fmt: "float3" },
  { key: "net_loc", name: "净增行", unit: "行", fmt: "int" },
  { key: "total_tokens", name: "总 Token", unit: "", fmt: "tok" },
  { key: "cost_usd", name: "成本", unit: "USD", fmt: "money" },
  { key: "churn_ratio", name: "返工率", unit: "", fmt: "pct" },
  { key: "chr", name: "缓存命中率", unit: "", fmt: "pct" },
  { key: "read_before_write", name: "先读后写率", unit: "", fmt: "pct" },
  { key: "search_edit_ratio", name: "搜索后编辑比", unit: "", fmt: "pct" },
  { key: "tool_error_rate", name: "工具错误率", unit: "", fmt: "pct" },
];
const METRIC_BY_KEY = Object.fromEntries(METRICS.map((m) => [m.key, m]));

function fmtVal(fmt, v) {
  if (v === null || v === undefined) return "—";
  switch (fmt) {
    case "int": return Math.round(v).toLocaleString();
    case "float2": return Number(v).toFixed(2);
    case "float3": return Number(v).toFixed(3);
    case "pct": return (v * 100).toFixed(1) + "%";
    case "money": return "$" + Number(v).toFixed(2);
    case "tok": return fmtCompact(v);
    case "M": return (v / 1e6).toFixed(2) + "M";
    default: return String(v);
  }
}
function fmtCompact(v) {
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(Math.round(v));
}

// ---- 全局筛选状态 ---------------------------------------------------------
const state = {
  view: "dashboard",
  days: 7,                 // 总览时间范围
  dashMetric: "tcer",
  filters: { persons: [], projects: [], models: [] },  // 会话视图多选
  sessStart: "", sessEnd: "",
  activeSession: null,
  filterOptions: { persons: [], projects: [], models: [] },
};

function rangeParams(days) {
  const end = Math.floor(Date.now() / 1000);
  const start = end - days * 86400;
  return { start, end };
}

// ---- 主题色板 -------------------------------------------------------------
const PALETTE = ["#5b8cff", "#38bda8", "#ffb454", "#e06c9f", "#b48ead",
  "#56c7c7", "#7ed957", "#f6c453", "#9b8cff", "#ff8a65"];

// =====================================================================
//  通用弹窗
// =====================================================================
let _modalCloser = null;
function openModal({ cls, title, bodyHtml, onMount, onClose }) {
  closeModal();
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.innerHTML = `
    <div class="modal ${cls || ""}">
      <div class="modal-head"><h3>${title || ""}</h3>
        <button class="modal-x" type="button">&times;</button></div>
      <div class="modal-body">${bodyHtml || ""}</div>
    </div>`;
  document.body.appendChild(mask);
  const close = () => {
    if (onClose) try { onClose(); } catch (e) { /* ignore */ }
    mask.remove();
    _modalCloser = null;
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  mask.addEventListener("click", (e) => { if (e.target === mask) close(); });
  mask.querySelector(".modal-x").addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  _modalCloser = close;
  if (onMount) onMount(mask);
}
function closeModal() { if (_modalCloser) _modalCloser(); }

// =====================================================================
//  ECharts 曲线
// =====================================================================
const charts = {};        // id -> echarts instance
const chartState = {};    // id -> { series, metric, title } for zoom modal

// Build the ECharts option. `big` uses roomier symbols/lines for the modal.
function chartOption(series, metric, big) {
  const groups = Object.keys(series || {});
  const m = METRIC_BY_KEY[metric] || METRIC_BY_KEY.tcer;
  if (!groups.length) {
    return { graphic: { type: "text", left: "center", top: "middle",
      style: { text: "该范围暂无数据", fill: "#8b90a0", fontSize: 13 } } };
  }
  const seriesOpt = groups.map((g, i) => ({
    name: g,
    type: "line",
    smooth: 0.35,
    showSymbol: !!big,
    symbolSize: 5,
    lineStyle: { width: big ? 2.6 : 2.2, color: PALETTE[i % PALETTE.length] },
    itemStyle: { color: PALETTE[i % PALETTE.length] },
    areaStyle: groups.length <= 4 ? {
      opacity: big ? 0.14 : 0.10, color: PALETTE[i % PALETTE.length],
    } : undefined,
    emphasis: { focus: "series" },
    data: series[g].map(([t, v]) => [t * 1000, v]),
  }));
  return {
    color: PALETTE,
    // containLabel keeps axis labels inside the grid; bottom reserves room for
    // the legend row so it never overlaps the x-axis labels.
    grid: { left: 6, right: 14, top: 12, bottom: 30, containLabel: true },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#1e2027", borderColor: "#2b2e38",
      textStyle: { color: "#e6e8ee", fontSize: 12 },
      valueFormatter: (v) => fmtVal(m.fmt, v),
    },
    legend: {
      type: "scroll", bottom: 0, textStyle: { color: "#9aa0b0", fontSize: 11 },
      itemWidth: 12, itemHeight: 8, icon: "roundRect", inactiveColor: "#4a4f5c",
    },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: "#2b2e38" } },
      axisLabel: { color: "#8b90a0", fontSize: 11, hideOverlap: true,
        formatter: (v) => { const d = new Date(v); return (d.getMonth() + 1) + "/" + d.getDate(); } },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: "#8b90a0", fontSize: 11, formatter: (v) => fmtCompact(v) },
      splitLine: { lineStyle: { color: "#23262f" } },
    },
    series: seriesOpt,
  };
}

function lineChart(elId, series, metric, title) {
  const el = $(elId);
  let inst = charts[elId];
  if (!inst) { inst = echarts.init(el, null, { renderer: "canvas" }); charts[elId] = inst; }
  chartState[elId] = { series, metric, title: title || "" };
  inst.clear();
  inst.setOption(chartOption(series, metric, false), true);
  inst.resize();
}

window.addEventListener("resize", () => {
  for (const k in charts) charts[k].resize();
  if (modalChart) modalChart.resize();
});

// ---- 图表放大弹窗 ---------------------------------------------------------
let modalChart = null;
function openChartModal(elId) {
  const st = chartState[elId];
  if (!st || !Object.keys(st.series || {}).length) return;
  const mName = (METRIC_BY_KEY[st.metric] || {}).name || "";
  openModal({
    cls: "chart-modal",
    title: `${st.title} · ${mName}`,
    bodyHtml: `<div class="chart" id="modal-chart"></div>`,
    onMount: () => {
      modalChart = echarts.init($("modal-chart"), null, { renderer: "canvas" });
      modalChart.setOption(chartOption(st.series, st.metric, true), true);
      modalChart.resize();
    },
    onClose: () => { if (modalChart) { modalChart.dispose(); modalChart = null; } },
  });
}

// =====================================================================
//  总览仪表盘
// =====================================================================
async function loadDashboard() {
  renderMetricSelect();
  const { start, end } = rangeParams(state.days);
  const p = new URLSearchParams({ metric: state.dashMetric, start, end });
  let data;
  try {
    data = await api("/api/overview?" + p.toString());
  } catch (e) {
    $("kpis").innerHTML = `<div class="empty-note">加载失败：${e.message}</div>`;
    return;
  }
  renderKPIs(data.totals || {});
  const mName = (METRIC_BY_KEY[state.dashMetric] || {}).name || "";
  $("metric-name-1").textContent = mName;
  $("metric-name-2").textContent = mName;
  $("metric-name-3").textContent = mName;
  lineChart("chart-person", data.series.person, state.dashMetric, "按人员");
  lineChart("chart-project", data.series.project, state.dashMetric, "按项目");
  lineChart("chart-model", data.series.model, state.dashMetric, "按模型");
}

function renderKPIs(t) {
  const totalTok = t.total_tokens || 0;
  const kpis = [
    { cls: "k-token", label: "总 Token", value: fmtVal("M", totalTok), unit: "M",
      sub: `输入 ${fmtVal("M", t.input_tokens || 0)}M · 输出 ${fmtVal("M", t.output_tokens || 0)}M<br>` +
           `缓存创建 ${fmtVal("M", t.cache_write_tokens || 0)}M · 缓存命中 ${fmtVal("M", t.cache_read_tokens || 0)}M` },
    { cls: "k-loc", label: "代码净增长", value: (t.net_loc || 0).toLocaleString(), unit: "行",
      sub: `${t.sessions || 0} 个会话` },
    { cls: "k-cost", label: "总成本", value: "$" + (t.cost_usd || 0).toFixed(2), unit: "",
      sub: "按站点价目表估算" },
    { cls: "k-tcer", label: "总评分 TCER", value: (t.tcer != null ? t.tcer.toFixed(2) : "—"), unit: "行/百万Token",
      sub: `缓存命中率 ${t.chr != null ? (t.chr * 100).toFixed(1) + "%" : "—"}` },
  ];
  $("kpis").innerHTML = kpis.map((k) => `
    <div class="kpi ${k.cls}">
      <div class="k-label">${k.label}</div>
      <div class="k-value">${k.value}<span class="k-unit">${k.unit}</span></div>
      <div class="k-sub">${k.sub}</div>
    </div>`).join("");
}

function renderMetricSelect() {
  const sel = $("dash-metric");
  if (sel.options.length) return;
  sel.innerHTML = METRICS.map((m) => `<option value="${m.key}">${m.name}</option>`).join("");
  sel.value = state.dashMetric;
  sel.addEventListener("change", () => { state.dashMetric = sel.value; loadDashboard(); });
}

// =====================================================================
//  聚合详情表（项目 / 人员 / 模型）
// =====================================================================
const DETAIL_COLS = [
  { key: "sessions", name: "会话数", fmt: "int" },
  { key: "total_tokens", name: "总 Token", fmt: "tok" },
  { key: "net_loc", name: "净增行", fmt: "int" },
  { key: "cost_usd", name: "成本", fmt: "money" },
  { key: "tcer", name: "TCER", fmt: "float2" },
  { key: "ctei", name: "CTEI", fmt: "float3" },
  { key: "chr", name: "缓存命中", fmt: "pct" },
  { key: "churn_ratio", name: "返工率", fmt: "pct" },
  { key: "read_before_write", name: "先读后写", fmt: "pct" },
  { key: "tool_error_rate", name: "工具错误", fmt: "pct" },
];
const DIM_LABEL = { project: "项目", person: "人员", model: "模型" };

async function loadDetail(dimension) {
  const titles = { project: "项目聚合详情", person: "人员聚合详情", model: "模型聚合详情" };
  const subs = {
    project: "按项目名聚合所有人上传的数据；名称不一致可点「归并」手动调整",
    person: "按上报人聚合；同一个人的不同账号名可点「归并」合并",
    model: "自动识别归一（如 claude-opus-4-8 与 claude-opus-4.8 视为同一模型），也可手动归并",
  };
  $("detail-title").textContent = titles[dimension];
  $("detail-sub").textContent = subs[dimension];
  const tbl = $("detail-table");
  tbl.innerHTML = `<tbody><tr><td class="spinner">加载中…</td></tr></tbody>`;

  let data;
  try {
    data = await api("/api/detail?dimension=" + dimension);
  } catch (e) {
    tbl.innerHTML = `<tbody><tr><td class="empty-note">加载失败：${e.message}</td></tr></tbody>`;
    return;
  }
  const rows = data.rows || [];
  if (!rows.length) { tbl.innerHTML = `<tbody><tr><td class="empty-note">暂无数据</td></tr></tbody>`; return; }

  // 归并对所有三个维度开放
  const head = `<thead><tr><th>${DIM_LABEL[dimension]}</th>` +
    DETAIL_COLS.map((c) => `<th>${c.name}</th>`).join("") + `</tr></thead>`;
  const body = rows.map((r) => {
    const rawInfo = (r.raw_names && r.raw_names.length > 1)
      ? `<span class="raw-list">合并自：${r.raw_names.map(esc).join(" · ")}</span>` : "";
    const mergeBtn =
      `<button class="grp-alias-btn" data-kind="${dimension}" data-group="${esc(r.group)}" data-raws="${esc((r.raw_names||[]).join("|"))}">归并</button>`;
    const cells = DETAIL_COLS.map((c) => `<td>${fmtVal(c.fmt, r[c.key])}</td>`).join("");
    return `<tr><td><span class="grp-name">${esc(r.display || r.group)}</span>${mergeBtn}${rawInfo}</td>${cells}</tr>`;
  }).join("");
  tbl.innerHTML = head + `<tbody>${body}</tbody>`;

  $$(".grp-alias-btn", tbl).forEach((b) => b.addEventListener("click", onMergeClick));
}

// 当前维度下所有可归并的原始名（供归并弹窗选择目标 / 追加来源）
let _detailRawNames = [];
async function onMergeClick(ev) {
  const btn = ev.currentTarget;
  const kind = btn.dataset.kind;
  const group = btn.dataset.group;
  const raws = (btn.dataset.raws || "").split("|").filter(Boolean);
  const sourceNames = raws.length ? raws : [group];

  // 收集本维度所有分组名，作为「归并到已有分组」的候选
  let allGroups = [];
  try {
    const d = await api("/api/detail?dimension=" + kind);
    allGroups = (d.rows || []).map((r) => r.group);
  } catch (e) { /* 退化为仅手填 */ }

  openMergeModal(kind, group, sourceNames, allGroups);
}

function openMergeModal(kind, group, sourceNames, allGroups) {
  const dimName = DIM_LABEL[kind] || kind;
  const others = allGroups.filter((g) => g !== group && g !== "未标注");
  const optionHtml = others.map((g) => `<option value="${esc(g)}">${esc(g)}</option>`).join("");
  openModal({
    cls: "merge-modal",
    title: `归并${dimName}`,
    bodyHtml: `
      <div class="merge-hint">将选中的原始${dimName}名统一映射到一个标准名称。
        映射后所有相关数据会按标准名聚合，可随时改回。</div>
      <div class="merge-field">
        <label>要归并的原始名称（取消勾选可排除）</label>
        <div class="merge-raws" id="merge-raws">
          ${sourceNames.map((n) => `<label><input type="checkbox" value="${esc(n)}" checked>${esc(n)}</label>`).join("")}
        </div>
      </div>
      ${others.length ? `
      <div class="merge-field">
        <label>归并到已有${dimName}（可选）</label>
        <select id="merge-existing"><option value="">— 不选，使用下方自定义 —</option>${optionHtml}</select>
      </div>` : ""}
      <div class="merge-field">
        <label>标准名称</label>
        <input id="merge-target" type="text" value="${esc(group)}" placeholder="输入标准名称">
      </div>
      <div class="merge-actions">
        <button class="btn-ghost" type="button" id="merge-cancel">取消</button>
        <button class="btn-primary" type="button" id="merge-ok">确定归并</button>
      </div>`,
    onMount: () => {
      const existing = $("merge-existing");
      if (existing) existing.addEventListener("change", () => {
        if (existing.value) $("merge-target").value = existing.value;
      });
      $("merge-cancel").addEventListener("click", closeModal);
      $("merge-ok").addEventListener("click", () => submitMerge(kind));
    },
  });
}

async function submitMerge(kind) {
  const canonical = $("merge-target").value.trim();
  if (!canonical) { $("merge-target").focus(); return; }
  const names = $$("#merge-raws input:checked").map((i) => i.value);
  if (!names.length) { return; }
  const okBtn = $("merge-ok");
  okBtn.disabled = true; okBtn.textContent = "归并中…";
  try {
    for (const raw of names) {
      await api("/api/aliases", { method: "POST",
        body: JSON.stringify({ kind, raw, canonical }) });
    }
    closeModal();
    await loadFilters();
    await loadDetail(kind);
  } catch (e) {
    okBtn.disabled = false; okBtn.textContent = "确定归并";
    alert("归并失败：" + e.message);
  }
}

// =====================================================================
//  会话详情视图
// =====================================================================
async function loadSessions() {
  const p = new URLSearchParams();
  const f = state.filters;
  if (f.persons.length) p.set("persons", f.persons.join(","));
  if (f.projects.length) p.set("projects", f.projects.join(","));
  if (f.models.length) p.set("models", f.models.join(","));
  const s = dateToTs(state.sessStart, false), e = dateToTs(state.sessEnd, true);
  if (s) p.set("start", s);
  if (e) p.set("end", e);

  const list = $("sess-list");
  list.innerHTML = `<div class="spinner">加载中…</div>`;
  let data;
  try {
    data = await api("/api/sessions?" + p.toString());
  } catch (err) {
    list.innerHTML = `<div class="empty-note">加载失败：${err.message}</div>`;
    return;
  }
  const sessions = data.sessions || [];
  if (!sessions.length) {
    list.innerHTML = `<div class="empty-note">无匹配会话</div>`;
    $("sess-detail").innerHTML = `<div class="placeholder">无匹配会话</div>`;
    return;
  }
  list.innerHTML = sessions.map((s) => {
    const tag = s.aggregate_only ? `<span class="tag">仅聚合</span>` : "";
    return `<div class="sess-item" data-id="${s.id}">
      <div class="si-title">${tag}${esc(s.title)}</div>
      <div class="si-meta">
        <span>${esc(s.person || "—")}</span>
        <span>${esc(s.project || "—")}</span>
        <span>${esc(s.model || "—")}</span>
        <span class="muted">${fmtDate(s.ts)}</span>
      </div>
    </div>`;
  }).join("");
  $$(".sess-item", list).forEach((it) =>
    it.addEventListener("click", () => selectSession(it.dataset.id, it)));
  if (data.total > sessions.length) {
    list.insertAdjacentHTML("afterbegin",
      `<div class="si-meta" style="padding:8px 14px;">共 ${data.total} 条，显示前 ${sessions.length} 条</div>`);
  }
}

async function selectSession(id, el) {
  $$(".sess-item").forEach((x) => x.classList.remove("active"));
  if (el) el.classList.add("active");
  state.activeSession = id;
  const box = $("sess-detail");
  box.innerHTML = `<div class="spinner">加载中…</div>`;
  let d;
  try { d = await api("/api/session?id=" + encodeURIComponent(id)); }
  catch (e) { box.innerHTML = `<div class="placeholder">加载失败：${e.message}</div>`; return; }

  if (d.aggregate_only) {
    box.innerHTML = `
      <div class="view-head"><h1>${esc(d.title || "聚合记录")}</h1><span class="tag">仅聚合信息</span></div>
      <div class="placeholder">本次会话仅上传聚合信息，无逐会话明细。<br>
      如需查看 JSONL 详细解析，请在客户端以「含明细」方式上传。</div>
      ${renderKvGrid(d.raw)}`;
    return;
  }
  box.innerHTML = `
    <div class="view-head"><h1>${esc(d.title || d.session_id || ("#" + d.id))}</h1>
      <span class="muted">${esc(d.person || "")} · ${esc(d.project || "")} · ${fmtDate(d.ts)}</span></div>
    ${renderKvGrid(d.raw)}
    ${renderConversation(d.raw)}
    <div class="detail-json">
      <h2 style="font-size:15px;">原始上传数据</h2>
      <details><summary class="muted">展开原始 JSON</summary>
        <pre>${esc(JSON.stringify(d.raw, null, 2))}</pre></details>
    </div>`;
}

// ---- 逐回合会话渲染 -------------------------------------------------------
// 后端 raw.conversation 是跨源统一的 block 列表（Claude / Codex / Grok /
// OpenCode 都归一到同一形状）：
//   {role:"user"|"assistant"|"tool", type:"text"|"thinking"|"tool_use"|"tool_result", ...}
const ROLE_LABEL = {
  user: "用户", assistant: "助手", tool: "工具",
};
const BLOCK_META = {
  text: { cls: "cv-text" },
  thinking: { cls: "cv-think", tag: "思考" },
  tool_use: { cls: "cv-tool", tag: "工具调用" },
  tool_result: { cls: "cv-result", tag: "工具结果" },
};

function renderConversation(raw) {
  const convo = raw && Array.isArray(raw.conversation) ? raw.conversation : null;
  if (!convo || !convo.length) {
    // 仅上传了聚合/指标、未附带明细会话时的提示
    return `<div class="conversation">
      <h2 style="font-size:15px;">会话明细</h2>
      <p class="muted">本会话未包含逐回合明细。如需查看对话，请在客户端以「含明细」方式重新上传。</p>
    </div>`;
  }
  const items = convo.map((b) => {
    const role = b.role || "assistant";
    const meta = BLOCK_META[b.type] || { cls: "cv-text" };
    const roleLabel = ROLE_LABEL[role] || role;
    const tag = meta.tag ? `<span class="cv-tag">${esc(meta.tag)}</span>` : "";
    let body;
    if (b.type === "tool_use") {
      const inp = b.input && typeof b.input === "object"
        ? JSON.stringify(b.input, null, 2) : String(b.input == null ? "" : b.input);
      body = `<div class="cv-toolname">${esc(b.name || "工具")}</div>` +
        (inp && inp !== "{}" ? `<pre class="cv-pre">${esc(inp)}</pre>` : "");
    } else if (b.type === "tool_result") {
      const errCls = b.is_error ? " cv-err" : "";
      body = `<pre class="cv-pre${errCls}">${esc(truncateText(b.text || "", 4000))}</pre>`;
    } else {
      body = `<div class="cv-body">${esc(b.text || "")}</div>`;
    }
    return `<div class="cv-block ${meta.cls} cv-${esc(role)}">
      <div class="cv-head"><span class="cv-role">${esc(roleLabel)}</span>${tag}
        ${b.ts ? `<span class="cv-ts">${fmtTime(b.ts)}</span>` : ""}</div>
      ${body}
    </div>`;
  }).join("");
  return `<div class="conversation">
    <h2 style="font-size:15px;">会话明细 <span class="muted">（${convo.length} 个回合块）</span></h2>
    <div class="cv-list">${items}</div>
  </div>`;
}

function truncateText(s, max) {
  s = String(s == null ? "" : s);
  return s.length > max ? s.slice(0, max) + "\n…（已截断）" : s;
}
function fmtTime(ts) {
  // block ts 是 epoch ms
  const d = new Date(ts);
  if (isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function renderKvGrid(raw) {
  if (!raw || typeof raw !== "object") return "";
  const items = [
    ["TCER", fmtVal("float2", raw.tcer)],
    ["CTEI", fmtVal("float3", raw.ctei)],
    ["评级", raw.grade || "—"],
    ["净增行", fmtVal("int", raw.net_loc)],
    ["总 Token", fmtVal("tok", raw.total_tokens)],
    ["成本", fmtVal("money", raw.cost_usd)],
    ["缓存命中率", fmtVal("pct", raw.chr)],
    ["返工率", fmtVal("pct", raw.churn_ratio)],
  ];
  return `<div class="kv-grid">` + items.map(([k, v]) =>
    `<div class="kv"><div class="kv-k">${k}</div><div class="kv-v">${v}</div></div>`).join("") + `</div>`;
}

// ---- 会话视图筛选下拉（多选）---------------------------------------------
function renderDropdown(elId, options, selected, onChange) {
  const el = $(elId);
  const label = () => selected.length ? `已选 ${selected.length} 项` : "全部";
  el.innerHTML = `<button type="button" class="dd-toggle"><span>${label()}</span><span>▾</span></button>
    <div class="dd-menu hidden"></div>`;
  const toggle = el.querySelector(".dd-toggle");
  const menu = el.querySelector(".dd-menu");
  menu.innerHTML = options.length
    ? options.map((o) => `<label><input type="checkbox" value="${esc(o)}" ${selected.includes(o) ? "checked" : ""}>${esc(o)}</label>`).join("")
    : `<div class="muted" style="padding:6px;">无数据</div>`;
  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    const wasOpen = !menu.classList.contains("hidden");
    // 关闭其它所有下拉，再切换自己 —— 保证同一时刻只开一个
    closeAllDropdowns();
    if (!wasOpen) menu.classList.remove("hidden");
  });
  // 阻止菜单内部点击冒泡到 document（否则刚点复选框就被全局关闭）
  menu.addEventListener("click", (e) => e.stopPropagation());
  $$("input", menu).forEach((cb) => cb.addEventListener("change", () => {
    const vals = $$("input:checked", menu).map((i) => i.value);
    onChange(vals);
    toggle.querySelector("span").textContent = vals.length ? `已选 ${vals.length} 项` : "全部";
  }));
}

function closeAllDropdowns() {
  $$(".dd-menu").forEach((m) => m.classList.add("hidden"));
}
// 点击页面任意其它位置：关闭所有下拉
document.addEventListener("click", closeAllDropdowns);

// =====================================================================
//  路由 / 视图切换
// =====================================================================
const VIEWS = {
  dashboard: () => { showView("view-dashboard"); loadDashboard(); },
  "detail-project": () => { showView("view-detail"); loadDetail("project"); },
  "detail-person": () => { showView("view-detail"); loadDetail("person"); },
  "detail-model": () => { showView("view-detail"); loadDetail("model"); },
  sessions: () => { showView("view-sessions"); setupSessionFilters(); loadSessions(); },
};

function showView(id) {
  $$(".view").forEach((v) => v.classList.add("hidden"));
  $(id).classList.remove("hidden");
}

function navigate(view) {
  state.view = view;
  $$(".menu-item").forEach((m) => m.classList.toggle("active", m.dataset.view === view));
  (VIEWS[view] || VIEWS.dashboard)();
}

let sessionFiltersReady = false;
function setupSessionFilters() {
  const o = state.filterOptions;
  renderDropdown("fd-projects", o.projects, state.filters.projects, (v) => state.filters.projects = v);
  renderDropdown("fd-persons", o.persons, state.filters.persons, (v) => state.filters.persons = v);
  renderDropdown("fd-models", o.models, state.filters.models, (v) => state.filters.models = v);
  if (sessionFiltersReady) return;
  sessionFiltersReady = true;
  $("sess-apply").addEventListener("click", () => {
    state.sessStart = $("fs-start").value;
    state.sessEnd = $("fs-end").value;
    loadSessions();
  });
}

// ---- 过滤选项 -------------------------------------------------------------
async function loadFilters() {
  try {
    const f = await api("/api/filters");
    state.filterOptions = {
      persons: f.persons || [], projects: f.projects || [], models: f.models || [],
    };
  } catch { /* 忽略 */ }
}

// ---- 工具函数 -------------------------------------------------------------
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function dateToTs(value, endOfDay) {
  if (!value) return null;
  const d = new Date(value + (endOfDay ? "T23:59:59" : "T00:00:00"));
  return Math.floor(d.getTime() / 1000);
}
function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

// =====================================================================
//  登录 / 启动
// =====================================================================
async function login(username, password) {
  const data = await api("/api/login", { method: "POST",
    body: JSON.stringify({ username, password }) });
  token = data.token;
  localStorage.setItem(TOKEN_KEY, token);
}
function logout() {
  token = null;
  localStorage.removeItem(TOKEN_KEY);
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
}

async function enterApp(username) {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("who").textContent = username || "";
  await loadFilters();
  navigate("dashboard");
}

// ---- 事件绑定 -------------------------------------------------------------
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-err").textContent = "";
  try {
    const u = $("username").value;
    await login(u, $("password").value);
    await enterApp(u);
  } catch (err) { $("login-err").textContent = err.message; }
});
$("logout").addEventListener("click", logout);
$("side-toggle").addEventListener("click", () => {
  $("app").classList.toggle("collapsed");
  // 侧栏宽度变化后重算图表尺寸
  setTimeout(() => { for (const k in charts) charts[k].resize(); }, 200);
});
$$(".menu-item").forEach((m) =>
  m.addEventListener("click", () => navigate(m.dataset.view)));
$$("#range-tabs button").forEach((b) => b.addEventListener("click", () => {
  $$("#range-tabs button").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  state.days = Number(b.dataset.days);
  loadDashboard();
}));

// 图表点击放大（卡片标题与图表区域都可触发）
["person", "project", "model"].forEach((dim) => {
  const cardId = "chart-" + dim;
  const card = $(cardId).closest(".chart-card");
  if (card) card.addEventListener("click", () => openChartModal(cardId));
});

// 已持有 token 则自动进入
(async () => {
  if (token) {
    try { await enterApp(); }
    catch { logout(); }
  }
})();