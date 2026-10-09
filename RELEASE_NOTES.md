# TCER v1.9.7 更新说明

**2026-10 前沿模型库全面同步与看板零消耗“幽灵模型”根因修复**

---

## 一句话总结

全面同步 2026-10 上游与各大厂商最新前沿模型价格（Claude Haiku 5.5 大降价、GPT-6.1 Sol / GPT-5.6 Cyber / Daybreak、Qwen 3.8 / MiMo 2.6 等，价表扩充至 340 款），并彻底根除 OMP/Pi 数据源在切换模型或 400 报错时因 0-token 记录导致的看板“幽灵模型”显示缺陷。

---

## 核心更新

### 1. 前沿模型价表与常用生态全面同步（扩充至 340 款）

- **关键模型官方调价跟进**：
  - **Claude Haiku 5.5**：同步 2026-10-07 官方大降价，更新为 `$0.1 / $0.5 / $0.01 / $0.125`（非缓存输入 $0.1、输出 $0.5，比旧标价降幅 80%）。
  - **GPT-6 Sol / Luna**：同步短上下文官方标准价（Sol: 2.0 / 10.0 / 0.20 / 2.50；Luna: 0.10 / 0.50 / 0.01 / 0.125）。
  - **GPT-5.6 Terra / Luna**：跟进官方 20% / 80% 降价生效值（Terra: 2.0 / 12.0 / 0.20 / 2.50；Luna: 0.20 / 1.20 / 0.02 / 0.25）。
- **新增 2026 前沿高频活跃模型**：
  - **OpenAI**：`gpt-6.1-sol`（及其基座别名 `gpt-6.1`，0.05× 缓存读）、`gpt-5.6-cyber`（Daybreak 网安模型）、`gpt-daybreak-blue-latest`、`gpt-daybreak-red-latest`。
  - **通义千问**：`qwen3.8-2.4t-a95b`、`qwen3.8-27b`、`qwen3.8-omni-flash`、`qwen3.7-flash`。
  - **小米**：`mimo-v2.6-pro`、`mimo-v2.6-flash`、`mimo-v2.6-pro-ultraspeed`。
  - **其他生态**：`grok-4.7`（xAI）、`step-5-preview`（阶跃星辰）、`hy4-preview` 及 `hunyuan-hy4-preview`（腾讯混元）、`glm-5.3-flashx`（智谱 AI）、`longcat-2.0`（美团）。
- **官方挂牌价核对与修正**：
  - **DeepSeek V4 Pro**：官方撤回迁移路由公告后，恢复并锁定高峰档官方标准价 `1.32 / 3.96 / 0.044 / 0.0`。
  - **DeepSeek Chat / Reasoner**：对齐上游标准价 `0.44 / 1.32 / 0.014 / 0.0`。
  - **o3-mini / o1-mini**：更正为标准挂牌价（o3-mini: 1.10 / 4.40 / 0.55；o1-mini 缓存读: 0.55）。
  - **MiMo V2.5**：输出单价微调修正为 0.28。
  - 全量新条目均注入所属厂商 `vendor` 并满足全角标点注释规范。

---

### 2. 彻底修复会话看板“幽灵模型”（Ghost Model）显示缺陷

- **缺陷背景**：在 OMP/Pi 会话中，当用户在界面切换过模型、或首回合因 API 密钥等原因返回 400 报错（0 Token 消耗）随后切换至新模型时，“模型使用详情”弹窗与卡片均正确统计单一主模型，但“会话概况”中的模型一栏却会显示两个模型，出现“隐身/幽灵”模型。
- **解析层根因消除（`omp_reader.py`）**：`model_change` 事件仅记录待命模型，不再提前将其注入 `u.models`；仅在助手回合实际产生非零 Token（`_add_turn_usage` 成功入账）时才计入会话模型列表（全空会话安全保留待命模型作为兜底）。
- **展示层收口（`format.py`）**：`models_label()` 增强过滤，当会话已产生实际 Token 时，优先且仅展示真正有消耗的模型，杜绝任何零消耗占位/报错模型泄露至看板或 HTML 报告。

---

## 质量与审计

- **自动化测试**：全量 797 项测试全部通过（`python -m pytest tests/`，0 failures）。
- **新增回归护栏**：
  - `tests/test_pricing.py` 新增 `test_upstream_synced_models_2026_10`。
  - `tests/test_omp_reader.py` 新增 `test_zero_token_error_model_not_in_models`。
- **真实会话审计**：48 个本地项目闭环审计全部通过（`python -m tcer.audit --ci`，PASS）。

---

# 历史版本

## TCER v1.9.6 更新说明

**支持选定周期生成 AI 研发周报并归档至 LLM 报告**

- 核心层：实现 `weekly.py` 周期会话事实提取、自然周/工作日解析与双模报表生成。
- 呈现层：扩展 `LlmReportsView` 支持 weekly 类别，导出菜单顶层挂载入口。
- 交互层：实现自适应四行周报弹窗，选项高对比指示，去除括号与冗余状态。
- 组件防御：`flat_button` 自动补全 `compound='left'` 杜绝文字吞没。

## TCER v1.9.5 更新说明

**全源新版探查（Claude Code 2.1.283 / OMP 18.x / AGY / Codex）与 2026 前沿模型库升级**

- 修复 Claude Code 2.1.283 头部采样致思考档位漏采问题与顶层 plan 模式流转捕获。
- 修复 OMP 18.x `reasoningTokens` 键名导致思考 token 归零缺陷。
- 全量模型注入厂商名（`vendor`）并将价格备注进化为标准化模型定位描述。

## TCER v1.9.4 更新说明

**团队术语库与语义判定层 (SharedBrain MVP)**

- 在效率度量之外新增第 7 页签「术语库」，构建团队黑话与跨职能语义对齐层。
