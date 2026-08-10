/* 页面：问题诊断 */
// ------------------------- 页面：问题诊断（左导航 + 双栏卡片 + 严重度多选）---------
const SEV_META = { high: "严重", medium: "关注", low: "提示", ok: "健康" };
const SEV_RANK = ["high", "medium", "low", "ok"];

async function renderDiagnosis() {
  const d = await api("/api/diagnosis?" + windowParams());
  S.diag = d;
  if (!S.diagSev) S.diagSev = new Set(["high", "medium", "low", "ok"]);
  S.diagDomain = (d.domains && d.domains[0]) ? d.domains[0].key : null;
  paintDiagnosis();
}

function worstSev(findings) {
  return findings.reduce((a, f) =>
    (SEV_RANK.indexOf(f.severity) < SEV_RANK.indexOf(a) ? f.severity : a), "ok");
}

function paintDiagnosis() {
  const d = S.diag;
  const content = document.getElementById("content");
  const c = d.counts || {};

  // 左侧导航：每域一项，显示状态点 + 过滤后条目数
  const nav = (d.domains || []).map((dom) => {
    const shown = dom.findings.filter((f) => S.diagSev.has(f.severity));
    const active = dom.key === S.diagDomain ? " active" : "";
    return `<div class="diag-nav-item${active}" data-domain="${dom.key}">
      <span class="diag-nav-dot dot-${worstSev(dom.findings)}"></span>
      <span>${dom.label}</span><span class="n">${shown.length}</span></div>`;
  }).join("");

  // 严重度多选筛选条
  const sevChips = SEV_RANK.map((s) => {
    const on = S.diagSev.has(s) ? " on" : "";
    const dotc = { high: "var(--bad)", medium: "var(--warn)", low: "var(--info)", ok: "var(--good)" }[s];
    return `<span class="sev-chip${on}" data-sev="${s}"><span class="sw" style="background:${dotc}"></span>${SEV_META[s]} ${c[s] || 0}</span>`;
  }).join("");

  // 当前域的卡片（双栏），按严重度过滤
  const dom = (d.domains || []).find((x) => x.key === S.diagDomain);
  const cards = dom
    ? dom.findings.filter((f) => S.diagSev.has(f.severity)).map(findingCard).join("")
    : "";
  const cardsHTML = cards
    ? `<div class="diag-cards">${cards}</div>`
    : `<div class="empty">当前严重度筛选下该域无条目</div>`;

  content.innerHTML = `
    <div class="sec-h">对 ${d.sessions_analyzed} 个会话做全域体检 · 左侧选域，卡片按严重度筛选</div>
    <div class="diag-toolbar"><span class="fg-label">严重度</span>${sevChips}</div>
    <div class="diag-layout">
      <div class="diag-nav">${nav}</div>
      <div>
        <div class="panel-head"><span class="panel-title">${dom ? escapeHTML(dom.label) : ""}</span></div>
        ${cardsHTML}
        <div class="caveat-foot">${d.caveat || ""}</div>
      </div>
    </div>`;

  content.querySelectorAll(".diag-nav-item").forEach((el) =>
    el.addEventListener("click", () => { S.diagDomain = el.dataset.domain; paintDiagnosis(); }));
  content.querySelectorAll(".sev-chip").forEach((el) =>
    el.addEventListener("click", () => {
      const s = el.dataset.sev;
      if (S.diagSev.has(s)) S.diagSev.delete(s); else S.diagSev.add(s);
      if (S.diagSev.size === 0) S.diagSev.add(s); // 至少留一个
      paintDiagnosis();
    }));
}

function findingCard(f) {
  const ev = (f.detail || []).map((e) => `<div class="f-ev">${escapeHTML(e)}</div>`).join("");
  const action = f.action
    ? `<div class="f-action">建议：${escapeHTML(f.action).replace(/「([^」]+)」/g, "<b>「$1」</b>")}</div>`
    : "";
  const badge = `<span class="f-badge sevb-${f.severity}">${SEV_META[f.severity] || f.severity}</span>`;
  return `<div class="finding sev-${f.severity}">
    <div class="f-head"><span class="f-title">${escapeHTML(f.title)}</span>${badge}</div>
    ${action}${ev}
  </div>`;
}

