# TCER v1.5.3

价表同步至 cc-switch v3.19.2：新增 9 个模型、调整 5 个模型价格。纯数据更新，GUI 与指标公式不变。

## 价表同步（cc-switch v3.19.2，189 条）

**新增 9 个模型**：

- Claude：`claude-opus-4-6`、`claude-sonnet-4-6`（短名，此前仅有带日期全名）
- GPT：`gpt-5.3-codex-spark`
- Gemini：`gemini-3.5-flash-lite`
- Kimi：`kimi-k2.7-code-highspeed`
- GLM：`glm-5-turbo`、`glm-5v-turbo`
- Qwen：`qwen3.8-max`、`qwen3.6-flash`

**调整 5 个模型价格**（均为降价）：

- `gpt-5.6-terra`：input 2.5→2、output 15→12
- `gpt-5.6-luna`：input 1→0.2、output 6→1.2（回归轻量档定位）
- `deepseek-chat`：input 0.27→0.14、output 1.1→0.28
- `deepseek-reasoner`：input 0.55→0.14、output 2.19→0.28
- `minimax-m3`：input 0.6→0.3、output 2.4→1.2

## 说明

- 价表共 191 条（cc-switch 189 条 + 本地保留 `big-pickle` / `opencode/big-pickle`）。
- 短名 `claude-opus-4-6` / `claude-sonnet-4-6` 进价表后走精确匹配（优先级最高），不再依赖反向前缀推导。
- 客户端纯离线、零第三方依赖不变；指标公式不变。
- ⚠️ `deepseek-reasoner` 调价后与 `deepseek-chat` 同价，疑似上游笔误，已按 SSOT（cc-switch 为准）照抄，欢迎反馈。

---

**完整变更**：[`v1.5.2...v1.5.3`](https://github.com/yqlizeao/TCER/compare/v1.5.2...v1.5.3)
