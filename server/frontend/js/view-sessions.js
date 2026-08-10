/* 页面：会话明细 */
// ------------------------- 页面：会话明细 -------------------------
// 会话列表本地偏好：排序/分组模式 + 各项目组的收起状态（浏览器记忆）。
const SESS_PREF = {
  get mode() { return localStorage.getItem("tcer_sess_mode") || "flat"; },
  set mode(v) { localStorage.setItem("tcer_sess_mode", v); },
  _collapsed() {
    try { return new Set(JSON.parse(localStorage.getItem("tcer_sess_collapsed") || "[]")); }
    catch (e) { return new Set(); }
  },
  isCollapsed(g) { return this._collapsed().has(g); },
  toggle(g) {
    const s = this._collapsed();
    if (s.has(g)) s.delete(g); else s.add(g);
    localStorage.setItem("tcer_sess_collapsed", JSON.stringify([...s]));
  },
};

// 私有/公开标签（会话列表副标题 + 详情副标题共用）。
function visTag(v) {
  return v === "public"
    ? '<span class="vis-tag vis-public">公开</span>'
    : '<span class="vis-tag vis-private">私有</span>';
}

function sessItemHTML(s) {
  return `
    <div class="sess-item" data-id="${s.id}">
      <div class="sess-t">${escapeHTML(s.title)}${s.aggregate_only ? ' <span class="tag-agg">仅聚合</span>' : ""}</div>
      <div class="sess-m"><span>${escapeHTML(s.person)}</span><span>${escapeHTML(s.project)}</span><span>${fmt.money(s.cost_usd)}</span>${visTag(s.visibility)}</div>
    </div>`;
}

async function renderSessions() {
  const d = await api("/api/sessions?" + windowParams());
  S.sessions = d.sessions;
  const content = document.getElementById("content");
  content.innerHTML = `
    <div class="sess-layout">
      <div class="sess-col">
        <div class="sess-toolbar">
          <div class="mode-toggle" id="sess-mode">
            <button data-mode="flat" class="${SESS_PREF.mode === "flat" ? "active" : ""}">按时间</button>
            <button data-mode="group" class="${SESS_PREF.mode === "group" ? "active" : ""}">按项目</button>
          </div>
        </div>
        <div class="sess-list" id="sess-list"></div>
      </div>
      <div class="panel" id="sess-detail"><div class="empty">← 选择左侧会话查看明细</div></div>
    </div>`;
  paintSessionList();
  document.getElementById("sess-mode").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    SESS_PREF.mode = b.dataset.mode;
    renderSessions();
  });
}

function paintSessionList() {
  const listEl = document.getElementById("sess-list");
  if (!listEl) return;
  const sessions = S.sessions || [];
  if (!sessions.length) { listEl.innerHTML = '<div class="empty">无会话</div>'; return; }

  if (SESS_PREF.mode === "flat") {
    listEl.innerHTML = sessions.map(sessItemHTML).join("");
  } else {
    // 按（聚合后的）项目分组，保持后端已有的时间倒序。
    const groups = new Map();
    for (const s of sessions) {
      const g = s.project || "未标注";
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(s);
    }
    listEl.innerHTML = [...groups.entries()].map(([g, items]) => {
      const collapsed = SESS_PREF.isCollapsed(g);
      const body = collapsed ? ""
        : `<div class="sess-group-body">${items.map(sessItemHTML).join("")}</div>`;
      return `
        <div class="sess-group">
          <div class="sess-group-h" data-group="${escapeHTML(g)}">
            <span class="sess-group-caret">${collapsed ? "▸" : "▾"}</span>
            <span class="sess-group-name">${escapeHTML(g)}</span>
            <span class="sess-group-n">${items.length}</span>
          </div>
          ${body}
        </div>`;
    }).join("");
  }
  bindSessListEvents();

  // 保持当前打开的会话/分组高亮。
  if (S.sd && S.sd.id != null) {
    const act = listEl.querySelector(`.sess-item[data-id="${S.sd.id}"]`);
    if (act) act.classList.add("active");
  }
}

