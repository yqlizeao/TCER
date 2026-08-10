/* 页面：投入产出 */
// ------------------------- 页面：成本与产出 -------------------------
async function renderExec() {
  const d = await api("/api/exec?" + windowParams());
  const k = d.kpis;
  const content = document.getElementById("content");

  // 三大块：效率 = 产出 / 成本。效率块并排放 TCER 与单位成本，
  // 产出块给分子（代码净增长），成本块给分母侧（投入成本/Token/缓存）。
  const groups = `
    <div class="kpi-group">
      <div class="kpi-group-head"><span class="kpi-group-title">效率</span>
        <span class="kpi-group-formula">= 产出 / 成本</span></div>
      <div class="kpi-group-body two">
        <div class="kpi-mini">
          <div class="kpi-label">产出效率 TCER</div>
          <div class="kpi-value">${fmt.f2(k.tcer.value)}<span class="unit">行/M</span></div>
          ${deltaHTML(k.tcer, false)}
        </div>
        <div class="kpi-mini">
          <div class="kpi-label">单位成本</div>
          <div class="kpi-value">${fmt.f2(k.cpe.value)}<span class="unit">$/千行</span></div>
          ${deltaHTML(k.cpe, true)}
        </div>
      </div>
    </div>
    <div class="kpi-group">
      <div class="kpi-group-head"><span class="kpi-group-title">产出</span></div>
      <div class="kpi-group-body">
        <div class="kpi-mini">
          <div class="kpi-label">代码净增长</div>
          <div class="kpi-value">${fmt.int(k.net_loc.value)}<span class="unit">行</span></div>
          ${deltaHTML(k.net_loc, false)}
          <div class="kpi-sub">${k.sessions.value} 个会话</div>
        </div>
      </div>
    </div>
    <div class="kpi-group">
      <div class="kpi-group-head"><span class="kpi-group-title">成本</span></div>
      <div class="kpi-group-body two">
        <div class="kpi-mini">
          <div class="kpi-label">总投入成本</div>
          <div class="kpi-value">${fmt.money(k.cost_usd.value)}</div>
          ${deltaHTML(k.cost_usd, true)}
        </div>
        <div class="kpi-mini">
          <div class="kpi-label">Token 消耗</div>
          <div class="kpi-value">${fmt.tokens(k.total_tokens.value)}</div>
          <div class="kpi-sub">缓存命中 ${fmt.pct(d.cache.chr)}</div>
        </div>
      </div>
    </div>`;

  content.innerHTML = `
    <div class="kpi-groups">${groups}</div>
    <div class="grid c2">
      <div class="panel">
        <div class="panel-head"><span class="panel-title">投入 vs 产出趋势</span>
          <span class="panel-note">每日：成本(左) · 净增行(右)</span></div>
        <div id="ch-roi" class="chart"></div>
      </div>
      <div class="panel">
        <div class="panel-head"><span class="panel-title">单位成本走势</span>
          <span class="panel-note">$/千行 越低越好</span></div>
        <div id="ch-cpe" class="chart"></div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head"><span class="panel-title">项目投入产出排名</span>
        <span class="panel-note">按成本降序 · 谁把预算转成了代码</span></div>
      ${projectRoiTable(d.projects)}
    </div>`;

  disposeCharts();
  const x = d.trend.map((p) => fmt.date(p.ts));
  lineChart(document.getElementById("ch-roi"), [
    { name: "成本 $", type: "line", smooth: true, data: d.trend.map((p) => p.cost_usd),
      yAxisIndex: 0, itemStyle: { color: PALETTE[3] }, areaStyle: { opacity: 0.08 } },
    { name: "净增行", type: "line", smooth: true, data: d.trend.map((p) => p.net_loc),
      yAxisIndex: 1, itemStyle: { color: PALETTE[1] } },
  ], { x, legend: true, yAxis: [{ fmt: (v) => "$" + v }, { fmt: (v) => v }] });

  lineChart(document.getElementById("ch-cpe"), [
    { name: "$/千行", type: "line", smooth: true, data: d.trend.map((p) => p.cpe),
      itemStyle: { color: PALETTE[2] }, areaStyle: { opacity: 0.08 } },
  ], { x });
}

function projectRoiTable(rows) {
  if (!rows.length) return `<div class="empty">窗口内无项目数据</div>`;
  const maxCost = Math.max(...rows.map((r) => r.cost_usd || 0), 1);
  const body = rows.map((r) => `
    <tr>
      <td>${escapeHTML(r.project)}</td>
      <td class="num bar-cell"><div class="bar" style="width:${((r.cost_usd || 0) / maxCost * 100).toFixed(0)}%"></div><span>${fmt.money(r.cost_usd)}</span></td>
      <td class="num">${r.cost_share == null ? "—" : fmt.pct(r.cost_share)}</td>
      <td class="num">${fmt.int(r.net_loc)}</td>
      <td class="num">${fmt.f2(r.cpe)}</td>
      <td class="num">${fmt.f2(r.tcer)}</td>
      <td class="num">${tierChip(r.tier)}</td>
      <td class="num">${r.sessions}</td>
    </tr>`).join("");
  return `<table class="tbl"><thead><tr>
      <th>项目</th><th>成本</th><th>成本占比</th><th>净增行</th>
      <th>$/千行</th><th>TCER</th><th>评级</th><th>会话</th>
    </tr></thead><tbody>${body}</tbody></table>`;
}

