/* 页面：项目总览 */
// ------------------------- 页面：项目总览（参考设计还原）-------------------------
const PROJ_COLORS = ["#2dd4bf", "#34d399", "#38bdf8", "#a78bfa", "#fbbf24", "#f43f5e"];
function projColor(i) { return PROJ_COLORS[i % PROJ_COLORS.length]; }

async function renderProjects() {
  const d = await api("/api/projects?" + windowParams());
  const k = d.kpis;
  const content = document.getElementById("content");

  const momPct = k.penetration_mom == null ? "" :
    `<span class="kpi-delta ${k.penetration_mom >= 0 ? "delta-up" : "delta-down"}">↗ ${(k.penetration_mom * 100).toFixed(1)}% MoM</span>`;

  const kpis = `
    <div class="kpi">
      <div class="kpi-label">监控项目数</div>
      <div class="kpi-value">${k.total_projects}</div>
      <span class="kpi-delta delta-up">↗ 新增 ${k.new_active} 个</span>
    </div>
    <div class="kpi">
      <div class="kpi-label">平均 AI 渗透率</div>
      <div class="kpi-value">${k.mean_penetration == null ? "—" : (k.mean_penetration * 100).toFixed(1)}<span class="unit">%</span></div>
      ${momPct || '<span class="kpi-sub">先读后写比例代理</span>'}
    </div>
    <div class="kpi">
      <div class="kpi-label">综合 ROI（估算）</div>
      <div class="kpi-value">${k.blended_roi == null ? "—" : k.blended_roi}<span class="unit">×</span></div>
      <span class="kpi-sub">基准 1.0×（TCER ${d.baseline_tcer}）</span>
    </div>
    <div class="kpi">
      <div class="kpi-label">效率离散度</div>
      <div class="kpi-value val-good">${k.efficiency_variance == null ? "—" : k.efficiency_variance}<span class="unit">%</span></div>
      <span class="kpi-sub">ROI 标准差 / 均值</span>
    </div>`;

  content.innerHTML = `
    <div class="kpi-row">${kpis}</div>
    <div class="grid c3">
      <div class="panel">
        <div class="panel-head"><div><div class="panel-title">ROI 与 AI 渗透率</div>
          <div class="panel-note">气泡大小 = 净增代码量</div></div></div>
        <div id="ch-bubble" class="chart" style="height:320px"></div>
      </div>
      <div class="panel">
        <div class="panel-head"><div><div class="panel-title">效率离群项目</div>
          <div class="panel-note">偏离均值 >1.5σ</div></div></div>
        ${outlierPanel(d.outliers)}
      </div>
    </div>
    <div class="panel">
      <div class="panel-head"><div class="panel-title">项目归一化数据</div></div>
      ${projNormTable(d.projects)}
    </div>`;

  disposeCharts();
  drawBubble(d.projects);
}

function outlierPanel(outliers) {
  if (!outliers || !outliers.length)
    return `<div class="empty">窗口内无 >1.5σ 离群项目</div>`;
  return outliers.map((o) => `
    <div class="outlier">
      <div class="outlier-head">
        <span class="outlier-name">${o.project}</span>
        <span class="pill ${o.over ? "over" : "under"}">${o.over ? "超预期" : "低于预期"}</span>
        <span class="outlier-roi"><span class="v ${o.over ? "val-good" : "val-bad"}">${o.roi}×</span><br><span class="l">ROI</span></span>
      </div>
      <div class="outlier-reason">${o.reason}</div>
    </div>`).join("");
}