function bindSessListEvents() {
  const listEl = document.getElementById("sess-list");
  listEl.querySelectorAll(".sess-item").forEach((el) => {
    el.addEventListener("click", async () => {
      listEl.querySelectorAll(".sess-item").forEach((x) => x.classList.remove("active"));
      listEl.querySelectorAll(".sess-group-h").forEach((x) => x.classList.remove("active"));
      el.classList.add("active");
      try {
        const detail = await api("/api/session?id=" + el.dataset.id);
        renderSessionDetail(detail);
      } catch (e) {
        document.getElementById("sess-detail").innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
      }
    });
  });
  // 分组标题：caret 收起/展开（记忆到浏览器）；标题其余区域点开显示分组汇总。
  listEl.querySelectorAll(".sess-group-h").forEach((h) => {
    const g = h.dataset.group;
    h.querySelector(".sess-group-caret").addEventListener("click", (e) => {
      e.stopPropagation();
      SESS_PREF.toggle(g);
      paintSessionList();
    });
    h.addEventListener("click", () => {
      listEl.querySelectorAll(".sess-item").forEach((x) => x.classList.remove("active"));
      listEl.querySelectorAll(".sess-group-h").forEach((x) => x.classList.remove("active"));
      h.classList.add("active");
      S.sd = null;
      showGroupSummary(g);
    });
  });
}

// 分组汇总：右侧显示该项目的汇总指标 + 批量私有/公开（带二次确认）。
async function showGroupSummary(project) {
  const el = document.getElementById("sess-detail");
  el.innerHTML = '<div class="empty">加载汇总…</div>';
  let g;
  try {
    g = await api("/api/group-summary?project=" + encodeURIComponent(project) +
                  "&" + windowParams());
  } catch (e) {
    el.innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
    return;
  }
  S.group = g;
  const cols = `
    <div class="sd-cols">
      <div class="sd-col"><div class="sd-col-h">规模</div>
        ${kv("会话数", fmt.int(g.sessions))}
        ${kv("净增行", fmt.int(g.net_loc))}
        ${kv("Token", fmt.tokens(g.total_tokens))}
      </div>
      <div class="sd-col"><div class="sd-col-h">效率</div>
        ${kv("TCER", fmt.f2(g.tcer))}
        ${kv("综合分", g.score == null ? "—" : fmt.f2(g.score) + (g.tier ? " " + tierChip(g.tier) : ""))}
        ${kv("返工率", fmt.pct(g.churn_ratio))}
      </div>
      <div class="sd-col"><div class="sd-col-h">成本</div>
        ${kv("成本", fmt.money(g.cost_usd))}
        ${kv("CPE", g.cpe == null ? "—" : fmt.f2(g.cpe))}
      </div>
    </div>`;
  const owned = g.owned_count || 0;
  const bulk = owned
    ? `<div class="grp-bulk">
         <div class="grp-bulk-info">你在本组拥有 <b>${owned}</b> 个会话（公开 ${g.owned_public || 0} · 私有 ${g.owned_private || 0}）。批量设置会<b>覆盖</b>这些会话原有的单独私有/公开设置。</div>
         <div class="grp-bulk-btns">
           <button class="btn-ghost" data-vis="public">全部设为公开</button>
           <button class="btn-ghost" data-vis="private">全部设为私有</button>
         </div>
       </div>`
    : `<div class="grp-bulk"><div class="grp-bulk-info">你在本组没有可批量设置的会话（只能设置自己上传的会话）。</div></div>`;
  el.innerHTML = `
    <div class="sd-head">
      <div><div class="sd-title">${escapeHTML(project)}</div>
        <div class="sd-sub">项目汇总 · ${g.sessions || 0} 个可见会话</div></div>
    </div>
    ${cols}
    ${bulk}`;
  el.querySelectorAll(".grp-bulk-btns button").forEach((b) =>
    b.addEventListener("click", () => bulkSetVisibility(project, b.dataset.vis)));
}

async function bulkSetVisibility(project, vis) {
  const label = vis === "public" ? "公开" : "私有";
  if (!confirm(`确定将本项目组中你拥有的所有会话设为「${label}」吗？\n\n此操作会覆盖这些会话原有的单独私有/公开设置。`)) return;
  const payload = { project, visibility: vis };
  if (S.persons.length) payload.persons = S.persons;
  if (S.days) {
    const end = Math.floor(Date.now() / 1000);
    payload.start = end - S.days * 86400;
    payload.end = end;
  }
  try {
    const r = await api("/api/visibility", { method: "POST", body: JSON.stringify(payload) });
    await renderSessions();
    showGroupSummary(project);
    alert(`已将 ${r.updated} 个会话设为「${label}」。`);
  } catch (e) {
    alert("设置失败：" + e.message);
  }
}

