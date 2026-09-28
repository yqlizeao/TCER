/* 页面：团队概念对齐（doc/concept-alignment-server.md §6）
   四个页面：我的概念（个人，仅本人）/ 团队概念（全员）/ 概念诊断（管理者，仅聚合）/ 概念治理（数据入口 + 评审）。
   数字全部来自后端，前端不重算；披露门槛由后端 disclosure.py 执行，前端只展示结果与说明。 */

const CX = { who: null };
const CX_STATUS = { active: "已批准", draft: "草稿", reviewing: "评审中", deprecated: "已废弃" };
const CX_SRC = { imported: "管理员导入", self: "本人填写", feishu: "飞书同步", inferred: "系统推断" };

async function cxWho() {
  if (!CX.who) CX.who = await api("/api/concepts/whoami");
  return CX.who;
}
// 概念页的 KPI 不做环比（kpiCard 会渲染「无环比对照」），用无 delta 的变体。
function cxKpi(label, valueHTML, sub) {
  return `<div class="kpi"><div class="kpi-label">${label}</div><div class="kpi-value">${valueHTML}</div>
    ${sub ? `<div class="kpi-sub">${sub}</div>` : ""}</div>`;
}
function cxRoles() { return new Set((CX.who && CX.who.roles) || []); }
function cxPost(path, body) {
  return api("/api/concepts/" + path, { method: "POST", body: JSON.stringify(body || {}) });
}
function cxEmpty(msg) { return `<div class="empty" style="padding:22px">${escapeHTML(msg)}</div>`; }
function cxChip(status) {
  const cls = { active: "tier-good", draft: "tier-mid", reviewing: "tier-mid", deprecated: "tier-bad" }[status] || "tier-mid";
  return `<span class="tier ${cls}">${CX_STATUS[status] || escapeHTML(status || "")}</span>`;
}
function cxPct(v) { return v == null ? "—" : (v * 100).toFixed(0) + "%"; }
function cxSemanticNote(sem) {
  if (!sem || sem.enabled) return "";
  return `<div class="hi hi-info cx-note">${escapeHTML(sem.note)}</div>`;
}
function cxRulesFoot(r) {
  if (!r) return "";
  return `<div class="caveat-foot">披露规则：管理视图任何分组不足 ${r.min_aggregate_persons} 人不出数；
    候选新词需 ≥${r.candidate_min_persons} 人、≥${r.candidate_min_projects} 个项目使用才进入公共队列；
    公共原文证据只取自设为公开的会话，且不显示作者；职能级统计需 ≥${r.min_function_usages} 次用法、来自 ≥${r.min_function_usage_persons} 人。
    概念数据不参与任何效率评分，也不得用于绩效评估。</div>`;
}
function cxBind(root, sel, ev, fn) { root.querySelectorAll(sel).forEach((el) => el.addEventListener(ev, (e) => fn(el, e))); }
function cxFlash(el, msg, bad) {
  if (!el) return;
  el.innerHTML = `<span style="color:var(${bad ? "--bad" : "--good"})">${escapeHTML(msg)}</span>`;
}

