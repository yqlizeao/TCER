# TCER v1.5.2

macOS 改用标准 `.app` 包（双击运行）+ 发布资产名加版本号；Windows 不受影响。

## macOS：改用 .app 包（双击运行）

- **问题**：v1.5.1 及更早的 mac 包是裸 Mach-O 单文件（`TCER-macos-arm64`），浏览器下载丢失可执行权限 + 打上 quarantine，Finder 不当程序、丢给文本编辑器读二进制 → 报「文本编码 Unicode(UTF-8)不适用」。
- **修复**：CI 改用 `--windowed`（无 `--onefile`）生成 `TCER.app` bundle，用 `ditto` 打成 `.app.zip` 上传。下载解压后**双击 `TCER.app`** 即可运行（首次需「右键 → 打开」过 Gatekeeper，因为项目不做代码签名）。
- **自动更新**：`updater._mac_replace` 重写支持 `.app.zip`——下载 zip → `ditto -x` 解压 → 旧 `.app` rename 到 `.old`（不删运行中文件）→ 新 `.app` move 到位 → 清 quarantine → `open` 启动。同时保留裸二进制 fallback（旧式安装也兼容）。

## 发布资产名加版本号

- Windows：`TCER-windows-x64-v1.5.2.exe`（原 `TCER-windows-x64.exe`）。
- macOS：`TCER-macos-arm64-v1.5.2.zip`（原 `TCER-macos-arm64`）。
- 自动更新不受影响：[`asset_for_current_platform`](tcer/core/updater.py) 按「Windows `.exe` 结尾 / mac 含 `macos`+`arm64`」匹配，命名加版本号仍命中。

## ⚠️ mac 用户：从 v1.5.0 / v1.5.1 升级

- mac 包格式从「裸二进制」改成「`.app.zip`」，**旧版（≤1.5.1）的应用内自动更新无法完成这次格式迁移**（旧 updater 会把 zip 当裸二进制 copy → 损坏安装）。
- **请手动下载本次 `TCER-macos-arm64-v1.5.2.zip`，解压后替换旧文件**；此后新版 updater 懂 `.app.zip`，自动更新恢复正常。
- Windows 用户不受影响，应用内自动更新照常。

## 说明

- 客户端纯离线、零第三方依赖不变；指标公式不变。
- Windows GUI 冒烟测试通过；mac 的 `.app` 自动更新替换逻辑需实机验证（v1.5.2 → v1.5.3 那次自动更新）。

---

**完整变更**：`v1.5.1...v1.5.2`