async function setSessionVisibility(id, vis) {
  if (!S.sd || vis === S.sd.visibility) return;
  try {
    await api("/api/visibility", { method: "POST", body: JSON.stringify({ id, visibility: vis }) });
    S.sd.visibility = vis;
    paintSessionDetail();
    const it = (S.sessions || []).find((s) => s.id === id);
    if (it) it.visibility = vis;
    paintSessionList();
  } catch (e) {
    alert("设置失败：" + e.message);
  }
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

  // 右上角：owner 可切私有/公开；非 owner 只读显示当前可见性标签。
  const vis = d.visibility || "private";
  const visCtl = d.is_owner
    ? `<div class="vis-toggle" id="vis-toggle" title="设置该会话是否对他人可见">
         <button data-vis="private" class="${vis === "private" ? "active" : ""}">私有</button>
         <button data-vis="public" class="${vis === "public" ? "active" : ""}">公开</button>
       </div>`
    : visTag(vis);

  el.innerHTML = `
    <div class="sd-head">
      <div><div class="sd-title">${escapeHTML(d.title || d.session_id || "会话")}${d.aggregate_only ? ' <span class="tag-agg">仅聚合</span>' : ""}</div>
        <div class="sd-sub">${escapeHTML(d.project || "—")} · ${escapeHTML(d.person || "—")} ${visTag(vis)}</div></div>
      <div class="sd-head-r">
        ${visCtl}
        <div class="mode-toggle">
          <button data-mode="view" class="${S.sdMode === "view" ? "active" : ""}">视图</button>
          <button data-mode="json" class="${S.sdMode === "json" ? "active" : ""}">JSON</button>
        </div>
      </div>
    </div>
    ${cols}
    ${body}`;

  el.querySelectorAll(".mode-toggle button").forEach((b) =>
    b.addEventListener("click", () => { S.sdMode = b.dataset.mode; paintSessionDetail(); }));
  const vt = el.querySelector("#vis-toggle");
  if (vt) vt.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => setSessionVisibility(d.id, b.dataset.vis)));
}

// escapeHTML 由 core.js 提供（全局）

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

// 归一角色：user / tool（工具结果） / assistant（含 thinking、tool_use）。
function blockRole(b) {
  if (b.role === "user") return "user";
  if (b.role === "tool" || b.type === "tool_result") return "tool";
  return "assistant";
}

// 后端 read_conversation 产出的平 block 列表（每条 {role,type,...}），不是
// 按回合分好的 turn。这里把相邻同角色的 block 合并成一个气泡，复用现有 turn 样式。
function transcriptHTML(conv) {
  const groups = [];
  for (const b of conv) {
    const role = blockRole(b);
    const last = groups[groups.length - 1];
    if (last && last.role === role) last.blocks.push(b);
    else groups.push({ role, blocks: [b] });
  }
  const turns = groups.map((g) => {
    const blocks = g.blocks.map(blockHTML).join("");
    return `<div class="turn ${g.role}">
      <div class="turn-ic ic-${g.role}">${ROLE_SVG[g.role]}</div>
      <div class="turn-body">${blocks}</div>
    </div>`;
  }).join("");
  return `<div class="tr">${turns || '<div class="empty">该会话无可展示的对话内容</div>'}</div>`;
}

// 工具入参对象 → 紧凑单行摘要（长值截断，避免 Bash/Write 的大段内容撑爆气泡）。
function toolArgs(input) {
  if (input == null) return "";
  if (typeof input !== "object")
    return escapeHTML(String(input).slice(0, 300));
  return Object.entries(input).map(([k, v]) => {
    let s = typeof v === "object" ? JSON.stringify(v) : String(v);
    if (s.length > 200) s = s.slice(0, 200) + "…";
    return `${escapeHTML(k)}: ${escapeHTML(s)}`;
  }).join(" · ");
}

function blockHTML(b) {
  if (b.type === "text")
    return `<div class="blk blk-text">${escapeHTML(b.text || "")}</div>`;
  if (b.type === "thinking")
    return `<div class="blk blk-thinking">💭 ${escapeHTML(b.text || "")}</div>`;
  if (b.type === "tool_use") {
    const args = toolArgs(b.input);
    return `<div class="blk blk-tool">
      <div class="blk-tool-head"><span class="blk-tool-ic">${toolSVG(b.name)}</span>
        <span class="blk-tool-name">${escapeHTML(b.name || "工具")}</span></div>
      ${args ? `<div class="blk-tool-arg">${args}</div>` : ""}</div>`;
  }
  if (b.type === "tool_result") {
    const err = b.is_error === true;
    return `<div class="blk blk-result ${err ? "err" : ""}">
      <div class="blk-result-head">${toolSVG(b.name)} 工具结果 ${err ? "· 失败" : "· 成功"}</div>
      <pre>${escapeHTML(b.text || "")}</pre></div>`;
  }
  return `<div class="blk blk-text">${escapeHTML(JSON.stringify(b))}</div>`;
}