// =============================== 我的概念（个人视图） =============================== //
async function renderConceptsMe() {
  await cxWho();
  const asOwner = S.cxAs || "";
  const d = await api("/api/concepts/me" + (asOwner ? "?as=" + encodeURIComponent(asOwner) : ""));
  const content = document.getElementById("content");
  const fnOpts = Object.entries(CX.who.functions || {}).map(([k, v]) =>
    `<option value="${k}" ${d.profile.function === k ? "selected" : ""}>${escapeHTML(v)}</option>`).join("");
  const grantedBy = CX.who.granted_by || [];
  const switcher = grantedBy.length ? `<div class="cx-switch"><span class="fg-label">查看</span>
    <select id="cx-as" class="filter"><option value="">我自己</option>${grantedBy.map((o) =>
      `<option value="${escapeHTML(o)}" ${o === asOwner ? "selected" : ""}>${escapeHTML(o)}（已授权给我）</option>`).join("")}</select></div>` : "";
  const readonly = d.viewing_as_grantee;
  const c = d.consent;

  const consentPanel = readonly ? "" : `
    <div class="panel">
      <div class="panel-head"><span class="panel-title">数据授权</span>
        <span class="panel-note">只有你本人能看到本页</span></div>
      <div class="cx-kv">
        <div><span>已上传会话</span><b>${c.sessions}</b></div>
        <div><span>含对话文本</span><b>${c.with_text}</b></div>
        <div><span>已授权团队概念分析</span><b>${c.consented}</b></div>
      </div>
      <p class="tk-hint">授权后，你上传的对话会用于统计团队术语的使用情况（不参与任何效率评分）。
        关闭授权会立即删除由你的对话派生的全部分析数据。</p>
      <div class="tk-gen">
        <button class="btn-primary" id="cx-consent-on">授权全部会话</button>
        <button class="btn-ghost" id="cx-consent-off">撤回授权并删除派生数据</button>
        <span id="cx-consent-msg"></span>
      </div>
    </div>`;

  const profilePanel = readonly ? "" : `
    <div class="panel">
      <div class="panel-head"><span class="panel-title">我的职能</span>
        <span class="panel-note">${d.profile.function_source ? "来源：" + (CX_SRC[d.profile.function_source] || "") : "尚未设置"}</span></div>
      <div class="tk-gen" style="flex-wrap:wrap">
        <select id="cx-fn" class="filter"><option value="">未设置</option>${fnOpts}</select>
        <input id="cx-team" class="filter" type="text" placeholder="所在小组（可选）" value="${escapeHTML(d.profile.team || "")}">
        <button class="btn-ghost" id="cx-fn-save">保存</button><span id="cx-fn-msg"></span>
      </div>
      <p class="tk-hint">职能用于团队层面按职能汇总术语用法，不会在任何视图里暴露到你个人。</p>
      <div class="panel-head" style="margin-top:6px"><span class="panel-title">授权他人查看本页</span></div>
      <div class="tk-gen">
        <input id="cx-grantee" class="filter" type="text" placeholder="对方登录名（如导师）">
        <button class="btn-ghost" id="cx-grant">授权</button>
      </div>
      <div class="cx-tags">${(d.grants || []).map((g) =>
        `<span class="ms-tag"><span class="ms-tag-tx">${escapeHTML(g)}</span><button class="ms-tag-x cx-revoke" data-g="${escapeHTML(g)}" type="button" title="撤销">×</button></span>`).join("") || '<span class="panel-note">未授权任何人</span>'}</div>
      <div class="panel-head" style="margin-top:10px"><span class="panel-title">谁看过我的数据</span></div>
      ${(d.access_log || []).length ? `<table class="tbl"><tbody>${d.access_log.map((a) =>
        `<tr><td>${escapeHTML(a.viewer)}</td><td class="num">${fmt.datetime(a.at)}</td></tr>`).join("")}</tbody></table>`
        : '<span class="panel-note">暂无他人访问记录</span>'}
    </div>`;

  const mis = d.ai_misreads || [];
  const misHTML = mis.length ? mis.map((m) => `
    <div class="cx-item">
      <div class="cx-item-h"><b>${escapeHTML(m.label)}</b>
        ${m.in_termbase ? "" : '<span class="tag-agg">未入库</span>'}
        <span class="cx-right">${m.events} 次 · 你的误解率 ${cxPct(m.rate)} · 团队 ${cxPct(m.team_rate)} · 估算浪费 ${fmt.money(m.cost_usd_est)}</span></div>
      ${m.examples.map((e) => `<div class="cx-ev"><span class="cx-lbl">你说</span>${escapeHTML(e.usage)}</div>
        <div class="cx-ev"><span class="cx-lbl">随后纠正</span>${escapeHTML(e.correction)}
        <span class="panel-note">${e.evidence === "rementioned" ? "（纠正时重提了该词）" : "（AI 改过文件后被纠正）"}</span></div>`).join("")}
    </div>`).join("") : cxEmpty("暂未发现 AI 误解你所用术语的情况");

  const subs = d.substitutions || [];
  const jargon = d.private_jargon || [];
  const terms = d.terms || [];
  const fid = d.fidelity || {};

  content.innerHTML = `
    ${switcher}
    ${readonly ? `<div class="hi hi-warn cx-note">你正在以受托身份查看 ${escapeHTML(asOwner)} 的个人视图，本次访问已被记录并对其可见。</div>` : ""}
    <div class="kpi-row">
      ${cxKpi("我用到的团队概念", fmt.int(d.richness.concepts_used), escapeHTML(d.richness.note))}
      ${cxKpi("AI 误解事件", fmt.int(mis.reduce((a, m) => a + m.events, 0)), "术语出现后 1–3 轮内被你纠正")}
      ${cxKpi("估算浪费", fmt.money(mis.reduce((a, m) => a + (m.cost_usd_est || 0), 0)), "按会话成本分摊的估算值")}
      ${cxKpi("子代理术语保真", cxPct(fid.ratio), `${fid.dispatches || 0} 次派发 · ${fid.kept || 0}/${fid.expected || 0} 个术语被保留`)}
    </div>
    <div class="grid c2">${consentPanel}${profilePanel}</div>
    <div class="grid c3">
      <div class="panel">
        <div class="panel-head"><span class="panel-title">AI 最常误解我的词</span>
          <button class="btn-ghost" id="cx-ctx" ${mis.length ? "" : "disabled"}>生成上下文术语段</button></div>
        <div id="cx-ctx-out"></div>
        ${misHTML}
      </div>
      <div class="panel">
        <div class="panel-head"><span class="panel-title">AI 换了我的说法</span></div>
        ${subs.length ? `<table class="tbl"><thead><tr><th>我说</th><th>AI 改说</th><th>次数</th></tr></thead><tbody>${subs.map((s) =>
          `<tr><td>${escapeHTML(s.from_label)}</td><td>${escapeHTML(s.to_label)}</td><td class="num">${s.n}</td></tr>`).join("")}</tbody></table>`
          : cxEmpty("暂无")}
        <p class="tk-hint">同一轮里你用了某术语，AI 却一次没沿用、改用了术语库中另一个概念——说明它可能把两者当成了一回事。</p>
      </div>
    </div>
    <div class="grid c2">
      <div class="panel">
        <div class="panel-head"><span class="panel-title">只有我在用的词</span>
          <span class="panel-note">使用人数不足公共门槛，只有你能看到</span></div>
        ${jargon.length ? `<table class="tbl"><thead><tr><th>说法</th><th>次数</th><th>疑似同义</th><th></th></tr></thead><tbody>${jargon.map((j) =>
          `<tr><td>${escapeHTML(j.surface)}</td><td class="num">${j.freq}</td><td>${escapeHTML(j.nearest_label || "—")}</td>
          <td>${readonly ? "" : `<button class="btn-ghost cx-nom" data-s="${escapeHTML(j.surface)}">提名入库</button>
          <button class="btn-ghost cx-ign" data-s="${escapeHTML(j.surface)}">无需收录</button>`}</td></tr>`).join("")}</tbody></table>`
          : cxEmpty("暂无只有你在用的高频说法")}
      </div>
      <div class="panel">
        <div class="panel-head"><span class="panel-title">我常用的团队概念</span></div>
        ${terms.length ? `<table class="tbl"><thead><tr><th>概念</th><th>状态</th><th>用法</th><th>会话</th><th>被误解</th><th></th></tr></thead><tbody>${terms.slice(0, 30).map((t) =>
          `<tr><td title="${escapeHTML(t.definition)}">${escapeHTML(t.label)}</td><td>${cxChip(t.status)}</td>
          <td class="num">${t.uses}</td><td class="num">${t.sessions}</td><td class="num">${t.misreads || "—"}</td>
          <td>${readonly ? "" : `<button class="btn-ghost cx-dispute" data-slug="${escapeHTML(t.slug)}" data-label="${escapeHTML(t.label)}">基线有问题</button>`}</td></tr>`).join("")}</tbody></table>`
          : cxEmpty("还没有检测到你使用团队术语（需先授权并上传含对话的会话）")}
        ${(d.disputes || []).length ? `<div class="panel-head" style="margin-top:10px"><span class="panel-title">我提出的质疑</span></div>
          <table class="tbl"><tbody>${d.disputes.map((x) => `<tr><td>${escapeHTML((terms.find((t) => t.slug === x.slug) || {}).label || x.slug)}</td><td style="white-space:normal">${escapeHTML(x.body)}</td>
          <td>${x.status === "open" ? "待处理" : x.status === "accepted" ? "已采纳" : "已驳回"}</td><td style="white-space:normal">${escapeHTML(x.reply || "")}</td></tr>`).join("")}</tbody></table>` : ""}
      </div>
    </div>
    ${cxSemanticNote(d.semantic)}`;

  const $ = (id) => document.getElementById(id);
  if ($("cx-as")) $("cx-as").addEventListener("change", (e) => { S.cxAs = e.target.value; route(); });
  if (!readonly) {
    $("cx-consent-on").addEventListener("click", async () => {
      const r = await cxPost("consent", { on: true }); cxFlash($("cx-consent-msg"), `已授权 ${r.updated} 个会话，后台分析中`);
    });
    $("cx-consent-off").addEventListener("click", async () => {
      if (!confirm("撤回后将删除由你的对话派生的全部概念分析数据，确认？")) return;
      await cxPost("consent", { on: false }); cxFlash($("cx-consent-msg"), "已撤回，派生数据将在后台清除");
    });
    $("cx-fn-save").addEventListener("click", async () => {
      try { await cxPost("my-profile", { function: $("cx-fn").value, team: $("cx-team").value.trim() }); cxFlash($("cx-fn-msg"), "已保存"); }
      catch (e) { cxFlash($("cx-fn-msg"), e.message, true); }
    });
    $("cx-grant").addEventListener("click", async () => {
      const g = $("cx-grantee").value.trim(); if (!g) return;
      try { await cxPost("grant", { grantee: g }); route(); } catch (e) { alert(e.message); }
    });
    cxBind(content, ".cx-revoke", "click", async (el) => { await cxPost("grant", { grantee: el.dataset.g, revoke: true }); route(); });
    cxBind(content, ".cx-nom", "click", async (el) => { await cxPost("candidate", { surface: el.dataset.s, action: "nominate" }); el.textContent = "已提名"; el.disabled = true; });
    cxBind(content, ".cx-ign", "click", async (el) => { await cxPost("candidate", { surface: el.dataset.s, action: "ignore" }); el.closest("tr").remove(); });
    cxBind(content, ".cx-dispute", "click", async (el) => {
      const body = prompt(`你认为「${el.dataset.label}」的团队定义哪里有问题？（会提交给概念负责人与评审）`);
      if (!body || !body.trim()) return;
      try { await cxPost("dispute", { slug: el.dataset.slug, body }); alert("已提交"); route(); } catch (e) { alert(e.message); }
    });
  }
  if ($("cx-ctx")) $("cx-ctx").addEventListener("click", async () => {
    const slugs = mis.filter((m) => m.in_termbase).map((m) => m.slug);
    const out = $("cx-ctx-out");
    if (!slugs.length) { out.innerHTML = `<div class="hi hi-warn cx-note">这些词还没有已批准的团队定义，先在「团队概念」里起草入库。</div>`; return; }
    const r = await api("/api/concepts/context?slugs=" + encodeURIComponent(slugs.join(",")));
    cxShowMarkdown(out, r.markdown, "粘贴到项目的 CLAUDE.md / AGENTS.md，让 AI 按团队含义理解这些词");
  });
}

