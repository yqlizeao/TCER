/* 路由 / 筛选 / 事件 / 认证 / 启动 */
// ------------------------- 路由 -------------------------
const VIEWS = {
  projects: { title: "项目总览", render: renderProjects },
  exec: { title: "投入产出", render: renderExec },
  engineering: { title: "工程效能", render: renderEngineering },
  diagnosis: { title: "问题诊断", render: renderDiagnosis },
  sessions: { title: "会话明细", render: renderSessions },
};

async function route() {
  const v = VIEWS[S.view];
  document.getElementById("view-title").textContent = v.title;
  document.getElementById("content").innerHTML = `<div class="empty">加载中…</div>`;
  disposeCharts();
  try { await v.render(); }
  catch (e) {
    document.getElementById("content").innerHTML =
      `<div class="empty">加载失败：${e.message}</div>`;
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
function showApp() {
  document.getElementById("login").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  document.getElementById("who").textContent = S.user || "已登录";
  loadFilters();
  route();
}
function logout() {
  S.token = ""; S.user = "";
  localStorage.removeItem("tcer_token");
  localStorage.removeItem("tcer_user");
  document.getElementById("app").classList.add("hidden");
  document.getElementById("login").classList.remove("hidden");
}
function bindLogin() {
  document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const u = document.getElementById("login-user").value.trim();
    const p = document.getElementById("login-pass").value;
    try {
      const r = await api("/api/login", { method: "POST", body: JSON.stringify({ username: u, password: p }) });
      S.token = r.token; S.user = u;
      localStorage.setItem("tcer_token", r.token);
      localStorage.setItem("tcer_user", u);
      showApp();
    } catch (err) {
      document.getElementById("login-err").textContent = "登录失败："+ err.message;
    }
  });
}

// ------------------------- 启动 -------------------------
bindLogin();
bind();
