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

const SOURCE_INFO = {
  claude: { name: "Claude Code", icon: "assets/claude.png" },
  codex: { name: "Codex", icon: "assets/codex.png" },
  grok: { name: "Grok", icon: "assets/grok.png" },
  omp: { name: "Oh My Pi", icon: "assets/omp.png" },
  pi: { name: "Pi", icon: "assets/pi.png" },
  opencode: { name: "OpenCode", icon: "assets/opencode.png" },
};
function getSourceInfo(source) {
  const s = String(source || "claude").toLowerCase().trim();
  return SOURCE_INFO[s] || { name: s || "Claude Code", icon: "assets/claude.png" };
}
function sourceBadgeHTML(source, showName = true) {
  const info = getSourceInfo(source);
  return `<span class="source-badge" title="Agent 数据源：${escapeHTML(info.name)}"><img class="source-icon" src="${info.icon}" alt="${escapeHTML(info.name)}" />${showName ? `<span class="source-name">${escapeHTML(info.name)}</span>` : ""}</span>`;
}
function sourceIconHTML(source, cls = "source-icon-inline") {
  const info = getSourceInfo(source);
  return `<img class="${cls}" src="${info.icon}" alt="${escapeHTML(info.name)}" title="Agent 数据源：${escapeHTML(info.name)}" />`;
}

function sessItemHTML(s) {
  const timeBadge = s.ts
    ? `<span class="sess-time" title="${fmt.datetime(s.ts)}">${fmt.time(s.ts)}</span>`
    : "";
  return `
    <div class="sess-item" data-id="${s.id}">
      <div class="sess-t">${sourceIconHTML(s.source, "source-icon-xs")}<span class="sess-title-text">${escapeHTML(s.title)}</span>${s.aggregate_only ? ' <span class="tag-agg">仅聚合</span>' : ""}</div>
      <div class="sess-m"><span>${escapeHTML(s.person)}</span><span>${escapeHTML(s.project)}</span>${timeBadge}<span>${fmt.money(s.cost_usd)}</span>${visTag(s.visibility)}</div>
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
        : (d.aggregate_only
            ? `<div class="empty">该记录为项目聚合数据（未附带逐会话明细）</div>`
            : `<div class="empty">该会话未附带逐回合对话内容</div>`));

  // 右上角：owner 可切私有/公开；非 owner 只读显示当前可见性标签。
  const vis = d.visibility || "private";
  const visCtl = d.is_owner
    ? `<div class="vis-toggle" id="vis-toggle" title="设置该会话是否对他人可见">
         <button data-vis="private" class="${vis === "private" ? "active" : ""}">私有</button>
         <button data-vis="public" class="${vis === "public" ? "active" : ""}">公开</button>
       </div>`
    : visTag(vis);

  const timeSub = d.ts
    ? ` · <span class="sd-time" title="会话时间">${fmt.datetime(d.ts)}</span>`
    : "";

  const srcBadge = sourceBadgeHTML(d.source, true);
  const srcIcon = sourceIconHTML(d.source, "source-icon-main");

  el.innerHTML = `
    <div class="sd-head">
      <div>
        <div class="sd-title">${srcIcon}<span class="sd-title-text">${escapeHTML(d.title || d.session_id || "会话")}</span>${d.aggregate_only ? ' <span class="tag-agg">仅聚合</span>' : ""}</div>
        <div class="sd-sub">${escapeHTML(d.project || "—")} · ${escapeHTML(d.person || "—")} · ${srcBadge}${timeSub} ${visTag(vis)}</div>
      </div>
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

  // 绑定左侧快速导航点点击与视野高亮联动
  const navDots = el.querySelectorAll(".tr-nav-dot");
  if (navDots.length) {
    navDots.forEach((dot) => {
      dot.addEventListener("click", (e) => {
        e.stopPropagation();
        const targetId = dot.dataset.target;
        const targetEl = el.querySelector("#" + targetId);
        if (targetEl) {
          navDots.forEach((d) => d.classList.remove("active"));
          dot.classList.add("active");
          targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
          targetEl.classList.add("turn-highlight");
          setTimeout(() => targetEl.classList.remove("turn-highlight"), 1200);
        }
      });
    });

    // 滚动监听：进入视野时自动切换高亮激活点
    if (typeof IntersectionObserver !== "undefined") {
      const obs = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const id = entry.target.id;
            const matchedDot = el.querySelector(`.tr-nav-dot[data-target="${id}"]`);
            if (matchedDot) {
              navDots.forEach((d) => d.classList.remove("active"));
              matchedDot.classList.add("active");
              break;
            }
          }
        }
      }, {
        root: el,
        rootMargin: "0px 0px -75% 0px",
        threshold: 0.05
      });
      el.querySelectorAll(".turn[id]").forEach((t) => obs.observe(t));
    }
  }
}

