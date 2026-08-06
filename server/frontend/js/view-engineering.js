/* 页面：工程效能 */
// ------------------------- 页面：工程效能 -------------------------
async function renderEngineering() {
  const d = await api("/api/engineering?" + windowParams());
  const k = d.kpis;
  const content = document.getElementById("content");

  const kpis = [
    kpiCard("活跃会话", fmt.int(k.sessions.value), k.sessions, false, "采用规模"),
    kpiCard("活跃人数", fmt.int(k.active_people.value), k.active_people, false, "上报人去重"),
    kpiCard("综合效率分", fmt.f2(k.score.value), k.score, false, "0–100 三正交轴"),
    kpiCard("返工率", fmt.pct(k.churn_ratio.value), k.churn_ratio, true, "自返工 越低越好"),
    kpiCard("工具错误率", fmt.pct(k.tool_error_rate.value), k.tool_error_rate, true, "失败调用占比"),
    kpiCard("先读后写率", fmt.pct(k.read_before_write.value), k.read_before_write, false, "改前先读 越高越稳"),
  ].join("");

  content.innerHTML = `
    <div class="kpi-row">${kpis}</div>
    ${weakSpotsBanner(d.weak_spots)}
    <div class="grid c2">
      <div class="panel">
        <div class="panel-head"><span class="panel-title">质量趋势</span>
          <span class="panel-note">返工率 · 工具错误率(左) · 综合分(右)</span></div>
        <div id="ch-q" class="chart"></div>
      </div>
      <div class="panel">
        <div class="panel-head"><span class="panel-title">采用趋势</span>
          <span class="panel-note">按天：会话数(柱) · 净增行(线)</span></div>
        <div id="ch-adopt" class="chart"></div>
      </div>
    </div>
    <div class="grid c2">
      <div class="panel">
        <div class="panel-head"><span class="panel-title">不同模型的产出能力</span>
          <span class="panel-note">TCER(柱) · 单位成本(线)</span></div>
        <div id="ch-model" class="chart"></div>
      </div>
      <div class="panel">
        <div class="panel-head"><span class="panel-title">模型效能明细</span>
          <span class="panel-note">按 TCER 降序</span></div>
        ${modelTable(d.models)}
      </div>
    </div>
    <div class="grid c2">
      <div class="panel">
        <div class="panel-head"><span class="panel-title">人员效能</span>
          <span class="panel-note">按综合分升序 · 短板在前</span></div>
        ${entityTable(d.people, "person", "成员")}
      </div>
      <div class="panel">
        <div class="panel-head"><span class="panel-title">项目健康</span>
          <span class="panel-note">按综合分升序</span></div>
        ${entityTable(d.projects, "project", "项目")}
      </div>
    </div>`;

  disposeCharts();
  const x = d.quality_trend.map((p) => fmt.date(p.ts));
  lineChart(document.getElementById("ch-q"), [
    { name: "返工率", type: "line", smooth: true, data: d.quality_trend.map((p) => pctv(p.churn_ratio)),
      itemStyle: { color: PALETTE[3] } },
    { name: "工具错误率", type: "line", smooth: true, data: d.quality_trend.map((p) => pctv(p.tool_error_rate)),
      itemStyle: { color: PALETTE[2] } },
    { name: "综合分", type: "line", smooth: true, data: d.quality_trend.map((p) => p.score),
      yAxisIndex: 1, itemStyle: { color: PALETTE[0] } },
  ], { x, legend: true, yAxis: [{ fmt: (v) => v + "%" }, { fmt: (v) => v, max: 100 }] });

  const xa = d.adoption_trend.map((p) => fmt.date(p.ts));
  lineChart(document.getElementById("ch-adopt"), [
    { name: "会话数", type: "bar", data: d.adoption_trend.map((p) => p.sessions),
      itemStyle: { color: PALETTE[0] } },
    { name: "净增行", type: "line", smooth: true, data: d.adoption_trend.map((p) => p.net_loc),
      yAxisIndex: 1, itemStyle: { color: PALETTE[1] } },
  ], { x: xa, legend: true, yAxis: [{ fmt: (v) => v }, { fmt: (v) => fmt.tokens(v) }] });

  // 模型维度产出：TCER 柱 + 单位成本线
  const mrows = (d.models || []).filter((m) => m.tcer != null);
  if (mrows.length) {
    lineChart(document.getElementById("ch-model"), [
      { name: "TCER", type: "bar", data: mrows.map((m) => m.tcer),
        itemStyle: { color: PALETTE[0] } },
      { name: "单位成本 $/千行", type: "line", smooth: true, data: mrows.map((m) => m.cpe),
        yAxisIndex: 1, itemStyle: { color: PALETTE[2] } },
    ], { x: mrows.map((m) => m.display || m.model), legend: true,
         yAxis: [{ fmt: (v) => v }, { fmt: (v) => "$" + v }] });
  }
}

