/* 页面：Auth Token 管理 —— 生成 / 查看 / 撤销上传凭据。

   客户端把 token 填入 tcer_ui.json 的 upload.auth_token 后上传时，服务端按该
   token 归属的用户记账；本页让用户自助生成 token（生成后只显示一次，仅存哈希）、
   查看已有 token 元数据、随时撤销。管理接口要求登录态 token（非 auth token），故只
   在网页登录后可用。 */
// ------------------------- 页面：Auth Token -------------------------

/* 复制文本到剪贴板。navigator.clipboard 仅在安全上下文（HTTPS / localhost）可用，
   HTTP 部署下它为 undefined 或直接 reject —— 这正是「点了复制没反应」的根因。
   回退到 execCommand("copy")：临时 textarea 选中 + 复制，兼容非安全上下文。 */
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      /* fall through to legacy path */
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (_) {
    return false;
  }
}

async function renderTokens() {
  const content = document.getElementById("content");
  content.innerHTML = `<div class="empty">加载中…</div>`;
  let data;
  try {
    data = await api("/api/tokens");
  } catch (e) {
    content.innerHTML = `<div class="empty">加载失败：${escapeHTML(e.message)}</div>`;
    return;
  }
  const rows = (data.tokens || []).map((t) => `
    <tr>
      <td>${escapeHTML(t.label) || "（无备注）"}</td>
      <td class="num">${t.created_at ? fmt.date(t.created_at) : "—"}</td>
      <td class="num">${t.last_used_at ? fmt.date(t.last_used_at) : "从未使用"}</td>
      <td><button class="btn-ghost tk-del" data-id="${t.id}">撤销</button></td>
    </tr>`).join("");

  content.innerHTML = `
    <div class="panel" style="max-width:820px">
      <div class="panel-title">Auth Token</div>
      <p class="tk-hint">
        把 Token 填入客户端 <code>tcer_ui.json</code> 的 <code>upload.auth_token</code> 即可携带上传；
        服务端据此把上传归到对应用户名下。未配置 Token 的上传按匿名处理。
        Token 生成后<strong>只显示一次</strong>（服务端仅存哈希，无法找回），请妥善保存。
      </p>
      <div class="tk-gen">
        <input id="tk-label" class="filter" type="text" placeholder="备注（可选，如 laptop / ci）" style="min-width:220px">
        <button id="tk-create" class="btn-primary">生成新 Token</button>
      </div>
      <div id="tk-new"></div>
      <table class="tbl" style="margin-top:14px">
        <thead><tr><th>备注</th><th>创建</th><th>最近使用</th><th></th></tr></thead>
        <tbody id="tk-rows">${rows || '<tr><td colspan="4" class="empty" style="padding:20px">暂无 Token</td></tr>'}</tbody>
      </table>
    </div>`;

  document.getElementById("tk-create").addEventListener("click", async () => {
    const label = document.getElementById("tk-label").value.trim();
    try {
      const r = await api("/api/tokens", { method: "POST", body: JSON.stringify({ label }) });
      document.getElementById("tk-new").innerHTML = `
        <div class="tk-reveal">
          <div class="tk-reveal-hd">新 Token（只显示这一次，请立即复制）</div>
          <div class="tk-reveal-row">
            <code id="tk-val">${r.token}</code>
            <button id="tk-copy" class="btn-ghost">复制</button>
          </div>
        </div>`;
      document.getElementById("tk-copy").addEventListener("click", () => {
        copyText(r.token).then((ok) => {
          document.getElementById("tk-copy").textContent = ok ? "已复制" : "复制失败";
        });
      });
      // Prepend the new token's row without a full re-render, so the one-time
      // reveal above stays on screen (a re-render would wipe it).
      const tbody = document.getElementById("tk-rows");
      const empty = tbody.querySelector(".empty");
      if (empty) tbody.innerHTML = "";
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${escapeHTML(label) || "（无备注）"}</td>` +
        `<td class="num">${fmt.date(Math.floor(Date.now() / 1000))}</td>` +
        `<td class="num">从未使用</td><td></td>`;
      tbody.insertBefore(tr, tbody.firstChild);
      document.getElementById("tk-label").value = "";
    } catch (e) {
      document.getElementById("tk-new").innerHTML = `<div class="tk-err">生成失败：${escapeHTML(e.message)}</div>`;
    }
  });

  content.querySelectorAll(".tk-del").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("撤销该 Token 后，使用它的客户端将无法再上传。确认撤销？")) return;
      try {
        await api("/api/tokens?id=" + encodeURIComponent(btn.dataset.id), { method: "DELETE" });
        renderTokens();
      } catch (e) {
        alert("撤销失败：" + e.message);
      }
    });
  });
}