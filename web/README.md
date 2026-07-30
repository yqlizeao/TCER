# TCER Web — 上传与仪表盘服务端

接收 TCER 客户端上传的效率报告，提供专业的多维效率仪表盘。后端纯 Python 标准库、
运行时零联网；前端为单页应用（SPA），曲线图使用本地内置的 ECharts（Apache-2.0，
`frontend/vendor/echarts.min.js`），不走 CDN，离线可用。

## 目录

```
web/
├── backend/           纯 stdlib HTTP 服务
│   ├── server.py      HTTP 服务 + 路由 + 静态托管
│   ├── db.py          SQLite 存储 + 聚合查询（去重 / 归一 / 别名）
│   ├── analysis.py    决策实验室：分层队列对比 + bootstrap 区间 + 证据分级
│   ├── insights.py    建议引擎：把对比结果转成可执行建议（含「说不出结论」态）
│   ├── auth.py        HMAC 签名的无状态 Bearer Token
│   ├── manage.py      账号管理 CLI
│   └── seed_mock.py   造数脚本（含植入的已知效应，验收用）
├── frontend/          单页应用（左右布局 · 可收起菜单 · ECharts 曲线）
│   ├── index.html     应用外壳（登录 / 侧栏 / 各视图容器）
│   ├── app.js         SPA 路由 + 图表 + 聚合表 + 会话视图
│   ├── style.css      深色主题
│   └── vendor/
│       └── echarts.min.js   本地内置图表库（Apache-2.0）
├── RESEARCH-insights.md    竞品调研 / 现状缺口 / 路线取舍（决策实验室的由来）
└── PLAN-client-upload.md   GUI 端上传功能实现计划（待实现）
```

## 界面

左右布局，左侧菜单可收起（宽/窄两态）。菜单项：

- **总览仪表盘**（默认首页）：顶部大盘数字（总 Token，单位固定 M，含输入/输出/缓存创建/
  缓存命中；代码净增长行数；总成本；总评分 TCER 行/百万Token），下方按**最近 7 天**
  （可切 30/90 天）聚合的三张曲线——按人员 / 按项目 / 按模型，曲线指标可切换。
- **详情**：
  - **项目聚合**——把所有人上传里项目名一致的合并统计；名称不一致可点「归并」手动调整。
  - **人员聚合**——按上报人聚合。
  - **模型聚合**——自动识别归一（如 `claude-opus-4-8` 与 `claude-opus-4.8` 视为同一模型，
    复用 `tcer.core.pricing`），也可手动归并。以表格呈现 TCER 等指标。
- **会话详情**：顶部筛选（项目 / 人员 / 模型多选 + 时间范围），次级 sidebar 列出会话，
  右侧展示会话概要 + 逐回合会话明细（用户 / 助手 / 思考 / 工具调用 / 工具结果气泡）+
  可折叠的原始上传数据。逐回合明细来自「含明细」上传时附带的 `conversation` 字段，五个
  数据源（Claude / Codex / Grok / OpenCode / omp）的不同磁盘结构在客户端各自的
  `read_conversation` 里归一到同一 block 形状再上传。仅上传聚合信息、或未附带明细的记录
  在列表打「仅聚合」tag / 显示补传提示。
- **决策实验室**（调研与选型见 [RESEARCH-insights.md](RESEARCH-insights.md)）：
  - **行动建议**——规则引擎给出「该改什么」的可执行结论（模型选型 / Agent 工具选型 /
    harness 档位 / Skill 与 MCP 插件取舍 / 会话拆小），每条附证据、置信区间与证据等级；
    达不到门槛的维度进「未能给出结论」清单并说明原因。
  - **队列对比**——任意配置维度分组，按「任务类型 × 会话规模」分层后对比，同时给出
    未分层差异（两者的落差就是混杂因素的大小）、95% bootstrap 区间与质量护栏。

## 启动

```bash
python web/backend/server.py
# 打开 http://127.0.0.1:8899  （默认账号 admin/admin，请尽快修改）
```

环境变量：`TCER_WEB_HOST`（默认 127.0.0.1）、`TCER_WEB_PORT`（8899）、
`TCER_WEB_SECRET`（Token 签名密钥，不设则每次重启随机——重启后旧 Token 失效）、
`TCER_WEB_DB`（SQLite 路径，默认 `web/backend/tcer_web.db`）。

## 账号管理

```bash
python web/backend/manage.py adduser  <用户名> <密码>
python web/backend/manage.py passwd   <用户名> <新密码>
python web/backend/manage.py listusers
```

> 第一版仅做账号密码校验，登录后不区分权限；细粒度权限后续再补。

## API