// escapeHTML 由 core.js 提供（全局）

// tool → SVG 图标（不同工具不同 icon）
const TOOL_ICON = {
  Read: '<path d="M4 4h11l5 5v11H4z"/><path d="M14 4v5h5"/>',
  Edit: '<path d="M4 20h16"/><path d="M14 4l6 6-9 9H5v-6z"/>',
  Write: '<path d="M12 4v16"/><path d="M4 8h16"/>',
  Bash: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3"/><path d="M13 15h4"/>',
  Grep: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4-4"/>',
  Glob: '<path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/>',
  TodoWrite: '<rect x="3" y="4" width="6" height="6" rx="1.5"/><path d="M5 7l1 1 2-2"/><path d="M12 7h9M12 17h9"/><rect x="3" y="14" width="6" height="6" rx="1.5"/><path d="M5 17l1 1 2-2"/>',
  Task: '<circle cx="12" cy="5" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M12 7.5v4M7.8 16l2.7-3M16.2 16l-2.7-3M10.5 11.5h3"/>',
  AskUserQuestion: '<path d="M21 11.5a8.5 8.5 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.5 8.5 0 01-3.8-.9L3 21l1.9-5.7a8.5 8.5 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.5 8.5 0 013.8-.9h.5a8.5 8.5 0 018 8v.5z"/><path d="M12 8.5a1.5 1.5 0 011.5 1.5c0 1-1.5 1.5-1.5 2"/><circle cx="12" cy="15" r=".5" fill="currentColor"/>',
  WebSearch: '<circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8M3.6 15h16.8M12 3a14 14 0 000 18M12 3a14 14 0 010 18"/>',
  LSP: '<path d="M7 8l-4 4 4 4"/><path d="M17 8l4 4-4 4"/><path d="M14 4l-4 16"/>',
  Skill: '<path d="M12 2l2.4 6.8H21l-5.5 4.3 2.1 6.9-5.6-4.2-5.6 4.2 2.1-6.9L3 8.8h6.6z"/>',
  _default: '<circle cx="12" cy="12" r="8"/><path d="M12 8v8"/>',
};
function toolSVG(name) {
  if (!name) return `<svg viewBox="0 0 24 24">${TOOL_ICON._default}</svg>`;
  const n = String(name).trim();
  if (TOOL_ICON[n]) return `<svg viewBox="0 0 24 24">${TOOL_ICON[n]}</svg>`;
  const lower = n.toLowerCase();
  let key = "_default";
  if (lower.includes("todo") || lower.includes("plan")) key = "TodoWrite";
  else if (lower.includes("glob") || lower.includes("find") || lower === "list_dir" || lower === "ls" || lower === "list") key = "Glob";
  else if (lower.includes("grep") || lower.includes("search")) key = "Grep";
  else if (lower.includes("read") || lower === "view" || lower === "view_image") key = "Read";
  else if (lower.includes("edit") || lower.includes("patch")) key = "Edit";
  else if (lower.includes("write") || lower.includes("create")) key = "Write";
  else if (lower.includes("bash") || lower.includes("terminal") || lower.includes("shell") || lower === "exec" || lower === "eval" || lower === "run") key = "Bash";
  else if (lower.includes("task") || lower.includes("subagent") || lower.includes("agent")) key = "Task";
  else if (lower.includes("ask") || lower.includes("question") || lower.includes("user_input")) key = "AskUserQuestion";
  else if (lower.includes("web") || lower.includes("fetch")) key = "WebSearch";
  else if (lower.includes("lsp")) key = "LSP";
  else if (lower.includes("skill")) key = "Skill";
  const p = TOOL_ICON[key] || TOOL_ICON._default;
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

  const navDots = [];
  const turns = groups.map((g, idx) => {
    const turnId = `turn-${idx}`;
    const blocks = g.blocks.map(blockHTML).join("");

    // 识别导航点：用户输入点 (green) 与 LLM 的 markdown 文本回复点 (blue)
    if (g.role === "user") {
      const userTxt = g.blocks
        .filter((b) => b.type === "text")
        .map((b) => b.text || "")
        .join(" ")
        .trim();
      const snippet = userTxt ? userTxt.slice(0, 80) : "用户输入";
      navDots.push({
        id: turnId,
        type: "user",
        title: `用户: ${snippet}`,
      });
    } else if (g.role === "assistant" && g.blocks.some((b) => b.type === "text")) {
      const asstTxt = g.blocks
        .filter((b) => b.type === "text")
        .map((b) => b.text || "")
        .join(" ")
        .trim();
      const snippet = asstTxt ? asstTxt.slice(0, 80) : "回复";
      navDots.push({
        id: turnId,
        type: "assistant",
        title: `助手: ${snippet}`,
      });
    }

    return `<div class="turn ${g.role}" id="${turnId}">
      <div class="turn-ic ic-${g.role}">${ROLE_SVG[g.role]}</div>
      <div class="turn-body">${blocks}</div>
    </div>`;
  }).join("");

  if (!turns) return '<div class="empty">该会话无可展示的对话内容</div>';
  if (!navDots.length) return `<div class="tr">${turns}</div>`;

  const dotsHTML = navDots.map((d, i) => `
    <div class="tr-nav-dot dot-${d.type} ${i === 0 ? "active" : ""}"
         data-target="${d.id}"
         title="${escapeHTML(d.title)}"></div>
  `).join("");

  return `
    <div class="tr-wrap">
      <div class="tr-nav" id="tr-nav" title="快速导航（绿：用户提问 · 蓝：助手回复）">
        ${dotsHTML}
      </div>
      <div class="tr">${turns}</div>
    </div>`;
}