function modelTable(rows) {
  rows = (rows || []).filter((m) => (m.sessions || 0) > 0);
  if (!rows.length) return `<div class="empty">无模型数据</div>`;
  const body = rows.map((m) => `
    <tr>
      <td><span class="model-pill">${m.display || m.model}</span></td>
      <td class="num">${fmt.f2(m.tcer)}</td>
      <td class="num">${fmt.f2(m.cpe)}</td>
      <td class="num">${fmt.int(m.net_loc)}</td>
      <td class="num">${fmt.pct(m.churn_ratio)}</td>
      <td class="num">${m.sessions}</td>
    </tr>`).join("");
  return `<table class="tbl"><thead><tr>
      <th>模型</th><th>TCER</th><th>$/千行</th><th>净增行</th><th>返工率</th><th>会话</th>
    </tr></thead><tbody>${body}</tbody></table>`;
}

function weakSpotsBanner(spots) {
  if (!spots || !spots.length) {
    return `<div class="hi hi-good" style="margin-bottom:14px">
      <span class="panel-title" style="color:var(--good)">✓ 未发现显著短板</span>
      <span class="panel-note"> — 样本内无人员/项目的返工率或错误率明显高于团队均值</span></div>`;
  }
  const label = { person_churn: "返工率偏高", person_error: "工具错误率偏高",
    project_churn: "项目返工率偏高", project_error: "项目错误率偏高" };
  const items = spots.map((s) => {
    const isPerson = s.kind.startsWith("person");
    return `<div class="f-ev">${isPerson ? "成员" : "项目"} <b>${s.subject}</b> ${label[s.kind] || ""}：
      ${fmt.pct(s.value)} vs 团队 ${fmt.pct(s.team)}（${s.sessions} 会话）</div>`;
  }).join("");
  return `<div class="hi hi-warn" style="margin-bottom:14px">
    <div class="panel-title" style="color:var(--warn);margin-bottom:6px">⚠ 短板提示（样本 ≥5 会话且高于团队均值 20%）</div>
    ${items}</div>`;
}

function entityTable(rows, field, head) {
  if (!rows.length) return `<div class="empty">无数据</div>`;
  const body = rows.map((r) => `
    <tr>
      <td>${r[field]}</td>
      <td class="num">${fmt.f2(r.score)} ${tierChip(r.tier)}</td>
      <td class="num">${fmt.f2(r.tcer)}</td>
      <td class="num">${fmt.pct(r.churn_ratio)}</td>
      <td class="num">${fmt.pct(r.tool_error_rate)}</td>
      <td class="num">${fmt.int(r.net_loc)}</td>
      <td class="num">${r.sessions}</td>
    </tr>`).join("");
  return `<table class="tbl"><thead><tr>
      <th>${head}</th><th>综合分</th><th>TCER</th><th>返工率</th>
      <th>错误率</th><th>净增行</th><th>会话</th>
    </tr></thead><tbody>${body}</tbody></table>`;
}

