# TCER v1.5.1

修复 macOS 上栏按钮显示为白色（Aqua 主题 bug）+ 标题栏跟随系统深色。

## macOS：按钮全白修复

- **问题**：macOS Tk 的 Aqua 主题忽略 `tk.Button` 的 `bg`（[bpo-44243](https://bugs.python.org/issue44243)），导致 `flat_button`（上栏刷新 / 工具 ▾ / 导出 ▾ / 上传 / 预设，以及各弹窗的校准 / 取消 / 前往下载等按钮）在 mac 上显示为白色，与深色界面冲突。
- **修复**：`flat_button` 在 mac 上改用 `_MacButton`（`tk.Label` 子类 + 点击/hover 绑定）绘制，绕过 Aqua 主题，bg/fg/hover 全可控。视角切换按钮早已用此法（故一直正常），现推广到全部 `flat_button`。
- **API 兼容**：`_MacButton` 兼容 `tk.Button` 的 `command`（构造传入与 `.config(command=)` 重绑），菜单按钮（工具 ▾ / 导出 ▾）的弹窗逻辑不受影响。
- **Windows / Linux 不变**：仅 `PLATFORM == "darwin"` 走 `_MacButton`，其余平台仍是原 `tk.Button`。

## macOS：标题栏跟随系统

- mac 标题栏由系统绘制、跟随系统外观（mac 深色模式 → 深色标题栏），无需代码干预。纯标准库 Tk 无法做到「系统浅色时也强制深」（需 pyobjc NSWindow，违背零依赖），故保持跟随系统。

## 说明

- 客户端纯离线、零第三方依赖不变；指标公式不变。
- GUI 冒烟测试 36 项全过（新增 `_MacButton` 的 command 兼容测试）。

---

**完整变更**：`v1.5.0...v1.5.1`