const CODE_PARAM_KEYS = new Set([
  "path", "file_path", "filepath", "pattern", "command", "cmd", "file",
  "query", "target", "dir", "cwd", "workdir", "symbol", "regex", "selector",
  "url", "new_name"
]);

// 工具入参对象 → 紧凑摘要（跳过已呈现在标题的 i/intent；关键路径/命令采用 <code> 格式化）
function toolArgs(input) {
  if (input == null) return "";
  let obj = input;
  if (typeof obj === "string") {
    try {
      const parsed = JSON.parse(obj);
      if (parsed && typeof parsed === "object") obj = parsed;
    } catch (_) {}
  }
  if (typeof obj !== "object")
    return `<code class="blk-tool-code">${escapeHTML(String(obj).slice(0, 300))}</code>`;

  const entries = Object.entries(obj).filter(([k]) => k !== "i" && k !== "intent");
  if (!entries.length) return "";

  return entries.map(([k, v]) => {
    let s = typeof v === "object" ? JSON.stringify(v) : String(v);
    const isCode = CODE_PARAM_KEYS.has(k.toLowerCase());
    if (s.length > 240) s = s.slice(0, 240) + "…";
    const valHTML = isCode
      ? `<code class="blk-tool-code">${escapeHTML(s)}</code>`
      : escapeHTML(s);
    return `<span class="blk-arg-item"><span class="arg-k">${escapeHTML(k)}:</span> ${valHTML}</span>`;
  }).join(" · ");
}

