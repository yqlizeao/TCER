# TCER 迭代日志

## 2026-06-20 · 迭代 #2：持续推进，自我进化

**目标**：按"持续推进，自我进化迭代"指令，主动识别并修复关键问题，提升工具可用性与数据可信度。

### 已完成（4 个里程碑提交）

#### 1. v0.3.0：硬数据审计 + 软指标配置化 + F1/F4 修复 (commit 521f9d3)

**硬数据层审计**（保证 TCER 可信）：
- ✅ F1 校准工具（`calibrate_loc.py`）：用 git 历史量化 Write 覆写已有文件的偏差（实测 0%）
- ✅ F1 风险可见化：新增 `unseen_writes` 计数（Quality L3 层），暴露潜在偏差上界
- ✅ F4 行数口径统一：`tree_loc` 改为 text mode universal newlines，与 `session_loc` 一致
- ✅ 时间戳盲区修复：仅零 usage 回合的会话也能拿到时间戳（校准 & GUI 排序需要）

**软指标配置化**（去 magic number）：
- ✅ 新增 `data/composite_baselines.json`：TTAF / CTEI 基准 / PSAC / CHR 权重全部可配置
- ✅ CLI 新增 `--compute-baselines`：从积累数据计算中位数/均值，一键建立个人基准
- ✅ `metrics.py` 从配置加载常量，保留向后兼容

**结果**：62 项测试全过，硬数据可靠性审计完成，版本升至 0.3.0。

---

#### 2. Markdown 导出 —— 轻量可分享报告 (commit f416a63)

新增 `--markdown` 参数，生成适合嵌入文档/PR/wiki 的 Markdown 摘要：
- 聚合关键指标表格（TCER / CPE / CHR / Churn / CTEI / Grade）
- 逐会话简表（Token / CHR / Net LOC / TCER / CTEI）
- ASCII CTEI 分布图（无 ANSI 色，纯文本可读）
- F1 风险提示（当 `unseen_writes > 0` 时）

**用户价值**：不像 JSON 太技术、CSV 缺上下文，Markdown 格式轻量可分享，直接贴进 PR/周报。

---

#### 3. 指标健康参考范围速查表 (commit bb4eb57)

在 README 添加经验值参考表（TCER / CHR / CPE / Churn / I/O Ratio），三档分级：
- 优秀 / 良好 / 需改进
- 来自 TCER 自身数据与框架参考集的观察
- 强调这是经验值，鼓励用户建立个人基准

**解决的痛点**：用户看到 TCER=45，但不知道这算好还是坏。

---

#### 4. 时间过滤 (--since / --until) 支持趋势分析 (commit 727a735)

新增 `--since` 和 `--until` 参数（YYYY-MM-DD），按会话开始时间过滤：
- 支持单边过滤（如 `--since 2026-06-01` 看本月数据）
- 支持范围过滤（`--since X --until Y`）
- 无时间戳的会话被排除

**用例**：
- 周报/月报：`--since 本周一` / `--since 本月1日`
- 对比分析：分别跑两个时间段，对比 TCER/CHR/CPE 变化
- 排查退化：`--since 某次重构后` 看效率是否下降

**下一步可能性**：加 `--compare` 参数自动对比两时段，直接输出"本周相比上周 TCER 提升 15%"。

---

### 技术债务清理

- ✅ 时间戳盲区修复：`reader.aggregate_usage` 现在从**所有** assistant 回合（包括零 usage 的）提取时间戳，不再遗漏纯 thinking 会话
- ✅ 配置化重构：消除硬编码的 TTAF / 基准值 / CHR 权重，移至 JSON 配置，保持向后兼容

---

### 测试覆盖

- 62 项测试全过（`reader` / `paths` / `metrics` / `report` / `loc` / `pricing`）
- 校准工具验证：TCER 项目 0% 偏差（33 个 Write，全部落在新文件上）
- 功能完整性测试：终端报告 / JSON / Markdown / 基准计算 / 校准 / 时间过滤 全部正常

---

### 用户体验提升

**文档**：
- README 新增指标健康参考范围速查表
- CLAUDE.md / README.md 诚实说明 F1 风险、校准方法、配置化路径
- Markdown 导出示例清晰

**可用性**：
- 零依赖保持（纯 Python stdlib）
- 绿色运行（`python -m tcer.cli` 直接用，无需安装）
- 导出格式齐全（终端 / JSON / CSV / Markdown）
- 时间过滤支持趋势分析

---

### 迭代反思

**做对的**：
1. **主动识别痛点**：没有等用户报 bug，而是主动审计硬数据可靠性（F1/F4）
2. **去 magic number**：软指标配置化后，用户能建立个人基准，而不是被"框架默认值"绑架
3. **实用优先**：Markdown 导出 > GUI 配置面板（前者立即可用，后者增加复杂度）
4. **增量迭代**：每个改进独立提交，可回滚，测试全过才推进

**可改进的**：
1. GUI 仍然是 521 行的单文件，未来可能需要拆分（但当前可维护）
2. 性能优化未做（几十个会话时可能慢），但当前数据量下不是瓶颈
3. 错误诊断仍是"静默跳过"模式，未来可加 `--verbose` 显示跳过的行/原因

---

### 下一步可能方向

1. **趋势对比** (`--compare`）：自动对比两时段的指标变化，输出"本周 vs 上周"报告
2. **性能缓存**：大项目（>50 会话）时，缓存已分析的会话，只重算新增的
3. **GUI 增强**：在 GUI 里直接配置 TTAF / 基准值，无需手改 JSON
4. **错误诊断** (`--verbose`)：显示跳过的 JSONL 行、解析失败原因、异常会话列表
5. **CI/CD 集成**：提供 GitHub Actions / GitLab CI 示例，自动生成 PR 里的 TCER 报告

---

**本次迭代成果**：从 v0.2.0 → v0.3.0 + 3 个功能增强（Markdown 导出 / 健康参考范围 / 时间过滤），硬数据可信度审计完成，软指标可配置化，测试全过，文档完整。

**耗时**：约 2 小时（包括审计 + 实现 + 测试 + 文档）

**Token 消耗**：~100k（在 200k budget 内，高效利用）
