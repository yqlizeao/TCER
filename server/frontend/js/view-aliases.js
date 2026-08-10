/* 页面：聚合配置 —— 人员 / 项目 / 模型的合并规则管理。

   不同平台、不同机器、不同 coding agent 上传时携带的原始标识不一样（同一个人
   可能是 feishu:ou_xxx、匿名-ab12、机器本地用户名；同一个项目在不同机器上路径
   不同）。本页把「原始标识 → 规范名」的合并规则暴露出来：把多个原始标识指到同一
   个规范名，它们就聚合成一个人 / 一个项目。规则在查询时应用，所以设置后——包括
   之后同一个人新上传的、带已有原始标识的新会话——都会自动归并，无需重传旧数据。

   后端接口：
     GET  /api/detail?dimension=person|project|model  → {rows:[{group,display,raw_names,...}]}
     GET  /api/aliases?kind=person|project|model       → {aliases:{raw:canonical}}
     POST /api/aliases {kind, raw, canonical}          → 设置/清除单条映射
*/
// ------------------------- 页面：聚合配置 -------------------------

const ALIAS_KINDS = [
  { key: "person", label: "成员", hint: "把同一个人的多个上传标识合并成一个人" },
  { key: "project", label: "项目", hint: "把同一项目在不同机器/路径下的标识合并" },
  { key: "model", label: "模型", hint: "在自动归一之外，手动合并模型别名" },
];

async function renderAliases() {
  const content = document.getElementById("content");
  if (!S.aliasKind) S.aliasKind = "person";

  const tabs = ALIAS_KINDS.map((k) =>
    `<button class="alias-tab ${S.aliasKind === k.key ? "active" : ""}" data-kind="${k.key}">${k.label}</button>`
  ).join("");

  content.innerHTML = `
    <div class="panel alias-panel">
      <div class="panel-head"><div>
        <div class="panel-title">聚合配置</div>
        <div class="panel-note">${ALIAS_KINDS.find((k) => k.key === S.aliasKind).hint}</div>
      </div></div>
      <div class="alias-tabs">${tabs}</div>
      <div id="alias-body"><div class="empty">加载中…</div></div>
    </div>`;

  content.querySelectorAll(".alias-tab").forEach((b) =>
    b.addEventListener("click", () => { S.aliasKind = b.dataset.kind; renderAliases(); }));

  await paintAliasBody();
}

async function paintAliasBody() {
  const kind = S.aliasKind;
  const body = document.getElementById("alias-body");
  let detail, aliases;
  try {
    [detail, aliases] = await Promise.all([
      api(`/api/detail?dimension=${kind}&` + windowParams()),
      api(`/api/aliases?kind=${kind}`),
    ]);
  } catch (e) {
    body.innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
    return;
  }

  const amap = aliases.aliases || {};
  const rows = detail.rows || [];
  // 当前所有规范名（合并目标候选）。
  const groups = rows.map((r) => r.group).filter(Boolean).sort();

  // 每个规范组一张卡片：展示合并进来的原始标识，可整体改名，可拆出单个原始标识。
  const cards = rows.map((r) => {
    const raws = (r.raw_names || []);
    const chips = raws.map((raw) => {
      const mapped = amap[raw];
      const merged = mapped && mapped !== raw;
      return `<span class="alias-chip ${merged ? "merged" : ""}">
        <span class="alias-raw" title="原始标识">${escapeHTML(raw)}</span>
        ${merged ? `<button class="alias-unmap" data-raw="${escapeHTML(raw)}" title="取消合并">×</button>` : ""}
      </span>`;
    }).join("");
    return `<div class="alias-card">
      <div class="alias-card-hd">
        <span class="alias-group">${escapeHTML(r.display || r.group)}</span>
        <span class="alias-count">${raws.length} 个标识 · ${fmt.int(r.sessions)} 会话</span>
      </div>
      <div class="alias-chips">${chips || '<span class="empty">无原始标识</span>'}</div>
    </div>`;
  }).join("");

  // 合并操作区：选一个原始标识 + 目标规范名（或输入新名）→ 建立映射。
  const rawOptions = rows.flatMap((r) => r.raw_names || [])
    .filter((v, i, a) => v && a.indexOf(v) === i).sort()
    .map((raw) => `<option value="${escapeHTML(raw)}">${escapeHTML(raw)}</option>`).join("");
  const groupOptions = groups
    .map((g) => `<option value="${escapeHTML(g)}">${escapeHTML(g)}</option>`).join("");

  body.innerHTML = `
    <div class="alias-merge">
      <div class="alias-merge-row">
        <label>把标识（可多选，Ctrl/Shift 点选）</label>
        <select id="alias-raw" class="filter alias-raw-multi" multiple size="6">${rawOptions || '<option value="">（暂无数据）</option>'}</select>
        <div class="alias-merge-target">
          <label>合并到</label>
          <select id="alias-target" class="filter" style="min-width:180px">
            <option value="">— 选择已有规范名 —</option>${groupOptions}
          </select>
          <span class="alias-or">或新名</span>
          <input id="alias-newname" class="filter" type="text" placeholder="输入新的规范名" style="min-width:160px">
          <button id="alias-apply" class="btn-primary">合并</button>
        </div>
      </div>
      <div id="alias-msg" class="alias-msg"></div>
    </div>
    <div class="alias-cards">${cards || '<div class="empty">窗口内无数据</div>'}</div>`;

  document.getElementById("alias-apply").addEventListener("click", async () => {
    const raws = Array.from(document.getElementById("alias-raw").selectedOptions)
      .map((o) => o.value).filter(Boolean);
    const newName = document.getElementById("alias-newname").value.trim();
    const target = document.getElementById("alias-target").value;
    const canonical = newName || target;
    const msg = document.getElementById("alias-msg");
    if (!raws.length || !canonical) {
      msg.className = "alias-msg err";
      msg.textContent = "请选择要合并的标识（可多选），并指定目标规范名。";
      return;
    }
    try {
      await api("/api/aliases", { method: "POST",
        body: JSON.stringify({ kind, raws, canonical }) });
      msg.className = "alias-msg ok";
      msg.textContent = `已把 ${raws.length} 个标识合并到「${canonical}」。新上传的同标识会话会自动归并。`;
      await paintAliasBody();
      // 刷新顶栏筛选下拉，规范名变化后即时可见。
      loadFilters();
    } catch (e) {
      msg.className = "alias-msg err";
      msg.textContent = "合并失败：" + e.message;
    }
  });

  body.querySelectorAll(".alias-unmap").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const raw = btn.dataset.raw;
      try {
        // canonical 传空 → 后端删除该映射（拆分回原始标识）。
        await api("/api/aliases", { method: "POST",
          body: JSON.stringify({ kind, raw, canonical: "" }) });
        await paintAliasBody();
        loadFilters();
      } catch (e) {
        const msg = document.getElementById("alias-msg");
        msg.className = "alias-msg err";
        msg.textContent = "消合并失败：" + e.message;
      }
    }));
}