// 规范化单条待办项
function normTodoItem(it) {
  let content = "";
  let status = "pending";
  if (typeof it === "string") {
    content = it;
  } else if (it && typeof it === "object") {
    content = it.content || it.task || it.step || it.title || it.text || JSON.stringify(it);
    const st = String(it.status || it.state || it.action || "").toLowerCase();
    if (st.includes("done") || st.includes("complete") || st.includes("finish") || it.completed === true) {
      status = "completed";
    } else if (st.includes("progress") || st.includes("active") || st.includes("doing") || st.includes("start")) {
      status = "in_progress";
    } else if (st.includes("cancel") || st.includes("drop") || st.includes("block")) {
      status = "cancelled";
    } else {
      status = "pending";
    }
  }
  return { content, status };
}

// 提取结构化待办列表，全面支持分阶段 Phase 树、扁平数组、单条任务操作与 JSON 字符串容错
function parseTodoData(input) {
  if (!input) return null;
  let obj = input;
  if (typeof obj === "string") {
    try {
      const p = JSON.parse(obj);
      if (p && typeof p === "object") obj = p;
    } catch (_) {}
  }
  if (Array.isArray(obj)) {
    return { op: "", phases: [{ phase: "", items: obj.map(normTodoItem) }] };
  }
  if (typeof obj !== "object") return null;

  const op = obj.op || obj.action || "";

  // 1. 检查 list 字段（omp/pi 分 phase 格式：list: [ { phase: "...", items: [...] } ]）
  let listVal = obj.list;
  if (typeof listVal === "string") {
    try {
      const p = JSON.parse(listVal);
      if (Array.isArray(p)) listVal = p;
    } catch (_) {}
  }
  if (Array.isArray(listVal) && listVal.length) {
    const isPhased = listVal.some((p) => p && typeof p === "object" && (Array.isArray(p.items) || Array.isArray(p.tasks)));
    if (isPhased) {
      const phases = listVal.map((p) => {
        const phName = (p && typeof p === "object") ? (p.phase || p.name || "") : "";
        const rawItems = (p && typeof p === "object") ? (p.items || p.tasks || []) : [];
        return {
          phase: phName,
          items: Array.isArray(rawItems) ? rawItems.map(normTodoItem) : [normTodoItem(p)],
        };
      });
      return { op, phases };
    }
    return { op, phases: [{ phase: "", items: listVal.map(normTodoItem) }] };
  }

  // 2. 检查常规数组字段：todos, plan, tasks, steps, items
  for (const k of ["todos", "plan", "tasks", "steps", "items"]) {
    let arr = obj[k];
    if (typeof arr === "string") {
      try {
        const p = JSON.parse(arr);
        if (Array.isArray(p)) arr = p;
      } catch (_) {}
    }
    if (Array.isArray(arr) && arr.length) {
      return { op, phases: [{ phase: "", items: arr.map(normTodoItem) }] };
    }
  }

  // 3. 单任务操作（例如 omp 的 todo 工具：{ action: "start", task: "..." }）
  if (obj.task || obj.content) {
    return {
      op,
      phases: [{
        phase: "",
        items: [{ content: obj.task || obj.content, status: obj.action || obj.status || "in_progress" }],
      }],
    };
  }

  return null;
}

