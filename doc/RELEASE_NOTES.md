# TCER v1.4.0

客户端上传配置迁移到 `tcer_ui.json`（单一配置源、弹窗内可编辑、按钮默认常驻）+ 修复 omp/pi advisor 审查消息误计入用户消息指标 + 服务端飞书 OAuth 登录。

## 客户端上传（配置迁移 + 可编辑）

- **配置移入 `tcer_ui.json` 的 `upload` 段**：服务器地址 / Auth Token / 是否附带明细不再走 `.env` 环境变量，集中到与界面偏好同一份 `tcer_ui.json`，随整体包分发、随包覆盖。
- **弹窗内可编辑**：上传对话框新增服务器地址、Auth Token、明细开关的编辑表单；点「立即上传」时先保存配置再上传，无需手改文件。
- **上传按钮默认常驻**：不再以「是否配置了 URL」作为显隐开关；未配置服务器地址时上传前提示填写。
- **移除硬编码地址**：`DEFAULT_URL` 留空，开源库不内置任何具体服务器地址。
- 删除 `env_config.py` 与客户端 `.env` 配置段（`.env.example` 仅保留服务端配置模板）。

## 修复：omp / pi advisor 审查消息

- omp/pi 的 advisor（审查模型）审查 prompt 以 `role:"user"` 写入其 transcript 并被折叠进主会话，此前误计入「用户消息」指标、且会出现在用户消息弹窗。
- 现按 omp 源头的权威标记（`synthetic` / `attribution:"agent"`）识别并排除：不计入 `user_msgs`、slash/纠正/首条长度信号、图片输入，也不出现在用户消息弹窗与会话视图。advisor 的 token 成本仍如实保留（真实花费）。

## 服务端：飞书 OAuth 登录

- 新增飞书登录（opt-in）：`TCER_LOGIN_MODE`（password / feishu / both）+ `TCER_FEISHU_APP_ID` / `APP_SECRET` / `REDIRECT_URI`；未配置时功能休眠、零联网。
- Auth Token 页文案统一，复制按钮兼容 HTTP 非安全上下文（`execCommand` 回退，修复「点了复制没反应」）。

## 说明

- 客户端纯离线、零第三方依赖不变；综合效率分公式核心不变。
- 全套 504 passed, 1 skipped。

---

**完整变更**：`v1.3.0...v1.4.0`