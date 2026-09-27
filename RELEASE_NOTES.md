# TCER v1.9.5 更新说明

**全源新版探查（Claude Code 2.1.283 / OMP 18.x / AGY / Codex）与 2026 前沿模型库升级**

---

## 一句话总结

全面探查并修复了 Claude Code 2.1.283 与 Oh My Pi 18.x 最新版的数据解析漂移，重点收录 2026 年最新前沿生态旗舰模型（Claude Opus 5.5 / GPT-6 / Gemini 3.8/3.9 / Grok 5 / DeepSeek V4.5 等，模型库扩充至 322 款），全量模型显式录入厂商名（Vendor）并将价格备注进化为标准化模型定位与能力描述。

---

## 核心更新

### 1. 多编程助手最新版本实机探查与漂移修复

- **Claude Code 2.1.283**：
  - **头部采样深度修正**：2.1.283 在会话首部密集注入环境快照附件（`attachment`），导致第一条 `assistant` 行后移，原先采样前 20 行漏采了思考档位；已将头部采样提升至 `head_n=60`，实机精准恢复 `reasoning_effort`（`"medium"`/`"high"`）。
  - **顶层 Plan 模式捕获**：支持 2.1.283 新增的独立顶层模式流转事件 `{"type": "mode", "mode": "plan"}`，避免计划模式切换被遗漏。
  - **消除模型降级误判**：彻底解决 2.1.283 主力模型 `claude-opus-5-5` 因缺失而前缀回退到旧版 `claude-opus-5` 的重大偏差，按官方实价 $4/$20/0.2 精准计费。
- **Oh My Pi (omp/18.3.4)**：
  - **推理 Token 漏采修复**：实机探查捕获最新 OMP 的 `reasoningTokens` 键名，解决此前仅读取 Pi 上游 `reasoning` 导致的思考 token 归零缺陷（实测单会话追回 6.1 万思考 token）。
  - **源能力对齐**：在 `metric_defs._SOURCE_SUPPORT` 中将 `omp` 纳入 `reasoning_tokens` 与 `reasoning_ratio` 支持集合。
- **Google Antigravity (agy 1.2.11) & Codex CLI (0.157.1)**：
  - 完成实机数据库（SQLite `gen_metadata`、`steps`）与增量差分日志回放验证，4,100 万 token 会话与 13.2 万 reasoning token 100% 闭环无误。

---

### 2. 2026 前沿模型库全面升级（322 款模型覆盖）

- **全量模型注入厂商名（`vendor`）**：
  - 库内所有模型全部规范标注厂商属性（`Anthropic`、`OpenAI`、`Google`、`xAI`、`DeepSeek`、`Alibaba`、`Zhipu AI`、`Moonshot AI`、`MiniMax`、`Mistral AI`、`Meta` 等）。
  - `pricing.py` 新增 `pricing.vendor(model) -> str` 接口；GUI 模型对比卡片表头新增厂商小徽章，Tooltip 悬浮呈现 `[厂商名] 模型名 · 官方标价`。
- **重点收录 2026 年最新前沿生态旗舰模型**：
  - **Anthropic 2026-09 最新 Claude 5.5 家族**：`claude-opus-5-5`（2026-09-22 发布的最新旗舰，实测实机主力，定价 $4/$20/0.2/5）、`claude-opus-5-5-20260922`、`claude-sonnet-5-5`（$1.5/$7.5）、`claude-haiku-5-5`（$0.5/$2.5）。
  - **OpenAI 2026 前沿 GPT-6 系列与 Codex 6 代**：`gpt-6-astra`（1.05M 上下文前沿智能体旗舰）、`gpt-6-sol`（深度推理旗舰）、`gpt-6-luna`、`gpt-6-terra`、`gpt-6-pro`、`gpt-6-codex`、`gpt-6-codex-spark`、`o4`。
  - **Google 2026 前沿 Gemini 3.8 / 3.9 系列**：`gemini-3.8-flash`、`gemini-3.8-flash-high`、`gemini-3.8-pro`（全模态深度推理旗舰）、`gemini-3.8-ultra`、`gemini-3.9-pro`、`gemini-3.9-flash`。
  - **xAI 2026 前沿 Grok 5 系列**：`grok-5`、`grok-5-thinking`（原生深度思考链与多智能体协同）、`grok-4.6-build`、`grok-4.6`。
  - **DeepSeek 2026 前沿进阶系列**：`deepseek-v4.5`、`deepseek-v4.5-pro`、`deepseek-r1`、`deepseek-v4.1-flash/pro`。
  - **阿里巴巴通义千问 2026 开源前沿**：`qwen4-coder`（全工程级自主代码重构旗舰）、`qwen4-max`、`qwen3.9-max`、`qwen3.9-flash`。
  - **其他前沿主力**：智谱 `GLM-5.5` / `GLM-5.5-Flash`、月之暗面 `Kimi K3.5` / `Thinking`、Meta `Llama 4 Scion` / `Maverick`、`MiniMax M3.5` / `Highspeed` 等。

---

### 3. 模型价格备注（`_note`）进化为标准化模型定位描述

- 告别零散口语化碎句，所有前沿模型均赋予官方级综合定位描述（厂商定位 + 核心适用场景 + 上下文/思考特征 + 特殊计费口径）。
- **标点与语法绝对统一**：条目内多维度说明一律使用全角分号 `；` 分隔；引导词统一使用全角冒号 `：`；补充说明统一使用全角括号 `（）`；货币统一使用 `$`。
- 完整兼容既有测试断言关键字，无缝保障自动化与审计流程。

---

## 质量与审计

- **自动化测试**：全量 756 项测试全部通过（`python -m pytest tests/`，0 failures）。
- **实机闭环审计**：45 个真实本地项目闭环审计全部通过（`python -m tcer.audit --all-projects --skip-empty --top 1 -q --summary-json -`，0 check failures）。

---

# 历史版本

## TCER v1.9.4 更新说明

**团队术语库与语义判定层(SharedBrain MVP)**

- 在效率度量之外新增第 7 页签「术语库」，构建团队黑话与跨职能语义对齐层。
- 详细说明请参见历史 Git 提交记录。