function renderTodoBlock(input) {
  const data = parseTodoData(input);
  if (!data || !data.phases || !data.phases.length) return "";

  let total = 0;
  let doneCount = 0;
  let inProgCount = 0;

  for (const ph of data.phases) {
    for (const it of ph.items) {
      total++;
      if (it.status === "completed") doneCount++;
      else if (it.status === "in_progress") inProgCount++;
    }
  }
  if (!total) return "";

  const iconMap = {
    completed: '<span class="todo-ic todo-done" title="已完成">✓</span>',
    in_progress: '<span class="todo-ic todo-doing" title="进行中">▶</span>',
    cancelled: '<span class="todo-ic todo-cancel" title="已取消">✕</span>',
    pending: '<span class="todo-ic todo-pending" title="待处理">○</span>',
  };

  const phasesHTML = data.phases.map((ph) => {
    const phTotal = ph.items.length;
    const phDone = ph.items.filter((x) => x.status === "completed").length;
    const phaseHeader = ph.phase
      ? `<div class="todo-phase-h">
           <span class="todo-phase-tag">阶段</span>
           <span class="todo-phase-name">${escapeHTML(ph.phase)}</span>
           <span class="todo-phase-count">${phDone}/${phTotal}</span>
         </div>`
      : "";

    const itemsHTML = ph.items.map((it) => `
      <div class="todo-item todo-${it.status}">
        ${iconMap[it.status] || iconMap.pending}
        <span class="todo-text">${escapeHTML(it.content)}</span>
      </div>
    `).join("");

    return `<div class="todo-phase-block">${phaseHeader}<div class="blk-todo-list">${itemsHTML}</div></div>`;
  }).join("");

  const pct = Math.round((doneCount / total) * 100);
  const opTag = data.op ? `<span class="blk-todo-op">操作: ${escapeHTML(data.op)}</span>` : "";

  return `<div class="blk-todo-card">
    <div class="blk-todo-header">
      <div class="blk-todo-stat">
        <span class="blk-todo-title">任务清单</span>
        <div class="blk-todo-meta">
          ${opTag}
          <span class="blk-todo-badge">${doneCount}/${total} 完成${inProgCount ? ` · ${inProgCount} 进行中` : ""}</span>
        </div>
      </div>
      <div class="blk-todo-bar"><div class="blk-todo-bar-fill" style="width: ${pct}%"></div></div>
    </div>
    <div class="blk-todo-phases">${phasesHTML}</div>
  </div>`;
}

function blockHTML(b) {
  if (b.type === "text")
    return `<div class="blk blk-text">${renderMarkdown(b.text || "")}</div>`;
  if (b.type === "thinking") {
    const rawTxt = (b.text || "").trim();
    const len = rawTxt.length;
    const lenLabel = len >= 1000 ? (len / 1000).toFixed(1) + "k" : String(len);
    const isLong = len > 140;
    return `<details class="blk blk-thinking" ${isLong ? "" : "open"}>
      <summary class="blk-thinking-summary">
        <span class="blk-thinking-title">💭 思考过程</span>
        <span class="blk-thinking-len">（${lenLabel} 字）</span>
        <span class="blk-toggle-cue"></span>
      </summary>
      <div class="blk-thinking-body">${renderMarkdown(rawTxt) || escapeHTML(rawTxt)}</div>
    </details>`;
  }
  if (b.type === "tool_use") {
    let inputObj = b.input;
    if (typeof inputObj === "string") {
      try {
        const p = JSON.parse(inputObj);
        if (p && typeof p === "object") inputObj = p;
      } catch (_) {}
    }
    const intent = (inputObj && typeof inputObj === "object")
      ? (inputObj.i || inputObj.intent || "")
      : "";

    const isTodo = b.name && /todo|plan/i.test(b.name);
    const todoHTML = isTodo ? renderTodoBlock(inputObj) : "";
    const args = todoHTML ? "" : toolArgs(inputObj);

    return `<div class="blk blk-tool">
      <div class="blk-tool-head">
        <span class="blk-tool-ic">${toolSVG(b.name)}</span>
        <span class="blk-tool-name">${escapeHTML(b.name || "工具")}</span>
        ${intent ? `<span class="blk-tool-intent">${escapeHTML(intent)}</span>` : ""}
      </div>
      ${todoHTML || (args ? `<div class="blk-tool-arg">${args}</div>` : "")}
    </div>`;
  }
  if (b.type === "tool_result") {
    const err = b.is_error === true;
    const txt = b.text || "";
    const lines = txt.split("\n");
    const lineCount = lines.length;
    const isLong = lineCount > 8 || txt.length > 500;
    const metaInfo = lineCount > 1 ? ` · ${lineCount} 行` : "";
    return `<details class="blk blk-result ${err ? "err" : ""}" ${isLong ? "" : "open"}>
      <summary class="blk-result-head">
        <span class="blk-result-title">${toolSVG(b.name)} 工具结果 ${err ? "· 失败" : "· 成功"}<span class="blk-result-meta">${metaInfo}</span></span>
        <span class="blk-toggle-cue"></span>
      </summary>
      <div class="blk-result-body"><pre>${escapeHTML(txt)}</pre></div>
    </details>`;
  }
  return `<div class="blk blk-text">${escapeHTML(JSON.stringify(b))}</div>`;
}

