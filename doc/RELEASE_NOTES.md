# TCER v1.5.4

价表快速跟进：新增 Grok 4.6 与 Gemini 3.7 Flash、修正 Grok 4.5 缓存价、补 DeepSeek V4 Flash 别名。纯数据更新，GUI 与指标公式不变。

## 新增模型

- **Gemini 3.7 Flash**（`gemini-3.7-flash`）：Google 昨日发布的最强 Flash 档。本地按**官方介绍价**先行补录——input $0.75 / output $3.75 / 缓存读 $0.075（2026-12-31 前生效；2027-01-01 起转标准价 $1.50 / $7.50 / $0.15，已在价表条目注明）。
- **Grok 4.6**（`grok-4.6`）：input $2 / output $6 / 缓存读 $0.5（cc-switch 上游 7dc0a72 同步）。
- **DeepSeek V4 Flash 别名**（`deepseek-v4-flash-0731`）：与 `deepseek-v4-flash` 同价（0.14/0.28/0.0028）。

## 价格修正

- **Grok 4.5** 缓存读 $0.5 → **$0.3**（上游修正，与 `grok-4.5-build` 看齐；此前 Grok 源会话的缓存读成本略被高估，本次起更准）。

## 说明

- 价表共 194 条（cc-switch 上游 191 + 本地保留 `big-pickle` / `opencode/big-pickle` + 本地先行 `gemini-3.7-flash`）。
- 上一版疑点撤销：`deepseek-reasoner` 调价后与 chat 同价不是上游笔误——正是 DeepSeek V4 统一定价，本版新增的 `0731` 别名佐证。
- 客户端纯离线、零第三方依赖不变；指标公式不变。

---

**完整变更**：[`v1.5.3...v1.5.4`](https://github.com/yqlizeao/TCER/compare/v1.5.3...v1.5.4)