function cxShowMarkdown(el, md, hint) {
  el.innerHTML = `<div class="tk-reveal"><div class="tk-reveal-hd">${escapeHTML(hint)}</div>
    <pre class="cx-pre">${escapeHTML(md)}</pre><button class="btn-ghost cx-copy">复制</button></div>`;
  el.querySelector(".cx-copy").addEventListener("click", (e) =>
    copyText(md).then((ok) => { e.target.textContent = ok ? "已复制" : "复制失败"; }));
}

// =============================== 团队概念（组织全貌） =============================== //
async function renderConceptsOrg() {
  await cxWho();
  const d = await api("/api/concepts/org");
  S.cxOrg = d;
  const content = document.getElementById("content");
  const cs = d.concepts || [];
  const h = d.health || {};
  const q = (S.cxQuery || "").toLowerCase();
  const shown = cs.filter((c) => !q || (c.label + c.slug + c.term_en + (c.alt_labels || []).join(" ")).toLowerCase().includes(q));

  content.innerHTML = `
    <div class="kpi-row">
      ${cxKpi("已批准概念", fmt.int(h.n_active), `草稿 ${h.n_draft || 0} · 已废弃 ${h.n_deprecated || 0}`)}
      ${cxKpi("待评审提案", fmt.int(h.open_proposals), "新建 / 修改 / 废弃")}
      ${cxKpi("待处理质疑", fmt.int(h.open_disputes), h.oldest_dispute_days != null ? `最久 ${h.oldest_dispute_days} 天` : "—")}
      ${cxKpi("候选新词", fmt.int((d.candidates || []).length), "多人多项目在用、尚未入库")}
    </div>
    <div class="grid c3">
      <div class="panel">
        <div class="panel-head"><span class="panel-title">基线定义库</span>
          <div class="tk-gen"><input id="cx-q" class="filter" type="text" placeholder="搜索概念 / 别名" value="${escapeHTML(S.cxQuery || "")}">
          <button class="btn-primary" id="cx-new">起草新概念</button>
          <button class="btn-ghost" id="cx-ctx-all">导出上下文术语段</button></div></div>
        <div id="cx-org-ctx"></div>
        <table class="tbl"><thead><tr><th>概念</th><th class="tx">状态</th><th class="tx">定义</th><th>用法</th><th>人数</th><th>文档提及</th><th>AI 误解率</th></tr></thead>
        <tbody>${shown.map((c) => `<tr class="cx-row" data-slug="${escapeHTML(c.slug)}">
          <td><b>${escapeHTML(c.label)}</b>${c.term_en ? ` <span class="panel-note">${escapeHTML(c.term_en)}</span>` : ""}</td>
          <td class="tx">${cxChip(c.status)}</td>
          <td class="cx-def tx">${escapeHTML(c.definition || "（缺定义）")}</td>
          <td class="num">${c.uses}</td><td class="num">${c.persons}</td><td class="num">${c.doc_mentions || "—"}</td>
          <td class="num">${cxPct(c.misread_rate)}</td></tr>`).join("") || `<tr><td colspan="7">${cxEmpty("术语库为空：可在「概念治理」导入客户端术语库，或起草新概念")}</td></tr>`}</tbody></table>
      </div>
      <div class="panel">
        <div class="panel-head"><span class="panel-title">候选新词</span><span class="panel-note">按显著性排序</span></div>
        ${(d.candidates || []).length ? `<table class="tbl"><thead><tr><th>说法</th><th>人</th><th>项目</th><th>疑似同义</th><th></th></tr></thead><tbody>${d.candidates.map((c) =>
          `<tr><td>${escapeHTML(c.surface)}${c.status === "nominated" ? ' <span class="tag-agg">已提名</span>' : ""}</td>
          <td class="num">${c.n_persons}</td><td class="num">${c.n_projects}</td><td>${escapeHTML(c.nearest_label || "—")}</td>
          <td><button class="btn-ghost cx-draft" data-s="${escapeHTML(c.surface)}">起草</button>
          ${cxRoles().has("reviewer") ? `<button class="btn-ghost cx-rej" data-s="${escapeHTML(c.surface)}">不收录</button>` : ""}</td></tr>`).join("")}</tbody></table>`
          : cxEmpty("暂无达到公共门槛的候选新词")}
      </div>
    </div>
    <div class="grid c2">
      <div class="panel"><div class="panel-head"><span class="panel-title">概念共现网络</span>
        <span class="panel-note">同一句话里一起出现 ≥3 次；只做描述，不代表因果</span></div>
        <div id="cx-graph" class="chart"></div></div>
      <div class="panel"><div class="panel-head"><span class="panel-title">术语使用趋势</span><span class="panel-note">近 6 个月 · 成员原话</span></div>
        <div id="cx-trend" class="chart"></div></div>
    </div>
    <div id="cx-detail"></div>
    ${cxSemanticNote(d.semantic)}
    ${cxRulesFoot(d.rules)}`;

  const $ = (id) => document.getElementById(id);
  $("cx-q").addEventListener("change", (e) => { S.cxQuery = e.target.value; renderConceptsOrg(); });
  $("cx-new").addEventListener("click", () => cxEditor($("cx-detail"), null));
  $("cx-ctx-all").addEventListener("click", async () => {
    const r = await api("/api/concepts/context");
    cxShowMarkdown($("cx-org-ctx"), r.markdown, "全部已批准概念 · 粘贴到项目的 CLAUDE.md / AGENTS.md");
  });
  cxBind(content, ".cx-row", "click", (el) => cxConceptDetail(el.dataset.slug));
  cxBind(content, ".cx-draft", "click", (el) => cxEditor($("cx-detail"), null, { pref_label: el.dataset.s }));
  cxBind(content, ".cx-rej", "click", async (el) => { await cxPost("candidate", { surface: el.dataset.s, action: "reject" }); el.closest("tr").remove(); });
  cxGraph($("cx-graph"), d.cooccurrence, cs);
  cxTrend($("cx-trend"), d.trend);
  if (S.cxSlug) cxConceptDetail(S.cxSlug);
}

