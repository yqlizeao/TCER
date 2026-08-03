# TCER v1.0.18

修正上个版本「输出吞吐（output_tps）」在 Claude 数据源下系统性偏低的问题。核心 TCER / CTEI / 成本口径不变。

## 修复

### 输出吞吐：Claude 改为「不适用」

v1.0.17 对 Claude 会话把**轮级墙钟**当成生成耗时做分母，导致数值虚低 3–10 倍（实测 omp 控制台 21.3 字/秒的同类会话，Claude 侧只显示个位数）。

- 根因：Claude 的 JSONL 只记录 `turn_duration`（一整个用户轮次的墙钟，含多次 API 补全 + 工具执行 + 用户等待，单轮 `messageCount` 可达 400+），**没有单次 API 补全的生成耗时**。业界标准（Artificial Analysis / vLLM / TGI）的输出速度定义是 `输出 Token ÷ 解码时间`，排除 TTFT 与工具/等待——Claude 离线日志无法还原该口径。
- 处理：`output_tps` 支持数据源收窄为 **Codex / Grok / Oh My Pi**（三者各自上报单次补全生成耗时）。**Claude / OpenCode / Pi 显示「不适用」**，不再给出误导性的偏低值。
- 同步修正指标提示文案与 `CLAUDE.md` 中对 `turn_duration` 语义的错误描述。

## 说明

- 仅指标口径修正，不涉及 reader token/LOC 采集；闭环审计口径不变。

---

**完整变更**：`v1.0.17...v1.0.18`