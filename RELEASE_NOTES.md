# TCER v1.0.19

适配 Codex Desktop（cli 0.146+）新会话格式：修正该版本会话导入后「代码产出与质量」指标全为 0 的问题。核心 TCER / CTEI / 成本口径不变。

## 修复

### Codex Desktop cli 0.146+：LOC 与工具占比归零

新版 Codex Desktop 改了 JSONL 结构，`codex_reader` 两处解析失配，导致该来源会话的净增行/写入/删除/涉及文件/测试行/文档行、探索占比、Bash 占比全部显示 0（Token 侧指标不受影响）。

- **LOC 归零**：文件编辑不再记为 `response_item` 的 `apply_patch` 调用，改为只出现在 `event_msg → patch_apply_end.changes[path]`（`update`→`unified_diff`、`add`/`delete`→`content`）。旧 `_loc_scan` 只认  `apply_patch` response_item → 无 LOC 信号。
  - 处理：`_loc_scan` 改双来源。旧 rollout 同时带 `apply_patch` response_item 与 `patch_apply_end` 结果事件，**优先 response_item，仅当无可解析 apply_patch 时回退 `patch_apply_end`**，避免双计；失败的 patch（`success:False`）跳过。
- **工具占比归零**：所有工具改走 JS 壳 `custom_tool_call name="exec"`，真实命令埋在 `input` 的 `tools.shell_command({command:"..."})` 里 → 全部塌进单一 `exec` 桶，探索/Bash 占比失真。
  - 处理：`_classify_tool` 识别 `exec`，抽出内层 shell 命令后走共享的 `_classify_shell_command`（Grep/Glob/Read/Edit/Bash）。提取只做常规反转义，**不用 `unicode_escape`**，避免中文命令体 mojibake。

## 说明

- 仅新增/修正 Codex reader 的格式解析，不改 TCER/CTEI/成本公式；旧版 Codex 会话行为不变（回归套件验证）。
- 真实文件验证：原先全 0 的会话现正确显示写入/删除 77/49、净增 28、涉及文件 3、测试行 46、工具 Grep 15 / Bash 21 / Read 4。
- 新增 5 个回归测试覆盖新格式 LOC、失败 patch 忽略、新旧格式不双计、exec 分类、UTF-8 命令不损坏。

---

**完整变更**：`v1.0.18...v1.0.19`