# 发版与运维（release-ops）

> 发版必须手写 `RELEASE_NOTES.md` 详细中文说明，不能只丢提交记录（见仓库根 RELEASE_NOTES.md）。

> 本节自 CLAUDE.md 迁移而来（2026-10 瘦身）：CLAUDE.md 只保留一行硬规则，完整细节、实测数据与历史缘由在此。条目前缀「原 #N」= 瘦身前 CLAUDE.md 的「关键注意事项」编号，可据此追溯。

<a id="update-config"></a>
### 原 #30 检查更新 / 直接更新 / 配置存储

- （opt-in 联网 + 绿色便携）：
  - ①**配置存储** `core/app_dirs.prefs_dir()` 统一 TCER 自有配置（`tcer_ui.json` / `tcer_upload.json`），发布版（`sys.frozen`）优先 **exe 同目录**（便携，删 exe 即净）、不可写回退 `~/.tcer/`、源码运行用 `~/.tcer/`，**不再用 `~/.claude/`**（那是 Claude Code 的目录），旧位置 `~/.claude/tcer_*.json` 首次启动自动迁移；
  - ②**检查更新** `core/update_check` 纯标准库查 GitHub Release latest（失败静默返回 `None`）+ `render_notes` 清理 markdown 供 Tk 展示（去 `##`/反引号/粗体，列表转 `•`），opt-in 联网仅查公开 Release、**不发送用户数据**（区别于上传 #3），启动检查默认值随运行形态（发布版 `frozen` 默认开、源码默认关），用户在「工具」菜单切（存 `ui_prefs['check_update_on_start']`）；
  - ③**直接更新** `core/updater`（仅发布版 `frozen`）：`asset_for_current_platform` 选包 + `download` 流式下载（进度回调）+ `apply_and_restart` 平台替换（Windows 生成 `updater.bat` 轮询删旧 exe→move 新→重启→自删，`mbcs` 编码兼容中文路径、绕过运行中 exe 锁；mac 覆盖二进制 + `xattr` 清 quarantine + 重启），**调用后须立即 `SystemExit`**；`UpdatePopup` 发布版且有平台包时显示「立即更新」（`controller.start_self_update` 后台下载，进度经 `root.after` 回主线程），否则回退「前往下载」。

## 历史与教训（发版）

- **配置不再放 `~/.claude/`**：那是 Claude Code 的目录；发布版配置放 exe 同目录（便携），不可写回退 `~/.tcer/`；旧位置首次启动自动迁移。
- **opt-in 联网只查公开 Release、不发用户数据**，与上传（#3 例外）严格区分；启动检查默认值随运行形态。
- **自更新调用后必须立即 `SystemExit`**；Windows 用 `updater.bat` 绕过运行中 exe 锁，`mbcs` 编码兼容中文路径。
- **`launch.bat` 文件为 GBK 编码**以保 cmd 中文注释，编辑时别转成 UTF-8。