function cxGraph(el, co, concepts) {
  const labels = Object.fromEntries((concepts || []).map((c) => [c.slug, c.label]));
  if (!co || !co.edges || !co.edges.length) { el.outerHTML = cxEmpty("共现数据不足"); return; }
  const used = new Set(co.edges.flatMap((e) => [e.a, e.b]));
  const maxC = Math.max(...co.nodes.map((n) => n.count), 1);
  const c = baseChart(el);
  c.setOption({
    backgroundColor: "transparent",
    tooltip: { backgroundColor: "#1c232d", borderColor: "#2a323d", textStyle: { color: "#e6edf3", fontSize: 12 },
      formatter: (p) => p.dataType === "edge"
        ? `${escapeHTML(labels[p.data.source] || p.data.source)} — ${escapeHTML(labels[p.data.target] || p.data.target)}<br>共现 ${p.data.count} 次 · 关联度 ${p.data.npmi}`
        : `${escapeHTML(p.data.name)}<br>出现 ${p.data.count} 次` },
    series: [{
      type: "graph", layout: "force", roam: true, draggable: true,
      force: { repulsion: 180, edgeLength: [60, 160] },
      label: { show: true, position: "right", color: "#e8eef2", fontSize: 11 },
      data: co.nodes.filter((n) => used.has(n.slug)).map((n) => ({
        id: n.slug, name: labels[n.slug] || n.slug, count: n.count,
        symbolSize: 10 + 26 * Math.sqrt(n.count / maxC), itemStyle: { color: "#2dd4bf" } })),
      links: co.edges.map((e) => ({ source: e.a, target: e.b, count: e.count, npmi: e.npmi,
        lineStyle: { width: 1 + 3 * Math.max(0, e.npmi), color: "#47535f", opacity: 0.8 } })),
    }],
  });
  c.on("click", (p) => { if (p.dataType === "node") cxConceptDetail(p.data.id); });
}

function cxTrend(el, t) {
  if (!t || !t.months || !t.months.length) { el.outerHTML = cxEmpty("趋势数据不足"); return; }
  lineChart(el, t.series.map((s, i) => ({ name: s.label, type: "line", smooth: true, showSymbol: false,
    data: s.values, lineStyle: { width: 2, color: PALETTE[i % PALETTE.length] }, itemStyle: { color: PALETTE[i % PALETTE.length] } })),
  { x: t.months, legend: true });
}

