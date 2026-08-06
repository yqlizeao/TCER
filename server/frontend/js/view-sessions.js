/* 页面：会话明细 */
// ------------------------- 页面：会话明细 -------------------------
async function renderSessions() {
  const d = await api("/api/sessions?" + windowParams());
  const content = document.getElementById("content");
  const list = d.sessions.map((s) => `
    <div class="sess-item" data-id="${s.id}">
      <div class="sess-t">${s.title}${s.aggregate_only ? ' <span class="tag-agg">仅聚合</span>' : ""}</div>
      <div class="sess-m"><span>${s.person}</span><span>${s.project}</span><span>${fmt.money(s.cost_usd)}</span></div>
    </div>`).join("");

  content.innerHTML = `
    <div class="sess-layout">
      <div class="sess-list">${list || '<div class="empty">无会话</div>'}</div>
      <div class="panel" id="sess-detail"><div class="empty">← 选择左侧会话查看明细</div></div>
    </div>`;

  content.querySelectorAll(".sess-item").forEach((el) => {
    el.addEventListener("click", async () => {
      content.querySelectorAll(".sess-item").forEach((x) => x.classList.remove("active"));
      el.classList.add("active");
      try {
        const detail = await api("/api/session?id=" + el.dataset.id);
        renderSessionDetail(detail);
      } catch (e) {
        document.getElementById("sess-detail").innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
      }
    });
  });
}

function kv(k, v) { return `<div class="sd-kv"><span class="k">${k}</span><span class="v">${v}</span></div>`; }

function renderSessionDetail(d) {
  S.sd = d;
  if (!S.sdMode) S.sdMode = "view";
  paintSessionDetail();
}

function paintSessionDetail() {
  const d = S.sd;
  const raw = d.raw || {};
  const el = document.getElementById("sess-detail");

  // 三栏概览：效率 / 产出 / 成本
  const cols = `
    <div class="sd-cols">
      <div class="sd-col"><div class="sd-col-h">效率</div>
        ${kv("TCER", fmt.f2(raw.tcer))}
        ${kv("综合分", raw.score == null ? "—" : fmt.f2(raw.score) + (raw.tier ? " " + tierChip(raw.tier) : ""))}
        ${kv("返工率", fmt.pct(raw.churn_ratio))}
      </div>
      <div class="sd-col"><div class="sd-col-h">产出</div>
        ${kv("净增行", fmt.int(raw.net_loc))}
        ${kv("采纳率", raw.churn_ratio == null ? "—" : fmt.pct(1 - raw.churn_ratio))}
        ${kv("先读后写", fmt.pct(raw.read_before_write))}
      </div>
      <div class="sd-col"><div class="sd-col-h">成本</div>
        ${kv("模型", `<span class="model-pill">${d.model || "—"}</span>`)}
        ${kv("成本", fmt.money(raw.cost_usd))}
        ${kv("Token", fmt.tokens(raw.total_tokens))}
      </div>
    </div>`;

  const conv = raw.conversation || raw.transcript;
  const hasConv = Array.isArray(conv) && conv.length;
  const body = S.sdMode === "json"
    ? `<pre class="raw">${escapeHTML(JSON.stringify(raw, null, 2))}</pre>`
    : (hasConv ? transcriptHTML(conv)
        : `<div class="empty">该会话未附带逐回合明细（仅聚合上传）</div>`);

  el.innerHTML = `
    <div class="sd-head">
      <div><div class="sd-title">${d.title || d.session_id || "会话"}${d.aggregate_only ? ' <span class="tag-agg">仅聚合</span>' : ""}</div>
        <div class="sd-sub">${d.project || "—"} · ${d.person || "—"}</div></div>
      <div class="mode-toggle">
        <button data-mode="view" class="${S.sdMode === "view" ? "active" : ""}">视图</button>
        <button data-mode="json" class="${S.sdMode === "json" ? "active" : ""}">JSON</button>
      </div>
    </div>
    ${cols}
    ${body}`;

  el.querySelectorAll(".mode-toggle button").forEach((b) =>
    b.addEventListener("click", () => { S.sdMode = b.dataset.mode; paintSessionDetail(); }));
}

function escapeHTML(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// tool → SVG 图标（不同工具不同 icon）
const TOOL_ICON = {
  Read: '<path d="M4 4h11l5 5v11H4z"/><path d="M14 4v5h5"/>',
  Edit: '<path d="M4 20h16"/><path d="M14 4l6 6-9 9H5v-6z"/>',
  Write: '<path d="M12 4v16"/><path d="M4 8h16"/>',
  Bash: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3"/><path d="M13 15h4"/>',
  Grep: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4-4"/>',
  _default: '<circle cx="12" cy="12" r="8"/><path d="M12 8v8"/>',
};
function toolSVG(name) {
  const p = TOOL_ICON[name] || TOOL_ICON._default;
  return `<svg viewBox="0 0 24 24">${p}</svg>`;
}
const ROLE_SVG = {
  user: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>',
  assistant: '<svg viewBox="0 0 24 24"><rect x="4" y="6" width="16" height="12" rx="3"/><path d="M9 11h.01M15 11h.01"/><path d="M12 3v3"/></svg>',
  tool: '<svg viewBox="0 0 24 24"><path d="M14 6a4 4 0 00-5 5l-6 6 2 2 6-6a4 4 0 005-5l-3 3-2-2z"/></svg>',
};

function transcriptHTML(conv) {
  const turns = conv.map((t) => {
    const role = t.role === "user" ? "user" : (t.role === "tool" || t.role === "toolResult") ? "tool" : "assistant";
    const blocks = (t.blocks || []).map(blockHTML).join("");
    return `<div class="turn ${role}">
      <div class="turn-ic ic-${role}">${ROLE_SVG[role]}</div>
      <div class="turn-body">${blocks}</div>
    </div>`;
  }).join("");
  return `<div class="tr">${turns}</div>`;
}

function blockHTML(b) {
  if (b.type === "text")
    return `<div class="blk blk-text">${escapeHTML(b.text || "")}</div>`;
  if (b.type === "thinking")
    return `<div class="blk blk-thinking">💭 ${escapeHTML(b.text || "")}</div>`;
  if (b.type === "tool_call") {
    const args = b.args
      ? Object.entries(b.args).map(([k, v]) => `${k}: ${escapeHTML(String(v))}`).join(" · ")
      : "";
    return `<div class="blk blk-tool">
      <div class="blk-tool-head"><span class="blk-tool-ic">${toolSVG(b.name)}</span>
        <span class="blk-tool-name">${b.name || "工具"}</span></div>
      ${args ? `<div class="blk-tool-arg">${args}</div>` : ""}</div>`;
  }
  if (b.type === "tool_result") {
    const err = b.ok === false;
    return `<div class="blk blk-result ${err ? "err" : ""}">
      <div class="blk-result-head">${toolSVG(b.name)} ${b.name || "结果"} ${err ? "· 失败" : "· 成功"}</div>
      <pre>${escapeHTML(b.text || "")}</pre></div>`;
  }
  return `<div class="blk blk-text">${escapeHTML(JSON.stringify(b))}</div>`;
}

