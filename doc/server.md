# server 端约定（server）

> 完整的概念对齐设计见 [concept-alignment-server.md](concept-alignment-server.md)；本文收录 server 聚合口径与概念模块的硬规则。

> 本节自 CLAUDE.md 迁移而来（2026-10 瘦身）：CLAUDE.md 只保留一行硬规则，完整细节、实测数据与历史缘由在此。条目前缀「原 #N」= 瘦身前 CLAUDE.md 的「关键注意事项」编号，可据此追溯。

<a id="aggregation"></a>
### 原 #29 server 聚合口径与桌面端一致

- `server/backend/db._agg_metrics` 的比率一律「分子和 ÷ 分母和」重算（返工率按 `code_added` 加权、工具错误率按工具调用数加权），**不取算术平均**（否则 5k 与 2M token 的会话等权，触发 Simpson 悖论）；无分母的比率（先读后写率/搜索后编辑比，分母来自工具时序、库里没有）退化为中位数并由 `_stat` 标注。
  - **聚合综合效率分按规则 9 重算**（从聚合 TCER/CPE/质量子信号重算三轴，与 audit 的 `aggregate_score_recompute` 同一口径），不对各会话分数取平均。决策实验室（`server/backend/analysis.py`）在此之上做「任务类型 × 会话规模」分层 + bootstrap 区间 + 证据分级，区间跨 0 一律不排名。

<a id="concepts"></a>
### 原 #41 server 团队概念对齐（`server/backend/concepts/`，设计见 [doc/concept-alignment-server.md](concept-alignment-server.md)）

- 客户端铁律不变，只加上传授权位 `upload_config.semantic_consent`（默认关，payload 仅 `detail` 且实名时置位，服务端对匿名再兜底忽略）。server 端纯标准库：
  - ①**授权门**——`pipeline` 只处理 `uploads.semantic_consent=1` 且带 conversation 的 session 行，撤回即按 `origin_ref` 清除全部派生表；
  - ②**术语匹配复用客户端** `termbase.find_terms_in_text`（server 与客户端同一套规则，勿另写），术语库 `concept.entry_json` 与 `TermEntry.to_dict()` 同构；
  - ③**披露层唯一出口** `disclosure.py`（管理视图 <5 人不出数、候选新词 ≥3 人 ≥2 项目才公开、公共证据只取 public 会话且不带作者）；
  - ④**个人视图按登录身份**，不接受 person 参数，admin 也不能越权（`rbac.can_view_personal` 是唯一判定点）；
  - ⑤**红线 R1**：概念模块不读效率指标列（测试钉住）；
  - ⑥新词挖掘必须保留**邻接字多样性过滤**（否则 CJK n-gram 产出大量跨词碎片），句首/句尾每次算一种邻接但至少一侧要有真实邻接字；
  - ⑦时间窗以数据最新时间为锚。改动后跑 `tests/test_server_concepts.py`，并可用 `seed_concepts.py` 造数走查。

## 补充（旧 AGENTS #29）

> 以下条目来自旧 AGENTS.md（CLAUDE.md 的较早分叉，2026-10 已改为短索引文件）。其中 CLAUDE.md 原本没有的内容迁入此处；与 CLAUDE.md 重复的条目以后者为准，不再重复收录。

- 服务器无逐会话任务类型 → 聚合综合效率分用聚合 TCER 作产出轴代理，`_stat` 标注 `recomputed (tcer-proxy)`，与 audit 的 `aggregate_score_recompute` 同源。
- schema 列为 `score`/`tier`（旧 `ctei` 列已废弃）。

## 历史与教训（server）

- **聚合口径与桌面端一致**：比率「分子和÷分母和」，不取算术平均；聚合综合效率分按规则重算，不对各会话分数取平均；决策实验室区间跨 0 不排名。
- **授权门**：语义分析只处理 `semantic_consent=1` 且实名的会话，撤回即清除派生表；匿名由服务端再兜底忽略。
- **概念模块不读效率指标列**（红线 R1，测试钉住）；个人视图只按登录身份，admin 也不能越权。
- **新词挖掘必须保留邻接字多样性过滤**，否则 CJK n-gram 产出大量跨词碎片。

