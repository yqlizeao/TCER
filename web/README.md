# TCER Web — 上传与仪表盘服务端

接收 TCER 客户端上传的效率报告，提供多维筛选的 TCER 曲线仪表盘。纯 Python 标准库 + 原生前端，零第三方依赖，与主项目一致。

## 目录

```
web/
├── backend/           纯 stdlib HTTP 服务
│   ├── server.py      HTTP 服务 + 路由 + 静态托管
│   ├── db.py          SQLite 存储（users / uploads）
│   ├── auth.py        HMAC 签名的无状态 Bearer Token
│   └── manage.py      账号管理 CLI
├── frontend/          原生 JS 仪表盘（登录 + 筛选 + 三张 SVG 曲线图）
│   ├── index.html
│   ├── app.js
│   └── style.css
└── PLAN-client-upload.md   GUI 端上传功能实现计划（待实现）
```

## 启动

```bash
python web/backend/server.py
# 打开 http://127.0.0.1:8787  （默认账号 admin/admin，请尽快修改）
```

环境变量：`TCER_WEB_HOST`（默认 127.0.0.1）、`TCER_WEB_PORT`（8787）、
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
| GET | `/api/filters` | Bearer | 筛选下拉数据 → `{persons,projects,models}` |
| GET | `/api/series` | Bearer | 时间序列 → `{series:{组:[[ts,值],…]}}` |
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

> ⚠️ 客户端当前 `report_row_dict` 尚未导出 `started_at`/`ended_at`，落地上传前需补上，否则明细点会全部落到同一上传时间。详见 `PLAN-client-upload.md`。

### `/api/series` 参数

- `dimension`：`person` | `project` | `model`（分组维度，对应三张图）
- `metric`：`tcer`（默认）| `ctei` | `cost_usd` | `net_loc` | `total_tokens` | `churn_ratio` | `chr` | `read_before_write` | `search_edit_ratio` | `tool_error_rate`
- `persons` / `projects` / `models`：逗号分隔多选过滤
- `start` / `end`：epoch 秒时间范围
- 优先返回 session 级数据点；无匹配 session 时回退到 aggregate。

## 仪表盘

顶部筛选：人员 / 项目 / 模型（均多选）+ 时间范围 + 指标切换。下方三张按维度分组的曲线图（横轴时间）。第一版默认指标为 TCER。

## 建议纳入的其他指标

第一版已把指标切换做成通用的，`/api/series` 的 `metric` 已支持以下已入库字段。建议优先关注：

1. **CTEI 综合效率分** — 项目的核心综合评分，最能反映整体效率走势。
2. **千行代码成本 CPE / 总成本** — 成本维度，管理视角最关心。
3. **返工率 churn_ratio** — 质量维度，异常升高值得预警。
4. **净增行 net_loc / 总 Token** — 产出规模，做人均/项目对比。

二级下钻指标（已入库，`metric` 参数可直接切换，字段名与客户端 `report_row_dict` 一致）：
**缓存命中率 CHR**（`chr`）、**先读后写率**（`read_before_write`）、**搜索后编辑比**（`search_edit_ratio`）、**工具错误率**（`tool_error_rate`）—— 更细的行为质量维度。

> 客户端对齐：上述指标与主指标字段名均与 `report_row_dict` 一致，无需改造；唯一待补的是 `started_at`/`ended_at`（时间轴）。详见 `PLAN-client-upload.md` 第 7 节。