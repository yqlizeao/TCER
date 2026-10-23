# AGENTS.md

本仓库（TCER：基于真实 AI 编程助手会话数据的 Token-to-Code 效率计量）的**工程手册是 [CLAUDE.md](CLAUDE.md)**（常驻不变量与规范），详细专题在 [doc/](doc/)。本文件只列最关键的铁律，请先读 CLAUDE.md 再动手。

## 最关键的铁律

1. **优先级：第一是数据的真实与完整性，第二是性能。** 优化不得改变数值结果；拿不准的数显示「不适用」，不要编造。
2. **纯离线 + 零依赖**：纯 Python ≥3.11 标准库；不依赖 git、不联网。仅有两处用户显式 opt-in 的联网例外（上传、LLM 解读），每次数据出境必须弹确认。
3. **按 message.id 去重**（Codex 对累计量做差分，resume 快照只去重不求和）；LOC 只来自会话内工具调用回放，且只回放成功（非 `is_error`）的调用。
4. **指标唯一真理源是 `TCER/gui/metric_defs.py`**；评分是「综合效率分 v2」（三正交轴，旧 CTEI 已移除）；比率聚合一律「分子和÷分母和」重算，不取平均。
5. **改 `reader` / `analyze` / `loc` / 计价后必须跑闭环审计**：`python -m tcer.audit`（真实本地会话 vs 原始 JSONL 重算），并跑 `python -m pytest tests/`。
6. **库层谨慎**：`TCER/core/` 有完整测试覆盖；新增字段必须同步 `TokenUsage.merge`、CSV 字段表与 `_SOURCE_SUPPORT`（护栏测试会失败）。
7. **GUI 约定**：界面全中文；按钮用 `widgets.flat_button`、菜单用 `FlatMenu`、禁 emoji 图标；样式规范见 [doc/style.md](doc/style.md)，踩坑见 [doc/gui-notes.md](doc/gui-notes.md)。
8. **禁止中间产物**：临时脚本/截图/草稿用完即删，不提交；经验写入 CLAUDE.md 或 doc/ 对应专题文件。

运行：`python -m TCER`（绿色免安装）。