async function cxConceptDetail(slug) {
  S.cxSlug = slug;
  const box = document.getElementById("cx-detail");
  if (!box) return;
  box.innerHTML = `<div class="panel">${cxEmpty("加载中…")}</div>`;
  let d;
  try { d = await api("/api/concepts/concept?slug=" + encodeURIComponent(slug)); }
  catch (e) { box.innerHTML = `<div class="panel">${cxEmpty(e.message)}</div>`; return; }
  const e = d.concept.entry;
  const fnl = (CX.who && CX.who.functions) || {};
  const st = d.stats;
  box.innerHTML = `
    <div class="panel cx-detail">
      <div class="panel-head"><span class="panel-title">${escapeHTML(e.pref_label)}${e.term_en ? " · " + escapeHTML(e.term_en) : ""}
        ${cxChip(d.concept.status)} <span class="panel-note">第 ${d.concept.version} 版 · 负责人 ${escapeHTML(d.concept.owner || "未指定")}</span></span>
        <div class="tk-gen"><button class="btn-ghost" id="cx-edit">提出修改</button>
          <button class="btn-ghost" id="cx-dsp">基线有问题</button>
          <button class="btn-ghost" id="cx-close">收起</button></div></div>
      <div class="grid c2" style="margin-bottom:0">
        <div>
          <div class="hi"><div class="eyebrow">团队定义</div><div>${escapeHTML(e.definition || "（尚未填写定义）")}</div></div>
          ${(e.alt_labels || []).length ? `<div class="cx-sub"><span class="cx-lbl">同义叫法</span>${e.alt_labels.map(escapeHTML).join("、")}</div>` : ""}
          ${Object.keys(e.renderings || {}).length ? `<div class="cx-sub"><div class="eyebrow">各职能的说法</div>${Object.entries(e.renderings).map(([k, v]) =>
            `<div class="cx-ev"><span class="cx-lbl">${escapeHTML(fnl[k] || k)}</span>${escapeHTML(v)}</div>`).join("")}</div>` : ""}
          ${(e.misconceptions || []).length ? `<div class="cx-sub"><div class="eyebrow">边界与常见误解</div>${e.misconceptions.map((m) =>
            `<div class="cx-ev"><span class="cx-lbl">${escapeHTML(fnl[m.role] || "全员")}</span>容易以为：${escapeHTML(m.wrong)}${m.actual ? `；实际：${escapeHTML(m.actual)}` : ""}</div>`).join("")}</div>` : ""}
        </div>
        <div>
          <div class="cx-kv">
            <div><span>用法</span><b>${st.uses}</b></div><div><span>使用人数</span><b>${st.persons}</b></div>
            <div><span>项目</span><b>${st.projects}</b></div><div><span>AI 误解</span><b>${st.misreads}</b></div>
            <div><span>文档提及</span><b>${st.doc_mentions}</b></div>
          </div>
          ${d.functions.length ? `<div class="eyebrow" style="margin-top:10px">各职能使用量</div>
            <table class="tbl"><tbody>${d.functions.map((f) => `<tr><td>${escapeHTML(f.label)}</td><td class="num">${f.uses} 次</td><td class="num">${f.persons} 人</td></tr>`).join("")}</tbody></table>` : ""}
          ${d.cooccurs.length ? `<div class="cx-sub"><span class="cx-lbl">常一起出现</span>${d.cooccurs.slice(0, 8).map((x) =>
            escapeHTML(x.a === d.concept.slug ? x.b : x.a)).join("、")}</div>` : ""}
        </div>
      </div>
      <div class="eyebrow" style="margin-top:12px">原文证据（仅来自公开会话与文档，不显示作者）</div>
      ${d.evidence.length ? d.evidence.map((x) => `<div class="cx-ev">${escapeHTML(x.text)} <span class="panel-note">${escapeHTML(x.project || "")} ${fmt.date(x.ts)}</span></div>`).join("")
        : cxEmpty(d.evidence_hidden ? `有 ${d.evidence_hidden} 条用法来自未公开的会话，只计数不展示原文` : "暂无")}
      ${d.disputes.length ? `<div class="eyebrow" style="margin-top:12px">质疑记录</div><table class="tbl"><tbody>${d.disputes.map((x) =>
        `<tr><td style="white-space:normal">${escapeHTML(x.body)}</td><td>${x.status === "open" ? "待处理" : x.status === "accepted" ? "已采纳" : "已驳回"}</td><td style="white-space:normal">${escapeHTML(x.reply || "")}</td></tr>`).join("")}</tbody></table>` : ""}
      ${d.history.length ? `<details class="cx-sub"><summary class="eyebrow">版本历史（${d.history.length}）</summary>${d.history.map((h) =>
        `<div class="cx-ev">第 ${h.version} 版 · ${CX_STATUS[h.status] || h.status} · ${escapeHTML(h.changed_by)} · ${fmt.datetime(h.changed_at)} · ${escapeHTML(h.reason || "")}</div>`).join("")}</details>` : ""}
      ${cxSemanticNote(d.semantic)}
      <div id="cx-editor"></div>
    </div>`;
  document.getElementById("cx-close").addEventListener("click", () => { S.cxSlug = null; box.innerHTML = ""; });
  document.getElementById("cx-edit").addEventListener("click", () => cxEditor(document.getElementById("cx-editor"), d.concept));
  document.getElementById("cx-dsp").addEventListener("click", async () => {
    const body = prompt(`你认为「${e.pref_label}」的团队定义哪里有问题？`);
    if (!body || !body.trim()) return;
    try { await cxPost("dispute", { slug, body }); cxConceptDetail(slug); } catch (err) { alert(err.message); }
  });
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

// 概念编辑器：新建 / 修改都生成「提案」，评审通过才生效。
function cxEditor(el, concept, seed) {
  const e = concept ? concept.entry : Object.assign({ slug: "", pref_label: "", term_en: "", alt_labels: [], definition: "", owner: "", renderings: {}, misconceptions: [] }, seed || {});
  const fnl = (CX.who && CX.who.functions) || {};
  const fnSel = (v) => `<select class="filter cx-role"><option value="">全员</option>${Object.entries(fnl).map(([k, n]) =>
    `<option value="${k}" ${v === k ? "selected" : ""}>${escapeHTML(n)}</option>`).join("")}</select>`;
  const rendRow = (k, v) => `<div class="cx-frow cx-rend">${fnSel(k)}<input class="filter cx-grow" type="text" placeholder="该职能口中的说法" value="${escapeHTML(v || "")}"><button class="btn-ghost cx-del" type="button">删除</button></div>`;
  const miscRow = (m) => `<div class="cx-frow cx-misc">${fnSel(m.role)}<input class="filter cx-grow cx-wrong" type="text" placeholder="容易误以为…" value="${escapeHTML(m.wrong || "")}"><input class="filter cx-grow cx-actual" type="text" placeholder="实际是…" value="${escapeHTML(m.actual || "")}"><button class="btn-ghost cx-del" type="button">删除</button></div>`;
  el.innerHTML = `
    <div class="panel cx-form">
      <div class="panel-head"><span class="panel-title">${concept ? "修改提案：" + escapeHTML(e.pref_label) : "起草新概念"}</span>
        <span class="panel-note">提交后进入评审，由评审批准后生效</span></div>
      <div class="cx-fgrid">
        <label>名称<input id="f-label" class="filter" type="text" value="${escapeHTML(e.pref_label)}"></label>
        <label>英文名<input id="f-en" class="filter" type="text" value="${escapeHTML(e.term_en || "")}"></label>
        <label title="小写字母、数字和连字符，创建后不可修改">唯一标识<input id="f-slug" class="filter" type="text" value="${escapeHTML(e.slug)}" ${concept ? "disabled" : ""} placeholder="如 hot-update"></label>
        <label>负责人<input id="f-owner" class="filter" type="text" value="${escapeHTML(e.owner || "")}" placeholder="登录名"></label>
      </div>
      <label class="cx-full">同义叫法（用顿号或逗号分隔）<input id="f-alt" class="filter" type="text" value="${escapeHTML((e.alt_labels || []).join("、"))}"></label>
      <label class="cx-full">团队定义<textarea id="f-def" class="filter" rows="3">${escapeHTML(e.definition || "")}</textarea></label>
      <div class="eyebrow">各职能的说法</div><div id="f-rends">${Object.entries(e.renderings || {}).map(([k, v]) => rendRow(k, v)).join("")}</div>
      <button class="btn-ghost" id="f-add-rend" type="button">添加一行</button>
      <div class="eyebrow" style="margin-top:10px">边界与常见误解</div><div id="f-miscs">${(e.misconceptions || []).map(miscRow).join("")}</div>
      <button class="btn-ghost" id="f-add-misc" type="button">添加一行</button>
      <label class="cx-full" style="margin-top:10px">修改理由<input id="f-reason" class="filter" type="text" placeholder="说明为什么要新建或修改"></label>
      <div class="tk-gen" style="margin-top:10px">
        <button class="btn-primary" id="f-submit">提交评审</button>
        ${concept && concept.status !== "deprecated" ? '<button class="btn-ghost" id="f-deprecate">提议废弃</button>' : ""}
        <button class="btn-ghost" id="f-cancel">取消</button><span id="f-msg"></span></div>
    </div>`;
  const $ = (id) => document.getElementById(id);
  const wireDel = () => cxBind(el, ".cx-del", "click", (b) => b.closest(".cx-frow").remove());
  $("f-add-rend").addEventListener("click", () => { $("f-rends").insertAdjacentHTML("beforeend", rendRow("", "")); wireDel(); });
  $("f-add-misc").addEventListener("click", () => { $("f-miscs").insertAdjacentHTML("beforeend", miscRow({})); wireDel(); });
  wireDel();
  $("f-cancel").addEventListener("click", () => { el.innerHTML = ""; });
  const collect = () => {
    const rend = {};
    el.querySelectorAll(".cx-rend").forEach((r) => {
      const k = r.querySelector(".cx-role").value, v = r.querySelector("input").value.trim();
      if (k && v) rend[k] = v;
    });
    const miscs = [];
    el.querySelectorAll(".cx-misc").forEach((r) => {
      const w = r.querySelector(".cx-wrong").value.trim();
      if (w) miscs.push({ role: r.querySelector(".cx-role").value, wrong: w, actual: r.querySelector(".cx-actual").value.trim() });
    });
    return Object.assign({}, concept ? concept.entry : {}, {
      slug: $("f-slug").value.trim(), pref_label: $("f-label").value.trim(), term_en: $("f-en").value.trim(),
      owner: $("f-owner").value.trim(), definition: $("f-def").value.trim(),
      alt_labels: $("f-alt").value.split(/[、,，]/).map((s) => s.trim()).filter(Boolean),
      renderings: rend, misconceptions: miscs, status: "active",
    });
  };
  $("f-submit").addEventListener("click", async () => {
    try {
      await cxPost("propose", { entry: collect(), reason: $("f-reason").value.trim() });
      cxFlash($("f-msg"), "已提交评审");
    } catch (err) { cxFlash($("f-msg"), err.message, true); }
  });
  if ($("f-deprecate")) $("f-deprecate").addEventListener("click", async () => {
    const reason = $("f-reason").value.trim();
    if (!reason) { cxFlash($("f-msg"), "请填写废弃理由", true); return; }
    try { await cxPost("propose", { entry: concept.entry, action: "deprecate", reason }); cxFlash($("f-msg"), "已提交废弃提案"); }
    catch (err) { cxFlash($("f-msg"), err.message, true); }
  });
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}

// =============================== 概念诊断（管理视图） =============================== //
async function renderConceptsDiag() {
  await cxWho();
  const content = document.getElementById("content");
  if (!cxRoles().has("manager")) { content.innerHTML = cxEmpty("概念诊断仅对「管理者」角色开放（且只展示 ≥5 人的聚合数据）"); return; }
  const d = await api("/api/concepts/diagnosis");
  const cov = d.coverage;
  const covPanel = `<div class="panel"><div class="panel-head"><span class="panel-title">数据覆盖</span></div>
    <div class="cx-kv">
      <div><span>上传会话</span><b>${cov.sessions}</b></div><div><span>含对话文本</span><b>${cov.with_text}</b></div>
      <div><span>已授权分析</span><b>${cov.consented}</b></div><div><span>授权成员</span><b>${cov.consenting_uploaders} / ${cov.uploaders}</b></div>
    </div>
    <table class="tbl" style="margin-top:8px"><thead><tr><th>职能</th><th>分析覆盖人数</th><th></th></tr></thead><tbody>${cov.functions.map((f) =>
      `<tr><td>${escapeHTML(f.label)}</td><td class="num">${f.n_persons}</td><td>${f.enough ? "" : '<span class="tag-agg">不足 5 人，职能级数据不出数</span>'}</td></tr>`).join("")}</tbody></table></div>`;
  const h = d.health || {};
  const healthPanel = `<div class="panel"><div class="panel-head"><span class="panel-title">基线治理健康度</span></div>
    <div class="cx-kv">
      <div><span>待评审提案</span><b>${h.open_proposals}</b></div><div><span>待处理质疑</span><b>${h.open_disputes}</b></div>
      <div><span>最久未处理</span><b>${h.oldest_dispute_days != null ? h.oldest_dispute_days + " 天" : "—"}</b></div>
      <div><span>无负责人概念</span><b>${(h.no_owner || []).length}</b></div><div><span>缺定义概念</span><b>${(h.no_definition || []).length}</b></div>
    </div>
    ${(h.hot_disputes || []).length ? `<div class="hi hi-warn cx-note">被多次质疑、需要复审：${h.hot_disputes.map(escapeHTML).join("、")}</div>` : ""}</div>`;

  if (d.insufficient) {
    content.innerHTML = `<div class="hi hi-warn cx-note">${escapeHTML(d.reason)}</div>
      <div class="grid c2">${covPanel}${healthPanel}</div>${cxSemanticNote(d.semantic)}${cxRulesFoot(d.rules)}`;
    return;
  }
  const sup = (r) => r.suppressed ? `<td colspan="9" class="panel-note">${escapeHTML(r.reason)}</td>` : null;
  const misRows = (d.misread_concepts || []).map((r) => `<tr><td>${escapeHTML(r.label)}${r.in_termbase === false ? ' <span class="tag-agg">未入库</span>' : ""}</td>${sup(r) ||
    `<td class="num">${r.events}</td><td class="num">${cxPct(r.rate)}</td><td class="num">${cxPct(r.strong_ratio)}</td><td class="num">${r.median_turns}</td><td class="num">${fmt.money(r.cost_usd_est)}</td><td class="num">${fmt.tokens(r.tokens_est)}</td><td class="num">${r.n_persons}</td>`}</tr>`).join("");
  const projRows = (d.misread_projects || []).map((r) => `<tr><td>${escapeHTML(r.label)}</td>${sup(r) ||
    `<td class="num">${r.events}</td><td class="num">${fmt.money(r.cost_usd_est)}</td><td class="tx">${(r.top_concepts || []).map(escapeHTML).join("、")}</td>`}</tr>`).join("");
  const subRows = (d.substitutions || []).map((r) => `<tr><td>${escapeHTML(r.label)}</td>${sup(r) || `<td class="num">${r.events}</td><td class="num">${r.n_persons}</td>`}</tr>`).join("");
  const fidRows = (d.fidelity || []).map((r) => `<tr><td>${escapeHTML(r.label)}</td>${sup(r) || `<td class="num">${r.dispatches}</td><td class="num">${cxPct(r.ratio)}</td>`}</tr>`).join("");
  const crossRows = (d.cross_function || []).map((r) => `<tr><td>${escapeHTML(r.label)}</td>${sup(r) ||
    `<td class="tx">${escapeHTML(r.project)}</td><td class="tx">${r.functions.map(escapeHTML).join("、")}</td><td class="num">${cxPct(r.misread_rate)}</td>`}</tr>`).join("");

  content.innerHTML = `
    <div class="grid c2">${covPanel}${healthPanel}</div>
    <div class="panel" style="margin-bottom:var(--gap)">
      <div class="panel-head"><span class="panel-title">人 ↔ AI：AI 误解团队术语的代价</span>
        <span class="panel-note">术语出现后 1–3 轮内被纠正；代价为按会话成本分摊的估算</span></div>
      ${misRows ? `<table class="tbl"><thead><tr><th>概念</th><th>事件</th><th>误解率</th><th>强证据占比</th><th>纠正间隔(轮)</th><th>估算浪费</th><th>估算 Token</th><th>涉及人数</th></tr></thead><tbody>${misRows}</tbody></table>` : cxEmpty("暂无")}
      <div class="f-action" style="margin-top:10px">建议：把误解率高的概念写进项目上下文（<b>「团队概念 → 导出上下文术语段」</b>），之后对比同一项目的误解率变化。</div>
    </div>
    <div class="grid c2">
      <div class="panel"><div class="panel-head"><span class="panel-title">误解代价最高的项目</span></div>
        ${projRows ? `<table class="tbl"><thead><tr><th>项目</th><th>事件</th><th>估算浪费</th><th class="tx">主要概念</th></tr></thead><tbody>${projRows}</tbody></table>` : cxEmpty("暂无")}</div>
      <div class="panel"><div class="panel-head"><span class="panel-title">同项目跨职能共用</span>
        <span class="panel-note">近 30 天同一项目内 ≥2 个职能都在用的概念</span></div>
        ${crossRows ? `<table class="tbl"><thead><tr><th>概念</th><th class="tx">项目</th><th class="tx">职能</th><th>AI 误解率</th></tr></thead><tbody>${crossRows}</tbody></table>` : cxEmpty("暂无（需要成员设置职能）")}</div>
    </div>
    <div class="grid c2">
      <div class="panel"><div class="panel-head"><span class="panel-title">AI 换词</span><span class="panel-note">用户说 A，AI 改说 B</span></div>
        ${subRows ? `<table class="tbl"><thead><tr><th>替换</th><th>次数</th><th>人数</th></tr></thead><tbody>${subRows}</tbody></table>` : cxEmpty("暂无")}</div>
      <div class="panel"><div class="panel-head"><span class="panel-title">AI ↔ AI：子代理术语保真</span><span class="panel-note">派发任务时保留了用户原话中术语的比例</span></div>
        ${fidRows ? `<table class="tbl"><thead><tr><th>项目</th><th>派发次数</th><th>保真率</th></tr></thead><tbody>${fidRows}</tbody></table>` : cxEmpty("暂无")}</div>
    </div>
    ${cxSemanticNote(d.semantic)}
    ${cxRulesFoot(d.rules)}`;
}

// =============================== 概念治理（评审 + 数据入口） =============================== //
async function renderConceptsAdmin() {
  await cxWho();
  const roles = cxRoles();
  const [props, disputes, docs, status, org] = await Promise.all([
    api("/api/concepts/proposals"), api("/api/concepts/disputes?status=open"),
    api("/api/concepts/documents"), api("/api/concepts/status"), api("/api/concepts/org")]);
  const labelOf = Object.fromEntries((org.concepts || []).map((c) => [c.slug, c.label]));
  const content = document.getElementById("content");
  const canReview = roles.has("reviewer");
  const propHTML = (props.proposals || []).map((p) => {
    const e = p.entry, cur = p.current;
    const diff = (k, lab) => {
      const a = cur ? JSON.stringify(cur[k] || "") : "", b = JSON.stringify(e[k] || "");
      if (a === b) return "";
      if (!cur && cxFmtVal(e[k]) === "（空）") return "";   // 新建提案不列空字段
      return `<div class="cx-ev"><span class="cx-lbl">${lab}</span>${cur ? `<s>${escapeHTML(cxFmtVal(cur[k]))}</s> → ` : ""}${escapeHTML(cxFmtVal(e[k]))}</div>`;
    };
    return `<div class="cx-item">
      <div class="cx-item-h"><b>${escapeHTML(e.pref_label)}</b> <span class="tag-agg">${{ create: "新建", update: "修改", deprecate: "废弃" }[p.action]}</span>
        ${p.stale ? '<span class="tag-agg">基于旧版本</span>' : ""}
        <span class="cx-right">${escapeHTML(p.proposed_by)} · ${fmt.datetime(p.created_at)}</span></div>
      ${p.reason ? `<div class="cx-ev"><span class="cx-lbl">理由</span>${escapeHTML(p.reason)}</div>` : ""}
      ${diff("pref_label", "名称")}${diff("definition", "定义")}${diff("alt_labels", "同义叫法")}${diff("renderings", "各职能说法")}${diff("misconceptions", "常见误解")}${diff("owner", "负责人")}${p.action === "update" ? diff("status", "状态") : ""}
      ${canReview ? `<div class="tk-gen" style="margin-top:6px"><button class="btn-primary cx-appr" data-id="${p.id}">批准</button>
        <input class="filter cx-note-in" type="text" placeholder="驳回理由（驳回必填）"><button class="btn-ghost cx-rjct" data-id="${p.id}">驳回</button></div>` : ""}
    </div>`;
  }).join("");
  const dspHTML = (disputes.disputes || []).map((x) => `<div class="cx-item">
      <div class="cx-item-h"><b>${escapeHTML(labelOf[x.slug] || x.slug)}</b><span class="cx-right">${escapeHTML(x.raised_by)} · ${fmt.datetime(x.created_at)}</span></div>
      <div class="cx-ev">${escapeHTML(x.body)}</div>
      <div class="tk-gen" style="margin-top:6px"><input class="filter cx-grow cx-reply" type="text" placeholder="回复（必填）">
        <button class="btn-ghost cx-acc" data-id="${x.id}">采纳</button><button class="btn-ghost cx-dny" data-id="${x.id}">驳回</button></div></div>`).join("");
  const kinds = docs.kinds || {};
  const st = status.last_stats || {};

  content.innerHTML = `
    <div class="grid c2">
      <div class="panel"><div class="panel-head"><span class="panel-title">待评审提案</span>
        <span class="panel-note">${canReview ? "评审不能批准自己的提案" : "你没有评审权限，只能查看"}</span></div>
        ${propHTML || cxEmpty("暂无待评审提案")}</div>
      <div class="panel"><div class="panel-head"><span class="panel-title">待处理质疑</span><span class="panel-note">概念负责人或评审处理，必须回复</span></div>
        ${dspHTML || cxEmpty("暂无待处理质疑")}</div>
    </div>
    <div class="grid c2">
      <div class="panel"><div class="panel-head"><span class="panel-title">术语库导入 / 导出</span></div>
        <p class="tk-hint">导入客户端「术语库」页签导出的 JSON。默认逐条生成提案进入评审；管理员可勾选直接入库（冷启动用）。</p>
        <div class="tk-gen" style="flex-wrap:wrap"><input type="file" id="cx-tb-file" accept=".json" class="filter">
          ${roles.has("admin") || roles.has("steward") ? '<label class="panel-note"><input type="checkbox" id="cx-tb-apply"> 直接入库</label>' : ""}
          <button class="btn-primary" id="cx-tb-import">导入</button>
          <button class="btn-ghost" id="cx-tb-export">导出团队术语库</button></div>
        <div id="cx-tb-msg" class="tk-hint"></div></div>
      <div class="panel"><div class="panel-head"><span class="panel-title">补充资料</span>
        <span class="panel-note">术语表 / Wiki / 需求文档 / 协作聊天导出，作为定义与用法的旁证</span></div>
        <div class="cx-fgrid">
          <label>类型<select id="cx-doc-kind" class="filter">${Object.entries(kinds).map(([k, v]) => `<option value="${k}">${escapeHTML(v)}</option>`).join("")}</select></label>
          <label>标题<input id="cx-doc-title" class="filter" type="text"></label>
          <label>可见性<select id="cx-doc-vis" class="filter"><option value="public">团队可见（可作证据展示）</option><option value="private">仅自己</option></select></label>
          <label>文件<input type="file" id="cx-doc-file" accept=".md,.txt,.csv" class="filter"></label>
        </div>
        <textarea id="cx-doc-text" class="filter cx-textarea" rows="6" placeholder="或直接粘贴正文"></textarea>
        <div class="tk-gen" style="margin-top:6px"><button class="btn-primary" id="cx-doc-add">上传</button><span id="cx-doc-msg"></span></div>
        ${(docs.documents || []).length ? `<table class="tbl" style="margin-top:8px"><tbody>${docs.documents.map((x) =>
          `<tr><td>${escapeHTML(x.title)}</td><td>${escapeHTML(kinds[x.kind] || x.kind)}</td><td class="num">${fmt.int(x.chars)} 字</td>
          <td>${x.visibility === "public" ? "团队可见" : "仅自己"}</td><td class="num">${fmt.date(x.uploaded_at)}</td>
          <td><button class="btn-ghost cx-doc-del" data-id="${x.id}">删除</button></td></tr>`).join("")}</tbody></table>` : ""}</div>
    </div>
    ${roles.has("steward") ? `<div class="grid c2">
      <div class="panel" id="cx-profiles-panel"><div class="panel-head"><span class="panel-title">成员职能</span><span class="panel-note">数据管理员</span></div>${cxEmpty("加载中…")}</div>
      <div class="panel"><div class="panel-head"><span class="panel-title">分析任务</span><span class="panel-note">数据管理员</span></div>
        <div class="cx-kv"><div><span>上次重建</span><b>${status.last_rebuild_at ? fmt.datetime(status.last_rebuild_at) : "从未"}</b></div>
          <div><span>状态</span><b>${status.running ? "运行中" : "空闲"}</b></div>
          <div><span>用法</span><b>${st.usages != null ? st.usages : "—"}</b></div><div><span>误解事件</span><b>${st.misreads != null ? st.misreads : "—"}</b></div></div>
        ${status.last_error ? `<div class="hi hi-bad cx-note">${escapeHTML(status.last_error)}</div>` : ""}
        <div class="tk-gen" style="margin-top:8px"><button class="btn-ghost" id="cx-rb">增量重建</button><button class="btn-ghost" id="cx-rb-full">全量重建</button><span id="cx-rb-msg"></span></div></div>
    </div>` : ""}
    ${roles.has("admin") ? `<div class="panel" id="cx-roles-panel"><div class="panel-head"><span class="panel-title">角色分配</span><span class="panel-note">系统管理员 · 任何角色都不能查看他人的个人视图</span></div>${cxEmpty("加载中…")}</div>` : ""}`;

  const $ = (id) => document.getElementById(id);
  cxBind(content, ".cx-appr", "click", async (el) => { try { await cxPost("decide", { id: +el.dataset.id, approve: true }); route(); } catch (e) { alert(e.message); } });
  cxBind(content, ".cx-rjct", "click", async (el) => {
    const note = el.parentElement.querySelector(".cx-note-in").value.trim();
    try { await cxPost("decide", { id: +el.dataset.id, approve: false, note }); route(); } catch (e) { alert(e.message); }
  });
  const resolve = (accept) => async (el) => {
    const reply = el.parentElement.querySelector(".cx-reply").value.trim();
    try { await cxPost("dispute/resolve", { id: +el.dataset.id, accept, reply }); route(); } catch (e) { alert(e.message); }
  };
  cxBind(content, ".cx-acc", "click", resolve(true));
  cxBind(content, ".cx-dny", "click", resolve(false));
  $("cx-tb-import").addEventListener("click", async () => {
    const f = $("cx-tb-file").files[0];
    if (!f) { cxFlash($("cx-tb-msg"), "请选择文件", true); return; }
    try {
      const r = await cxPost("termbase/import", { termbase: JSON.parse(await f.text()), apply: !!($("cx-tb-apply") && $("cx-tb-apply").checked) });
      $("cx-tb-msg").innerHTML = `新提案 ${r.proposed} · 直接入库 ${r.applied} · 未变化 ${r.unchanged}` +
        (r.errors.length ? `<br><span style="color:var(--bad)">${r.errors.map(escapeHTML).join("<br>")}</span>` : "");
    } catch (e) { cxFlash($("cx-tb-msg"), e.message, true); }
  });
  $("cx-tb-export").addEventListener("click", async () => {
    const r = await api("/api/concepts/termbase/export");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }));
    a.download = "termbase.json"; a.click(); URL.revokeObjectURL(a.href);
  });
  $("cx-doc-file").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    $("cx-doc-text").value = await f.text();
    if (!$("cx-doc-title").value) $("cx-doc-title").value = f.name.replace(/\.[^.]+$/, "");
  });
  $("cx-doc-add").addEventListener("click", async () => {
    try {
      await cxPost("documents", { kind: $("cx-doc-kind").value, title: $("cx-doc-title").value.trim(),
        visibility: $("cx-doc-vis").value, text: $("cx-doc-text").value });
      route();
    } catch (e) { cxFlash($("cx-doc-msg"), e.message, true); }
  });
  cxBind(content, ".cx-doc-del", "click", async (el) => {
    if (!confirm("删除该资料？")) return;
    try { await cxPost("documents/delete", { id: +el.dataset.id }); route(); } catch (e) { alert(e.message); }
  });
  if ($("cx-rb")) {
    const rb = (full) => async () => {
      cxFlash($("cx-rb-msg"), "运行中…");
      try { const r = await cxPost("rebuild", { full }); cxFlash($("cx-rb-msg"), `完成：处理 ${r.stats.processed} · 跳过 ${r.stats.skipped} · 清除 ${r.stats.purged}`); }
      catch (e) { cxFlash($("cx-rb-msg"), e.message, true); }
    };
    $("cx-rb").addEventListener("click", rb(false));
    $("cx-rb-full").addEventListener("click", rb(true));
    cxProfilesPanel();
  }
  if ($("cx-roles-panel")) cxRolesPanel();
}

