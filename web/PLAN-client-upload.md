# 客户端上传功能 Plan（待实现）

> 本文件是 GUI 端「上传」功能的实现计划。服务端（`web/backend`）已先行落地，本计划与其 `POST /api/upload` 契约对齐。

## 1. 入口：主界面右上角上传按钮

位置：`tcer/gui/views.py` 顶部工具条，「导出 ▾」「工具 ▾」旁边新增「上传 ▾」或「上传…」按钮。

- 参考 `_make_export_menu` / `_make_tool_menu` 的构建方式（`tk.Menubutton` + `theme` 配色 + `Tooltip`）。
- 点击打开一个**选项面板**（`popups.py` 里新增 `UploadDialog`，参考现有 `show_advanced` 弹窗风格）。

## 2. 选项面板字段

| 字段 | 控件 | 默认 | 说明 |
|---|---|---|---|
| 服务器地址 | Entry | 上次值 | 如 `http://host:8899` |
| 账号 / 密码 | Entry / Entry(show=*) | 上次账号 | 密码可选记住 |
| 是否匿名 | Checkbutton | 否 | 勾选后 person 上传为空/哈希占位 |
| 选择项目 | 下拉/多选 | 当前项目 | 来源 `ProjectRef` 列表 |
| 是否上传详情 | Checkbutton | **否**（仅聚合） | 勾选才发送 per-session 明细 |
| 是否自动上传 | Checkbutton + 间隔 | 否 / 30min | 勾选后后台定时上传 |
| [完成上传] | Button | — | 触发上传并回显状态 |

## 3. 选项持久化

- 复用项目现有 config 落盘模式（`metrics.save_baselines` 写 JSON 的方式）。
- 新增 `tcer/core/upload_prefs.py`：读写 `~/.claude/tcer_upload.json`（或 `CLAUDE_CONFIG_DIR` 下）。
  - 存：server_url、username、（可选）password、anonymous、last_project、detail、auto_upload、interval_min。
  - **密码**：默认不落盘明文；如需记住，至少 base64/obfuscate，并在 UI 注明非加密存储。
- 打开面板时自动 `load()` 回填。

## 4. 上传数据构造

复用 `tcer/core/export.py::report_row_dict` —— 服务端 schema 已按它对齐。

```jsonc
POST /api/upload   (Header: Authorization: Bearer <token>)
{
  "client_version": "tcer x.y.z",
  "anonymous": false,
  "user": "joey",              // anonymous=true 时省略或置 null
  "project": "TCER",           // 选中的项目 key
  "detail": false,             // false=仅 aggregate，true=含 sessions
  "generated_at": 1720000000,  // epoch s
  "aggregate": { ...report_row_dict(agg)... , "sessions_counted": N },
  "sessions":  [ ...report_row_dict(r)... ]   // detail=true 时才带
}
```

先 `POST /api/login {username,password}` 换取 `token`，再带 `Authorization` 调用 upload。

## 5. 自动上传

- `app.py` 用 `after(interval_ms, ...)` 或后台线程 `threading.Timer`，到点复用「完成上传」逻辑。
- 失败静默重试 + 状态栏提示，不打断分析主流程。

## 6. 时间轴对齐（重要）

服务端图表横轴是时间，需要每条记录带时间戳：
- **明细模式**：每个 session 用 `started_at`（当前 `report_row_dict` **未导出** started_at/ended_at，需补充导出这两个字段）。
- **聚合模式**：用 aggregate 的时间窗（补充导出 aggregate 的 started_at/ended_at），或退化为上传时间 `generated_at`。

> ⚠️ 落地上传前，需先在 `report_row_dict` 补 `started_at` / `ended_at`（epoch ms），否则时间曲线只能按上传时间分桶。

## 7. 客户端对齐清单（服务端已就绪，客户端待补充）

服务端已按 `export.report_row_dict` 的字段名对齐，客户端无需改造即可发送该字典。以下是**唯一需要客户端补充**的对齐点，按优先级：

### 7.1 必须补：`started_at` / `ended_at`（阻塞时间轴）
- 现状：`report_row_dict`（`tcer/core/export.py`）**未导出**这两个字段，`SessionReport.usage` 里有 `started_at`/`ended_at`（epoch ms）。
- 影响：服务端 `db.insert_records` 按 `row["started_at"]` 定位每条记录在时间轴的位置；缺失时全部回退到 `generated_at`（上传时刻），导致同一批明细点堆在同一横坐标。
- 待办：在 `report_row_dict` 增加 `"started_at": u.started_at` 与 `"ended_at": u.ended_at`（保持 epoch ms；服务端会自动折算为秒）。

### 7.2 已对齐：四个下钻指标（无需改动）
服务端已支持按以下指标出图，且字段名与 `report_row_dict` **完全一致**，客户端照常发送即可：

| 指标 | 客户端字段 | 服务端列 |
|---|---|---|
| 缓存命中率 CHR | `chr` | `chr` |
| 先读后写率 | `read_before_write` | `read_before_write` |
| 搜索后编辑比 | `search_edit_ratio` | `search_edit_ratio` |
| 工具错误率 | `tool_error_rate` | `tool_error_rate` |

### 7.3 已对齐：主指标与分组维度
- 分组维度：`person`（来自 payload 顶层 `user`）、`project`（顶层 `project`）、`model`（取 `models_label`，缺失时取 `models[0]`）。
- 主指标：`tcer` / `ctei` / `cost_usd` / `net_loc` / `total_tokens` / `churn_ratio` 均已是 `report_row_dict` 现有字段，无需改动。

> 结论：客户端上传功能落地时，**代码改动仅 7.1 一处**（补两个时间字段）；其余全部字段已对齐，直接发 `report_row_dict` 即可。