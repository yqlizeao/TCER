/* 路由 / 筛选 / 事件 / 认证 / 启动 */
// ------------------------- 路由 -------------------------
const VIEWS = {
  projects: { title: "项目总览", render: renderProjects },
  exec: { title: "投入产出", render: renderExec },
  engineering: { title: "工程效能", render: renderEngineering },
  diagnosis: { title: "问题诊断", render: renderDiagnosis },
  sessions: { title: "会话明细", render: renderSessions },
  aliases: { title: "聚合配置", render: renderAliases },
  tokens: { title: "Auth Token", render: renderTokens },
};

async function route() {
  const v = VIEWS[S.view];
  document.getElementById("view-title").textContent = v.title;
  document.getElementById("content").innerHTML = `<div class="empty">加载中…</div>`;
  disposeCharts();
  try { await v.render(); }
  catch (e) {
    document.getElementById("content").innerHTML =
      `<div class="empty">加载失败：${escapeHTML(e.message)}</div>`;
  }
}

// ------------------------- 筛选下拉 -------------------------
async function loadFilters() {
  try {
    const f = await api("/api/filters");
    fillSelect("f-person", f.persons, "全部成员");
    fillSelect("f-project", f.projects, "全部项目");
  } catch (e) { /* empty DB is fine */ }
}
function fillSelect(id, items, placeholder) {
  const el = document.getElementById(id);
  el.innerHTML = `<option value="">${placeholder}</option>` +
    items.map((i) => `<option value="${i}">${i}</option>`).join("");
}
function getMulti(id) {
  return Array.from(document.getElementById(id).selectedOptions)
    .map((o) => o.value).filter(Boolean);
}

// ------------------------- 事件 -------------------------
function bind() {
  document.getElementById("rail-nav").addEventListener("click", (e) => {
    const btn = e.target.closest(".rail-item");
    if (!btn) return;
    document.querySelectorAll(".rail-item").forEach((x) => x.classList.remove("active"));
    btn.classList.add("active");
      S.view = btn.dataset.view;
    route();
  });

  document.getElementById("range-seg").addEventListener("click", (e) => {
    if (e.target.tagName !== "BUTTON") return;
    document.querySelectorAll("#range-seg button").forEach((x) => x.classList.remove("active"));
    e.target.classList.add("active");
    S.days = +e.target.dataset.days;
    route();
  });

  const onFilter = () => {
    S.persons = getMulti("f-person");
    S.projects = getMulti("f-project");
    route();
  };
  document.getElementById("f-person").addEventListener("change", onFilter);
  document.getElementById("f-project").addEventListener("change", onFilter);

  document.getElementById("logout").addEventListener("click", logout);
  window.addEventListener("resize", () => S.charts.forEach((c) => c.resize()));
}

// ------------------------- 认证 -------------------------
// 名字首字母大写（账密用户无头像时的占位）；中文取首字，英文取首字母大写。
function initialOf(name) {
  const s = (name || "").trim();
  if (!s) return "?";
  const ch = s[0];
  return /[a-z]/.test(ch) ? ch.toUpperCase() : ch;
}

// 左下角用户区：飞书用户显示头像图片，账密用户显示名字首字母。
function renderUser(name, avatarUrl) {
  document.getElementById("who").textContent = name || "已登录";
  const el = document.getElementById("user-avatar");
  if (avatarUrl) {
    el.className = "user-avatar has-img";
    // 只允许 https: 开头的头像 URL，防止属性逃逸（javascript: / data: 等协议注入）
    const safeAvatar = /^https:\/\//i.test(avatarUrl) ? avatarUrl : "";
    el.innerHTML = `<img src="${escapeHTML(safeAvatar)}" alt="" width="24" height="24">`;
  } else {
    el.className = "user-avatar";
    el.textContent = initialOf(name);
  }
}

async function showApp() {
  document.getElementById("login").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  renderUser(S.name || S.user, S.avatar);   // 立即用已知信息渲染
  try {                                       // 再向后端确认（拿飞书头像/名字）
    const me = await api("/api/me");
    S.name = me.name || S.user; S.avatar = me.avatar_url || "";
    localStorage.setItem("tcer_name", S.name);
    localStorage.setItem("tcer_avatar", S.avatar);
    renderUser(S.name, S.avatar);
  } catch (e) { /* 401 会自行 logout；其余忽略 */ }
  loadFilters();
  route();
}
function logout() {
  S.token = ""; S.user = ""; S.name = ""; S.avatar = "";
  localStorage.removeItem("tcer_token");
  localStorage.removeItem("tcer_user");
  localStorage.removeItem("tcer_name");
  localStorage.removeItem("tcer_avatar");
  document.getElementById("app").classList.add("hidden");
  document.getElementById("login").classList.remove("hidden");
}

// 依据后端登录配置显隐 账密表单 / 飞书按钮 / 分隔线。
async function applyLoginConfig() {
  let cfg = { password_login: true, feishu_login: false };
  try { cfg = await api("/api/config"); } catch (e) { /* 用默认 */ }
  const pw = document.getElementById("login-form");
  const fs = document.getElementById("login-feishu");
  const dv = document.getElementById("login-divider");
  pw.classList.toggle("hidden", !cfg.password_login);
  fs.classList.toggle("hidden", !cfg.feishu_login);
  dv.classList.toggle("hidden", !(cfg.password_login && cfg.feishu_login));
  const userInput = document.getElementById("login-user");
  const passInput = document.getElementById("login-pass");
  if (userInput) userInput.required = cfg.password_login;
  if (passInput) passInput.required = cfg.password_login;
}

// 飞书回调把 token 放在 URL fragment（#feishu=token=..&name=..）里返回。
function consumeFeishuRedirect() {
  const params = new URLSearchParams(window.location.search);
  const err = params.get("feishu_error");
  if (err) {
    document.getElementById("login-err").textContent = "飞书登录失败：" + err;
    history.replaceState(null, "", window.location.pathname);
    return false;
  }
  const hash = window.location.hash || "";
  if (!hash.startsWith("#feishu=")) return false;
  const fp = new URLSearchParams(hash.slice("#feishu=".length));
  const token = fp.get("token");
  if (!token) return false;
  S.token = token;
  S.user = fp.get("name") || "飞书用户";
  S.name = S.user; S.avatar = "";
  localStorage.setItem("tcer_token", token);
  localStorage.setItem("tcer_user", S.user);
  localStorage.setItem("tcer_name", S.name);
  history.replaceState(null, "", window.location.pathname);
  return true;
}

function bindLogin() {
  document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const u = document.getElementById("login-user").value.trim();
    const p = document.getElementById("login-pass").value;
    try {
      const r = await api("/api/login", { method: "POST", body: JSON.stringify({ username: u, password: p }) });
      S.token = r.token; S.user = u; S.name = u; S.avatar = "";
      localStorage.setItem("tcer_token", r.token);
      localStorage.setItem("tcer_user", u);
      localStorage.setItem("tcer_name", u);
      localStorage.removeItem("tcer_avatar");
      showApp();
    } catch (err) {
      document.getElementById("login-err").textContent = "登录失败："+ err.message;
    }
  });
  document.getElementById("login-feishu").addEventListener("click", () => {
    window.location.href = "/api/auth/feishu/start";
  });
}

// ------------------------- 启动 -------------------------
bindLogin();
bind();
applyLoginConfig();
if (consumeFeishuRedirect() || S.token) {
  showApp();
}