function cxFmtVal(v) {
  if (v == null || v === "") return "（空）";
  if (typeof v === "string" && CX_STATUS[v]) return CX_STATUS[v];
  if (Array.isArray(v)) return v.map((x) => typeof x === "object" ? `${x.wrong || ""}${x.actual ? "→" + x.actual : ""}` : x).join("、") || "（空）";
  if (typeof v === "object") return Object.entries(v).map(([k, x]) => `${(CX.who.functions || {})[k] || k}：${x}`).join("；") || "（空）";
  return String(v);
}

async function cxProfilesPanel() {
  const el = document.getElementById("cx-profiles-panel");
  const d = await api("/api/concepts/profiles");
  const byP = Object.fromEntries(d.profiles.map((p) => [p.person, p]));
  const persons = Array.from(new Set([...d.known_persons, ...Object.keys(byP)]));
  const fnOpts = (v) => `<option value="">未设置</option>` + Object.entries(d.functions).map(([k, n]) =>
    `<option value="${k}" ${v === k ? "selected" : ""}>${escapeHTML(n)}</option>`).join("");
  el.innerHTML = `<div class="panel-head"><span class="panel-title">成员职能</span><span class="panel-note">数据管理员 · 系统推断的职能不会用于个人结论</span></div>
    <p class="tk-hint">批量导入 CSV：每行「成员,职能,小组,登录名」，职能可写中文（策划/程序/美术/交互/音频/QA）。</p>
    <div class="tk-gen"><input type="file" id="cx-pf-file" accept=".csv,.txt" class="filter"><button class="btn-ghost" id="cx-pf-import">导入</button><span id="cx-pf-msg"></span></div>
    <table class="tbl" style="margin-top:8px"><thead><tr><th>成员</th><th>职能</th><th>小组</th><th>来源</th><th></th></tr></thead><tbody>${persons.map((p) => {
      const r = byP[p] || {};
      return `<tr data-p="${escapeHTML(p)}"><td>${escapeHTML(p)}</td><td><select class="filter cx-pf-fn">${fnOpts(r.function)}</select></td>
        <td><input class="filter cx-pf-team" type="text" value="${escapeHTML(r.team || "")}"></td><td>${escapeHTML(CX_SRC[r.function_source] || "—")}</td>
        <td><button class="btn-ghost cx-pf-save">保存</button></td></tr>`;
    }).join("")}</tbody></table>`;
  cxBind(el, ".cx-pf-save", "click", async (b) => {
    const tr = b.closest("tr");
    try { await cxPost("profiles/set", { person: tr.dataset.p, function: tr.querySelector(".cx-pf-fn").value, team: tr.querySelector(".cx-pf-team").value.trim() }); b.textContent = "已保存"; }
    catch (e) { alert(e.message); }
  });
  document.getElementById("cx-pf-import").addEventListener("click", async () => {
    const f = document.getElementById("cx-pf-file").files[0]; if (!f) return;
    const r = await cxPost("profiles/import", { csv: await f.text() });
    cxFlash(document.getElementById("cx-pf-msg"), `导入 ${r.imported} 行` + (r.errors.length ? `，${r.errors.length} 行有误：${r.errors.join("；")}` : ""), !!r.errors.length);
    if (!r.errors.length) cxProfilesPanel();
  });
}