| 方法 | 路径 | 认证 | 说明 |
|---|---|---|---|
| POST | `/api/login` | — | `{username,password}` → `{token}` |
| POST | `/api/upload` | Bearer | 上传报告（见下） → `{inserted}` |
| GET | `/api/filters` | Bearer | 筛选下拉数据（归一后） → `{persons,projects,models}` |
| GET | `/api/overview` | Bearer | 大盘数字 + 三张曲线 → `{totals,series:{person,project,model}}` |
| GET | `/api/detail` | Bearer | 聚合表 → `{dimension,rows:[{group,display,raw_names,…指标}]}` |
| GET | `/api/aliases` | Bearer | 读取别名 `?kind=project\|model` → `{aliases}` |
| POST | `/api/aliases` | Bearer | 写别名 `{kind,raw,canonical}`（canonical 空=删除） → `{ok}` |
| GET | `/api/sessions` | Bearer | 会话列表 → `{sessions,total}`（`aggregate_only` 标记） |
| GET | `/api/session` | Bearer | 单条明细 `?id=` → `{raw,aggregate_only,…}` |
| GET | `/api/dimensions` | Bearer | 可对比的维度与指标（UI 不硬编码） → `{dimensions,metrics}` |
| GET | `/api/compare` | Bearer | 队列对比 `?dimension=&metric=` → `{cohorts,caveat}` |
| GET | `/api/insights` | Bearer | 行动建议 → `{findings,coverage,caveat}` |
| GET | `/api/health` | — | `{ok:true}` |

### 上传结构（对齐客户端）

Body 直接复用客户端 `export.report_row_dict` 的字段；服务端只提升需要筛选/绘图的列，完整行存 `raw_json`。

```jsonc
{
  "user": "joey",            // 上报人；anonymous=true 时忽略
  "anonymous": false,
  "project": "TCER",
  "detail": false,           // false=仅 aggregate；true=含 sessions 明细
  "generated_at": 1720000000,// epoch 秒，明细无 started_at 时作时间轴回退
  "aggregate": { /* report_row_dict(agg) + sessions_counted */ },
  "sessions":  [ /* report_row_dict(r) … */ ]   // detail=true 才带
}
```

**时间轴**：每条记录的横轴时间取 session 的 `started_at`（epoch ms，会自动折算为秒），缺失时回退到 `generated_at`。

**配置维度**：决策实验室要对比的是「会话是用什么配置跑出来的」，这些字段来自
`report_row_dict`，服务端提升为独立列：`source` / `task_type` / `reasoning_effort` /
`approval_policy` / `permission_profile` / `collaboration_mode` / `cli_version` / `entrypoint`。
另有两个字典字段：

- `tool_calls`：原始工具名 → 次数。MCP 服务器从 `mcp__<server>__<tool>` 键派生，**五源通用**。
- `tool_variants`：`"Skill:dataviz"` / `"Agent:Explore"` → 次数。`Skill`/`Task`/`Agent` 在
  `tool_calls` 里只有一个键，看不出到底调了哪个技能，所以由 reader 从调用入参补录。
  **目前仅 Claude 源可解析**——其余四个 CLI 没有以 reader 已解析的形状暴露技能/子代理身份。


### 公共查询参数

`/api/overview`、`/api/detail`、`/api/sessions` 共用一组筛选：

- `persons` / `projects` / `models`：逗号分隔多选过滤（**项目/模型按归一后的标准名匹配**）
- `start` / `end`：epoch 秒时间范围
- `/api/overview` 另有 `metric`（曲线指标）；`/api/detail` 另有 `dimension`（`project|person|model`）
- 可选指标 `metric`：`tcer`（默认）| `ctei` | `cost_usd` | `cpe` | `net_loc` | `total_tokens` |
  `churn_ratio` | `chr` | `read_before_write` | `search_edit_ratio` | `tool_error_rate`

### 聚合与去重规则

- **去重**：一次上传的所有行共享 `batch_id`。含明细的上传其 aggregate 行会被丢弃（由 session 行覆盖），
  仅聚合的上传保留 aggregate 行。避免明细 + 聚合双重计数。
- **求和 vs. 比率**：Token / 净增行 / 成本按求和；**比率一律用「分子和 ÷ 分母和」重算，不取均值**
  （TCER、CPE、CHR 直接重算；返工率按 `code_added` 加权；工具错误率按工具调用数加权）。
  对 per-session 比率取算术平均会让 5k token 的会话与 2M token 的会话等权，触发 Simpson 悖论。
  `read_before_write` / `search_edit_ratio` 的分母不在库里（来自工具时序），退化为**中位数**，
  由返回体的 `_stat` 字段标明每个比率实际用了哪种统计量。
- **聚合 CTEI 重算不取平均**：CTEI 三因子化后可聚合（`CLAUDE.md` 规则 9），
  服务端按 `tcer.core.metrics.ctei(聚合TCER, 聚合CPE, 聚合CHR)` 重算并附 `grade`，
  与桌面端 audit 的 `aggregate_ctei_recompute` 同一口径；**不**对各会话 CTEI 取算术平均。
- **项目归一**：`project_aliases` 手动 `raw→canonical`；未命中则原样。
- **模型归一**：先查 `model_aliases`；否则复用 `tcer.core.pricing`（含 `.`↔`-` 变体尝试，
  故 `claude-opus-4-8` 与 `claude-opus-4.8` 自动合并），落到价表规范 key。

## 造数

```bash
python web/backend/seed_mock.py   # 近 30 天多人/多项目/多模型样本（含模型变体，验证自动归一）
```