function projNormTable(rows) {
  if (!rows.length) return `<div class="empty">无项目数据</div>`;
  const body = rows.map((r, i) => {
    const pen = r.penetration == null ? 0 : r.penetration * 100;
    const penBad = r.roi != null && r.roi < 1;
    const accCls = r.acceptance != null && r.acceptance < 0.5 ? "val-bad" : "val-good";
    const roiCls = r.roi != null && r.roi < 1 ? "val-bad" : "val-good";
    const hrsCls = r.hours_saved != null && r.hours_saved < 0 ? "val-bad" : "";
    return `<tr>
      <td><span class="pid"><span class="dot" style="background:${projColor(i)}"></span>${r.project}</span></td>
      <td>${r.stack || "—"}</td>
      <td><div class="pen-cell"><span>${pen.toFixed(1)}%</span><span class="pen-bar ${penBad ? "bad" : ""}"><i style="width:${Math.min(pen,100)}%"></i></span></div></td>
      <td class="num ${accCls}">${r.acceptance == null ? "—" : (r.acceptance*100).toFixed(1)+"%"}</td>
      <td class="num ${hrsCls}">${r.hours_saved == null ? "—" : fmt.int(r.hours_saved)+"h"}</td>
      <td class="num ${roiCls}">${r.roi == null ? "—" : r.roi+"×"}</td>
      <td><span class="model-pill">${r.primary_model || "—"}</span></td>
    </tr>`;
  }).join("");
  return `<table class="tbl"><thead><tr>
    <th>项目</th><th>技术栈</th><th>AI 渗透率</th>
    <th>采纳率</th><th>节省工时</th><th>综合 ROI</th><th>主要模型</th>
  </tr></thead><tbody>${body}</tbody></table>`;
}

function drawBubble(projs) {
  const el = document.getElementById("ch-bubble");
  if (!el) return;
  const c = baseChart(el);
  const maxLoc = Math.max(...projs.map((p) => p.net_loc || 0), 1);
  const data = projs.map((p, i) => ({
    value: [p.penetration == null ? 0 : p.penetration * 100, p.roi || 0,
            p.net_loc || 0, p.project],
    symbolSize: 14 + Math.sqrt((p.net_loc || 0) / maxLoc) * 42,
    itemStyle: { color: (p.roi != null && p.roi < 1) ? "#f43f5e" : projColor(i),
                 opacity: 0.85, borderColor: "#0a0c0f", borderWidth: 1 },
  }));
  // regression-ish baseline line across the x-range
  const xs = projs.map((p) => (p.penetration || 0) * 100);
  const minX = Math.min(...xs, 40), maxX = Math.max(...xs, 95);
  c.setOption({
    backgroundColor: "transparent",
    grid: { left: 52, right: 24, top: 20, bottom: 42 },
    tooltip: {
      backgroundColor: "#151a20", borderColor: "#1e242c",
      textStyle: { color: "#e8eef2", fontSize: 12 },
      formatter: (p) => `${p.value[3]}<br>渗透率 ${p.value[0].toFixed(1)}% · ROI ${p.value[1]}× · 净增 ${p.value[2].toLocaleString()} 行`,
    },
    xAxis: {
      name: "AI Penetration (%)", nameLocation: "middle", nameGap: 28,
      nameTextStyle: { color: "#5a6673", fontSize: 11 }, min: Math.max(0, minX - 8), max: Math.min(100, maxX + 8),
      axisLine: { lineStyle: { color: "#1e242c" } }, axisLabel: { color: "#5a6673", fontSize: 10 },
      splitLine: { show: true, lineStyle: { color: "#141a20", type: "dashed" } },
    },
    yAxis: {
      name: "ROI Multiplier", nameLocation: "middle", nameGap: 34, nameRotate: 90,
      nameTextStyle: { color: "#5a6673", fontSize: 11 }, scale: true,
      axisLine: { lineStyle: { color: "#1e242c" } }, axisLabel: { color: "#5a6673", fontSize: 10 },
      splitLine: { show: true, lineStyle: { color: "#141a20", type: "dashed" } },
    },
    series: [
      { type: "line", data: [[minX - 8, 0.4], [maxX + 8, (maxX + 8) / 22]],
        lineStyle: { color: "#2a343d", type: "dashed", width: 1 }, symbol: "none",
        silent: true, z: 1 },
      { type: "scatter", data: data, z: 3 },
    ],
  });
}