async function cxRolesPanel() {
  const el = document.getElementById("cx-roles-panel");
  const d = await api("/api/concepts/roles");
  const assignable = Object.entries(d.roles).filter(([k]) => k !== "member");
  el.innerHTML = `<div class="panel-head"><span class="panel-title">角色分配</span><span class="panel-note">系统管理员 · 任何角色都不能查看他人的个人视图</span></div>
    <table class="tbl"><thead><tr><th>账号</th>${assignable.map(([, n]) => `<th>${escapeHTML(n)}</th>`).join("")}<th></th></tr></thead><tbody>${d.users.map((u) =>
      `<tr data-u="${escapeHTML(u.username)}"><td>${escapeHTML(u.display)}</td>${assignable.map(([k]) =>
        `<td style="text-align:center"><input type="checkbox" class="cx-rl" value="${k}" ${u.roles.includes(k) ? "checked" : ""}></td>`).join("")}
        <td><button class="btn-ghost cx-rl-save">保存</button></td></tr>`).join("")}</tbody></table>`;
  cxBind(el, ".cx-rl-save", "click", async (b) => {
    const tr = b.closest("tr");
    const roles = Array.from(tr.querySelectorAll(".cx-rl:checked")).map((x) => x.value);
    try { await cxPost("roles", { username: tr.dataset.u, roles }); b.textContent = "已保存"; CX.who = null; }
    catch (e) { alert(e.message); }
  });
}
