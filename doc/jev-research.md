# 调研：Jev — TypeSafe System One 判定模型深度剖析

> 目的：TCER v1.9.2 已接入 Jev 作「TypeSafe System One 判定引擎」（`core/typesafe_client.py`，
> 相空间收敛动力学报告的两阶段级联判定）。本文档对 Jev 做系统调研：官网发布文、官方手册
> （docs.typesafe.ai）、社区最佳实践雷达（awesome-jev-projects，358 个开源项目）、底层原理
> （RLCD / 并行采样 / 校准理论）与第三方批判性分析，并映射回 TCER 自身实践。
>
> 来源分层：**一手**＝typesafe.ai 官方博客 + docs.typesafe.ai 官方手册（Mintlify，`.md` 直读）
> + evals.typesafe.ai 官方基准站 + GitHub org（typesafe-ai）+ BusinessWire 官方新闻稿；
> **社区**＝logicrw/awesome-jev-projects 项目数据（`projects.json` 全量 358 项）+ OmniJev/awesome-jev
> 复现目录 + Hacker News 发布帖（1929 分/509 评论，经 Algolia API 全量提炼）；
> **二手**＝TechCrunch 独家采访 / DataCamp / TrueFoundry / flaviocopes / LangChain / dev.to /
> Latent.Space / jevai.wiki 社区 wiki 等。数字均标注出处；官方基准为**自评口径**，
> 第三方普遍未复测（见 §8），开源复刻现状见 §7.4。
>
> 检索工具：tavily（search/extract）/ fathomsearch（brave/duckduckgo/hackernews/grok/github/arxiv/
> semanticscholar）/ tinyfish（search / fetch_content 批量读页）/ firecrawl（firecrawl_map /
> developer_search / research 论文检索）/ web_reader / WebFetch（含 hn.algolia.com JSON API）。
> 调研轮次：第一轮 2026-09-21（官网/手册/生态全景）；第二轮同日（垂直深挖 + 关键事实交叉验证）；
> 第三轮同日（tinyfish 批量精读剩余 cookbook + firecrawl 开发者索引/论文检索 + 中文社区视角），
> 均已并入本版。时点：Jev 发布后第 6 天，早期访问阶段。

---

## 1. 一句话定位

**Jev 是一个不生成文本的判定模型**：输入「状态（state）+ 类型化问题（questions）」，
输出「类型化答案 + 校准概率 + 置信度」，供软件直接分支使用——官方口号是
「frontier-intelligence function call：unstructured state in, typed probabilistic decisions out」
（类型安全的前沿智能函数调用）。

| 维度 | LLM（GPT/Claude/Gemini） | Jev（System One） |
|---|---|---|
| 输出 | 自由文本字符串，需解析+校验才能进软件 | 预声明 schema 的类型化值，**结构上不可能**越界 |
| 采样 | 顺序自回归，逐 token 条件生成 | **并行采样**，全部答案一次查询产出 |
| 训练目标 | RLHF（人类偏好）/ RLVR（可验证奖励） | **RLCD**（校准决策） |
| 概率 | 被问也常过度自信、前后不一 | 逐字段校准概率 + 置信度 |
| 端到端延迟 | 3–329 秒（前沿模型） | **70–500ms**（实测均值 ~111ms / 多数 ~100ms） |
| 输入价 | $0.20–10 / MTok | **$0.042 / MTok** |
| 输出价 | ≈5× 输入价 | **免费**（"too cheap to meter"） |
| 失败模式 | 幻觉 / 拒答 / 形状不合法 | 不会形状不合法，**但可以自信地选错** |

来源：官方发布博客 + consistency_noul cookbook 实测（TypeSafe 单次 111ms、$0.000043/次，
vs claude-opus-4-8 reasoning 13886ms、$0.034275/次——**快 125 倍、便宜 805 倍**，TypeSafe 自测）。

**不能做的事**（设计上放弃）：写回复、产代码、解释推理过程。它只「判断」，不「写作」。
类比：**Code calculates. Jev judges. Reasoning models reason and generate.**
（代码负责计算，Jev 负责判断，推理模型负责推理与生成。）

---

## 2. 背景：公司、命名与哲学

- **公司**：TypeSafe AI（旧金山，**2024 年创立**，11–50 人），隐身两年后于 **2026-09-15** 发布 Jev
  （早期访问，waitlist 制）。种子轮 **$40M，DCVC 领投**——官方新闻稿（BusinessWire 2026-09-15）
  + Yahoo Finance + seedtable 多源确认；Dealroom 一处写作 $25.9M（自估口径，与官方稿冲突，
  以官方 $40M 为准）。DCVC 普通合伙人 James Hardiman 评语：「把日益强大的模型变成开发者能大规模
  可靠构建产品的技术，是 AI 剩下最大的挑战之一」。
- **三位联合创始人**：**Diogo Almeida**（CEO）+ **Erik Gafni** + **Sasha Sheng**。Almeida 前 OpenAI，
  **2022 年 InstructGPT 论文作者之一**，GPT-4 贡献者名单归入「Foundational RLHF and InstructGPT
  work」，RLHF 共同发明人（官方 ML primer + 百度百科/seedtable 交叉）。他的动机句（TechCrunch 采访）：
  「We have lightning in a bottle, and yet it is not useful」——问题出在「我们为人类语言优化」，
  而「计算机说的是另一种语言」。
- **发布热度**：HN 发布帖 **1929 分 / 509 评论**（6 天内）；Almeida 发布推 **421 万浏览 / 1.98 万赞**；
  **36 小时内 14 万开发者涌入内测**（36氪）；需求一度把 API 打挂（TechCrunch）。HN 上 CEO
  （用户名 CompleteSkeptic）亲自下场答问。36氪另报**估值约 2 亿美元**（未经官方证实，单一来源），
  并确认 Almeida 为 **InstructGPT 论文第四作者**。**服务尚未向中国大陆开放**（央广网）。
- **命名**：
  - *System One* ← Kahneman《思考，快与慢》：System 1 快而直觉，System 2 慢而深思。Jev 做 System 1 的活
    （快判断），推理模型做 System 2 的活。团队刻意反驳「System 1 易错所以不可靠」的直觉——在受约束的
    判定空间里，System One 反而可以做得比替代方案更可靠。
  - *Jev* ← 经济学家 William Stanley **Jevons**（杰文斯悖论：煤的效率提升反而增加了煤的总消耗）。
  寓意：**「智能成本每下降一个数量级，就解锁数量级更多的新用例」**。
- **哲学**：Machine Native Intelligence（机器原生智能）——自动化的大头将是 AI↔AI / AI↔软件交互
  （官方预估 **99% 机器对机器、1% 对人**），所以机器接口比聊天接口重要。AI 应当具备软件的性质：
  结构、可靠、可观测、可测试、快、一致、便宜。口号是 **"building prod, not God"**（造生产件，不造万能神）。

---

## 3. 底层剖析：三层新栈

官方声称全新造了三层栈：**新模型架构 + 并行采样器 + RLCD 训练法**。架构细节未公开
（FAQ 里「Jev 是不是小号 LLM」「怎么做到的」等问题有标题无答案），但输入输出行为契约完整：

### 3.1 非自回归、并行判定

- 一个请求 = **一个 state + 一组问题**；所有问题对同一 state **并行、隔离**评估，
  互看不见对方的答案。加问题几乎不增加延迟，只加问题本身的 token（很便宜）。
- 输出是「你预定义的答案空间上的概率分布」，不是逐 token 生成——所以不存在「写到一半崩掉」，
  形状错误率结构性为 0%（官方承认这是「by construction（按构造）」，不是实测统计）。
- 上限：**Choice ≤ 255 个选项**；超出走两阶段（先独立打分再显式选择，官方 Wikiracing 演示用过，
  偶发变慢）。Score 2–10 级。**仅文本输入**（字符串/JSON 对象/文本数组），暂不支持图像音视频。

### 3.2 RLCD vs RLHF vs RLVR（官方 ML primer）

| 方法 | 全称 | 优化目标 | 产物 | 副作用 |
|---|---|---|---|---|
| RLHF | RL from Human Feedback | 人类偏好的回答 | 聊天机器人（InstructGPT/ChatGPT） | 奖励谄媚与「听起来自信的幻觉」；**mode dropping**（偏好优化把输出分布收窄到单一风格，压制其他模式；极端化即 GAN 的 mode collapse） |
| RLVR | RL with Verifiable Rewards | 程序可验证的正确性 | 推理模型（数学等强、但更慢更贵） | 限于可验证任务 |
| **RLCD** | RL for **Calibrated Decisions** | **认知论上诚实的概率**（epistemically honest probabilities） | 判定模型（Jev） | 放弃文本生成 |

primer 的关键论点：**「人类偏好」与「机器可信」是不同的优化目标**——一段话可以让人爱不释手
却不能用于无人值守的自动化。RLHF 适合聊天模型；生产自动化需要的是「受约束的决策 + 校准的不确定度」。

### 3.3 校准：承诺与精确边界

- 承诺：校准模型给出的概率 p，在**大样本群体上**对应真实发生率——标 0.2 的那组预测约 20% 发生，
  标 0.8 的约 80% 发生。官方实测（consistency cookbook）：15 次重复采样，逐问题概率标准差均值
  **0.0102**，低于所有 LLM 对照组（含 temperature=0 的 Haiku/GPT-mini）——LLM 在判断题上
  「与自己意见不合」（run-to-run 漂移），Jev 稳定。
- 边界（官方自己反复强调）：**校准是群体性质，不保证任何单次答案正确**。官方文档明说
  「calibration is measured across groups of predictions and does not guarantee any individual
  answer is correct」。所以置信度用于路由（act / confirm / escalate），不是行动许可证。

### 3.4 「零幻觉」的精确解读（第三方最重要的纠偏）

TrueFoundry 等指出该口号会被转述压扁。准确说法是两层：

- **类型/形状幻觉：按构造消灭。** 模型不产自由文本，不可能编造一个引用、一个不存在的工具名、
  一个非法值。schema 合法性有构造性保证（ falsify 只需一个反例，官方称「数学上不可能给出」）。
- **语义判断错误：依然存在。** 限定在三选项里的模型仍可能**自信地选错**。被消灭的是「畸形的答案」，
  不是「错误的判断」。校准是官方对此的回应，而校准恰恰是最需要独立验证的声明
  （至今无第三方复测，见 §8）。

一句话：**typed ≠ correct**（类型化输出保证接口，不保证真值）——与 TCER 在 LLM 解读层
坚持「判定数值与叙事文本分离、本地合成、audit_warnings 机械校验」是同一个设计直觉。

### 3.5 架构线索拼图（官方从未公开，以下为多方证据拼合，置信度分级）

官方对架构守口如瓶（CEO 在 HN：「close to the chest for now」，暗示**可能发论文**）。
第二轮调研拼出的证据链：

| 证据 | 来源 | 指向 |
|---|---|---|
| 「Jev 是 **transformer-based** 但不是 LLM」 | TechCrunch 报道表述 | 底座仍是 transformer，但输出头/训练目标完全不同 |
| 「外部观察者怀疑在**开源权重 LLM 之上**构建」 | TechCrunch（outside observers suspect） | 基座可能来自开源模型而非从零预训练 |
| GitHub org fork 了 **LLaDA**（扩散语言模型官方实现）与 **vllm** | github.com/typesafe-ai 仓库列表（第二轮实测） | 扩散/掩码式非自回归 + 高吞吐推理引擎——与「并行采样」宣称吻合的最重线索 |
| 「输出 token 免费，因为**他们不做自回归**——输出一次前向传播算完」 | TypeSafe 员工 zenlikethat（HN 评论） | 非 LLaDA 不可；任何「一次前向出全部答案」的架构（encoder 判别头 / 并行受限解码 / 扩散单步）都符合 |
| HN 架构高赞猜测：specialized **encoder-only(-ish)** transformer + scalar/ordinal 输出头，非因果 | 用户 quotemstr | 判别式模型（理解 state、不生成） |
| GLiNER2 被指 prior art；社区贴出「LLM 底座 + 树/图注意力 + 双标量头（logits+confidence）」假想 | HN（krackers / cooljoseph） | 「实体抽取判别器」路线的历史先例 |
| **纯合成数据训练**：「一半公司是统计上严谨的合成数据实验室」 | TechCrunch 专访 Almeida 原话 | RLCD 的燃料是自产合成决策对，不依赖人类标注/互联网语料 |

**开源复刻给出的反向标定**（见 §7.4）：Harsha Gundala 的 Qwen-2.5-1B-RLCD（KV-cache 广播 +
logit 切片的**并行受限解码**，2 小时复刻）证明「Jev 体验的大部分」——类型安全、并行、快——
用普通 LLM + 聪明的推理技巧就能得到（M4 MacBook 5×）；**但复刻不出 RLCD 校准**，
且 TypeSafe 未公开 RLCD 细节供独立验证（X 用户 Nicholas Dunzelman 的总结，Apidog 深读同结论）。
Latent.Space 盘点的 2 天 6 个克隆中，路线分三派：冻结论模型读 logits（OpenJev/mini-jev）、
从头训选项评分器（jevlike）、换解码引擎（Qwen-RLCD / vLLM PR 57250 的 DiffusionGemma Jev 模式）；
对 Jev 的一句锐评：「confidence is **entropy-based, not calibrated**」——**Jev 真正的护城河只剩校准**，
而这恰是唯一未公开、未复验的部分。

结论（置信度中）：Jev ≈ 判别式/并行解码 transformer（可能基于开源权重），输出头直出概率分布，
以大规模合成数据 + RLCD（重罚高置信错误分布——HN beta 用户 porridgeraisin 转述机制）训练。
**此为拼图推断，非官方确认**；官方论文若发布应回头修订本节。

---

## 4. API 契约

### 4.1 端点与错误

```
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <TYPESAFE_API_KEY>
Content-Type: application/json

{ "state": <string|object|array>, "model": "jev-latest", "questions": { "<id>": {...}, ... } }
→ { "model": "jev-1.13.0", "answers": { "<id>": {...}, ... }, "usage": {"input_tokens": N, "output_tokens": N} }
```

- `GET /v1/models` 列模型（当前只列别名）；响应的 `model` 字段回报**实际应答的版本化 ID**——
  调过阈值的系统应**钉住版本 ID**（`jev-latest` 别名随发版漂移，答案会从脚下变走）。
- 错误码四分支：**401** 鉴权失败 / **422** 请求 schema 校验失败（body 指明字段）/
  **429** 限流 / **529** 过载（退避重试；SDK 默认带指数退避并尊重 `retry-after`）。
  ——与 TCER `typesafe_client._send_request` 的转译分支一一对应。
- 另有 Vercel AI Gateway 通道（`typesafe-ai/jev`，同价），LangChain / pydantic-ai / LiteLLM /
  Vercel AI SDK 7 均已原生集成。

### 4.2 三原语（question types）

问题 ID 只归代码用，**不发给模型**——完整语义必须写进 `instructions`。结构化 state 用反引号路径
引用（如 `` `ticket.messages[0].text` ``）。`instructions` / `criteria` 均可取 string | object | array；
易混淆选项用对象展开 `what` / `not_for` / `examples` 等自定义字段（字段名不保留、自选，
模型看到的是「字段名+值」整体）。

| 原语 | 问什么 | criteria 形状 | 返回字段 | 语义要点 |
|---|---|---|---|---|
| **Choice** | N 选 1 | map：选项名→描述（≤255 项，描述可 `null`） | `choice`（最高概率项）+ `probabilities`（全选项分布，和为 1）+ `confidence` | 描述要能区分邻近选项；可能覆盖不全时加 `other`/`none_of_the_above` 逃生口；**模型选不了你没列的值**——候选覆盖检查在代码侧 |
| **Score** | 序数尺度位置 | array：有序等级描述（2–10 级） | `score`（概率加权位置，可落在两级之间）+ `legend` + `probabilities`（逐级）+ `confidence` | `score = Σ(级号×概率)`（例：1×0.57+2×0.43=1.43）；**等级描述「情境」不要「程度」**（"有替代方案的坏功能" 好，"中等严重" 无效）；模型看不到级号与邻居，比较式措辞无意义 |
| **Noul** | 是否为真 | 可选 `{true, false}` 边界描述 | `noul`（0–1，P(是)） | **无独立 confidence**——二值分布本身已完整；0.5＝五五开，**不是「中等程度」**；多标签场景一标签一 Noul；措辞让高=是（避免否定式提问） |

### 4.3 置信度（confidence）

- 语义：**概率描述哪个结果可能，置信度描述信念多集中**。由 `probabilities` 分布派生
  （交互 demo 用 `(N×峰值概率−1)/(N−1)` 近似；全部质量在一个选项=1.0，均匀分布=最小值）。
  是「分布形状的摘要统计」，不是正确性证明，也不是行动许可。
- 官方三段路由范式：**高**→自动执行；**中**→请用户确认/标记复核；**低**→转人工或推理模型。
  阈值随风险缩放（confidence 文档的银行示例：查余额只需过 0.5 底线；批准转账要 >0.85，
  0.6–0.85 区间先向用户求确认——「Your code encodes the risk tolerance」，风险容忍度编码在代码里）。
- Noul 无 confidence，但官方 consistency cookbook 给出**不确定带**替代：<0.30 判否、
  0.30–0.70 转人工、>0.70 判是（带宽是示例值，生产应由标注数据 + 误判代价决定）。
- 不锁死：完整 `probabilities` 总是返回，可换自定义集中度度量。
- **第三方最深解读（pydantic-ai 集成文档）**：布尔/受限场景下 confidence 实际语义是
  **margin**——答案概率离「实际使用的决策阈值」的距离缩放到 0-1（阈值 0.75 下 0.8 的 yes
  报 0.2 而非对半翻转口径的 0.6）；多字段 typed 输出取**最不确定字段**的 confidence；
  这解释了为何「confidence 高」在改阈值后依然自洽。

### 4.4 模型规格（jev-1.13，2026-09 快照）

| 项 | 值 |
|---|---|
| 当前版本 / 别名 | `jev-1.13.0`；`jev-latest`（SDK 默认，指向稳定版）、`jev-preview`（当前无预览版，与 latest 同） |
| 定价 | 输入 $0.042/MTok（$42/BTok），输出免费；只按输入 token 计费 |
| 限流 | 250,000 tokens/s + 1,200 requests/min（超限 429）；**动态调整、可无预告变更**；企业可提额 |
| 上下文 | 单请求 64k token（state + 全部问题合计）；state + 单个最长问题 ≤ 32k（≈15 万英文字符） |
| 定制 | **无微调/LoRA**，全账户同一套权重——定制只靠 state/instructions/criteria |
| 语言 | **英语为主要训练语言、最准**；其他语言（含 CJK）可用但准确率降低 |
| 数据 | 不用客户请求/响应训练；企业可选零保留（ZDR） |

（TCER 实测与「英语最准」吻合：同 state 中英题面 A/B，英文版 confidence 普遍更高且
input token 省 20–35%，见 §9。）

---

## 5. 已知缺陷（官方 model-jaggedness/jev-1.13，九条全录）

官方罕见地自曝缺陷清单（"Jev isn't perfect. Here are some jagged edges we are aware of"，
多数承诺后续版本修复）。总画像：**擅长 System One 任务；弱在间接指涉、数值精度与字面解读**。

| # | 缺陷 | 表现 | 官方 workaround |
|---|---|---|---|
| 1 | **字面解读** | 回答「你写的问题」而非「你想问的问题」；限定词/否定/隐含条件按字面执行 | instructions 里写明精确条件；criteria 加边界例；歧义拆成两个字面问题由代码合并 |
| 2 | **数学与计数** | 不是计算器；字符数、词频、长列表计数随集合变大误差增大；hex/RGB 邻近比较、低级二进制/汇编问题表现差 | 计数在代码里做（逐项一个 Noul 求和）；数值先换算再传入；**不要用 Score 期望值在等级间插值还原幅值** |
| 3 | **日期时间比较** | 日期按文本读，不做有序量；先后/间隔/窗口判断不可靠，混合格式与相对表述更糟 | 模型只做**抽取**（年/月/日的 Choice，含"未写明"选项），组装/排序/时差/工作日全在代码 |
| 4 | **间接指涉** | 双重否定、属性的属性、多跳推理掉准确率 | 直接写指令，显式点名 state 相关部分 |
| 5 | **大 state 无关细节** | 无关材料当干扰项，且出错难定位输入源（**context rot，上下文腐烂**） | 代码先过滤只发需要的字段；或用 Noul 做相关性过滤 |
| 6 | **对抗内容** | 默认不把 state 当敌意输入；注入指令/误导框架/自辩文本可移动答案 | criteria 显式化；上线前充分测试边界；官方称「未来会改进」 |
| 7 | **指令与 criteria 矛盾** | instructions 与 criteria 不一致、真→否的倒置映射表现差 | criteria 视为指令的延伸，两者用「普通人能读懂」的语言对齐 |
| 8 | **结构不变量不保证** | Noul 与 Choice 输出不可直接比较（0.22 Noul vs 0.01 是/0.99 否并存）；一问与其否定的两个 Noul 实测和为 1.19 而非 1.0 | 不依赖跨问题结构恒等式/算术恒等式；Noul 阈值不可平移给 Choice（**Choice 相对、Noul 绝对**——Noul 可以对全部选项都低） |
| 9 | **生成** | 未按文本生成训练；链式 Choice 逼它逐字生成又差又慢 | 候选由正则/生成模型产出，Jev 只**选**；生成交给别的模型 |

**长期指导**（jaggedness 页收尾）：代码能精确算的别问模型；一问塞多个判断要拆；
System 2 型多层间接任务别来；多余上下文有代价——"Jev suffers from context rot"。

---

## 6. 核心模式与实证数字（官方 patterns + cookbooks 提炼）

### 6.1 推测扇出（speculative fan-out）

「可能用到的问题一次全问，用不上的答案代码里丢弃」。全部问题并行评估，加问题几乎不加延迟，
只加问题 token。实证（parallel_questions cookbook，GDPR 维基文 53,777 字符 + 13 问
[8 Noul + 2 Choice + 3 Score]，各跑 5 次）：

| 策略 | 调用数 | 成本 | 总耗时 |
|---|---|---|---|
| 1 次全问 | 1 | $0.000497 | 0.27s |
| 13 次单问 | 13 | $0.006090 | 2.71s |

→ **12.2× 便宜、10.0× 快，且逐题答案与单问模式逐值一致**（仅 2 题有同量级采样噪声；
「噪声是问题的属性，不是批量的属性」）。文档主导型负载下文档只发一次，文档越大节省越逼近 N×。
**注意**：官方技能页特别点名**编码代理（coding agents）最容易犯「一问一调」的错**。

### 6.2 置信度门控路由（confidence-gated routing）

confidence 作第二决策轴：「答案告诉你是什么，置信度告诉你做不做」。语音银行示例：
intent Choice（查余额/批准转账/其他）+ 分级阈值——任意动作 <0.6 转人工；
`approve_transfer` 0.6–0.85 要用户复述确认；>0.85 才自动执行。低风险动作只过 0.6 底线。

### 6.3 复合评分（composite scoring）

多准则排名 = 每维度一个原子 Score（同请求并行）→ 代码归一化（score÷顶级号 → 0–1）→ 加权合成。
简历筛人示例：python_depth/team_leadership/system_design/generalist 四维 5 级量表，
同一组归一化分数套两套权重——Senior IC（0.40/0.10/0.40/0.10）与 Eng Manager
（0.15/0.40/0.20/0.25）得两份排名。**改权重=改代码常量，不用重写 prompt**，全程可审计可回放。

### 6.4 意图路由（intent routing）

前门分类器：intent Choice（4 类）+ complexity Score（3 级）一次调用，路由表落代码——
`order_status`→纯代码查库（零 LLM 成本）；`product_question`/`return_exchange`→挂不同上下文的
专科 LLM；intent confidence<0.5 或「投诉且 complexity>1/复杂度不确定」→转人工。

### 6.5 级联（SDE cascade：mini → verify → reasoning）

结构化抽取的质量/成本折中：便宜小模型抽取 → **Jev 做逐字段验证器**（每字段一组 Noul，
**坏=true 措辞**：幻觉/跑题/不合理/类型不符/字段错空/格式违规…）→ 任一 P(坏)>0.7（max 门，
不是均值——「一面自信的红旗就该升级，不被平均掉」）才升级到贵推理模型重抽。
实证：gpt-5.4-mini 在无日期页面**编造**了 schema 示例值当描述——JSON schema 校验通过（合法但错），
Jev `hallucinated=0.95`/`off_target=0.85` 双红旗触发升级，gpt-5.5 高推理放弃幻觉值。
100 条扫描：级联前沿在（成本，质量）平面上**每一个单模型的左上方**（更便宜且更好）。
好验证器的五条标准：窄而落地、坏=true、逐字段+max 聚合、独立便宜、**可分离**（真错误高、正确值低，
一个阈值切得开——分离度决定 Pareto 曲线位置）。

### 6.6 层次分类（hierarchical classification，beam search）

树/分类法走到叶子：每节点兄弟集=一个 Choice，全概率分布=边权。**贪心**（只走最高概率）一步错
不可挽回；**束搜索**（K=3）保留 K 条路径逐层并行分类，按路径得分
`几何均值 = (Π边概率)^(1/决策数)`（长度归一，防浅深叶子不公平）剪枝。
实证 4 层分类法：**贪心 2/4 对，束搜索 4/4 对**（束搜索救回了 CPC 专利分类落到兜底节点、
Shopify 商品类目落到邻类的两个错误）。补充：choice 官方可靠性上限「约 240 个选项」附近
（classification_using_confidence 用 75 选项无压力）。

### 6.7 自一致性带（consistency cookbook）

同一保险理赔 borderline 案 + 14 问评分表 ×15 次重复：Jev 逐题标准差均值 0.0102（全场最低），
LLM 组（含 t=0）在判断题上自我漂移。动作设计：**<0.30 否 / 0.30–0.70 人工 / >0.70 是**——
避免 0.49/0.51 在 0.5 阈值两侧触发相反动作。官方自省三连：该带宽是示意值非校准保证；
一致性 ≠ 正确性；过了带也不证明对。

### 6.8 引用核查（citation check）

两步管线：①规范化后**精确串匹配**查引用原文在不在源文档（不在=捏造，零模型调用）；
②一个 Choice（supports/contradicts/says_nothing）判节选与论断的关系；③confidence≥0.8 自动定案，
以下转人工。RFC 7519 + 8 条引用实证：4 verified（0.93+）、1 fabricated（串匹配截获）、
1 contradicted（**原文一字不差但上下文说反话**——纯字符串匹配的盲区，0.99）、2 unsupported 低置信转人工。
教训：**精确匹配先行省模型调用，语义判定兜住「引对了字、读反了义」**。

### 6.9 置信度分层上报（classification using confidence）

SEC 年报 → 75 个 SIC 行业组单 Choice。置信度即分流器：conf≥0.9 报**具体组**，否则上报其所属
**大部**（标签免费继承，不加调用）。60 份实测：强制组级 39/60（65%）；可信半区（conf≥0.9，
30 份）**90% 对**，不可信半区 40% 对（两部类 70%）；兜底策略合计 **48/60（80%）可用答案**。
「可信的那半 90% 的时间是对的；另一半 40%」——置信度把同一批请求切出了两个质量世界。

### 6.11 剩余 cookbook 数字一览（第三轮 tinyfish 批量精读补全）

| Cookbook | 模式 | 实证数字 |
|---|---|---|
| **rerank_typesafe** | BM25 短名单 + 每查询-候选对一个问题重排（CLERC 法律查询） | Top-1 **5%→18%**、Top-5 15%→35%、Top-10 **38%→62%**；1200 次调用共 1.536M input tokens **$0.0645** |
| **skill_suggestion** | 182 个 Hermes 技能「最多选 1 个」：一次全排 + 二次 top3 深读（可全拒） | **错误加载 16.8%→7.3%（2.3×降）、无谓加载 9.8%→4.0%（2.4×降）**——省的是 agent 上下文预算而非 token 钱 |
| **semantic_find** | 行级语义查找：218 个行 ID 一个 Choice 打分 + Noul 查「文档含答案吗」 | ≤255 行单请求；超出两趟。Choice 概率和恒为 1——**没有答案时也必有第一名**，所以必须配 Noul exists 兜底 |
| **entity_alignment** | 知识图谱实体对齐：450 对啤酒目录，**单个 Score 三级（合并/不链接/给策展人）承载整个决策** | 三级即三种处置动作——「无需拟合阈值」；实测 80% 对判为不链接 |
| **autoresearch** | 特征自研究循环：提议问题→文本转数值特征→CatBoost 回归（酒评 80-100 分预测） | 5 轮循环仅 **+0.097 分（95% CI [-0.147,-0.050]）**——官方如实呈现的**微弱收益**，与「特征工厂」直觉形成清醒对照 |
| **llm_guardrails** | LLM 进出口双向筛查：越狱 Noul + 危害程度 Score → 阈值决定 pass/review/block/route | 模式即 §6.2 三段路由的工业化应用 |
| **classifying_rag_passages** | RAG 段落打分：矛盾段落**保留并标记**、注入指令段落**丢弃**、无关段落过滤 | 「矛盾≠没用」是 RAG 语境的独有洞察 |
| **function_calling** | NL→类型化函数调用：函数名与闭集参数映射为 confidence-aware 问题，开放参数交给调用方 | 选择而非生成的函数调用版 |
| **date_extraction** | jaggedness #3 的官方解法：年（1900-2050 每年一选项+none）/月/日 Choice 抽取，代码组装校验 | 相对日期（「下周五」）同样走分量抽取 |

### 6.10 官方建造方法论（how-to-build 页 + agent skill，第二轮补全）

**三架构定位**：传统软件（决策树）↔ LLM agent（模型自选下一步，「每个循环都多一次脱轨机会」）
↔ **AI-powered software**（代码持有控制流，模型只出现在需要「可编程常识」的窄点上）。
System One 可组合性六性质：Structured（类型安全按构造）/ Parallel / Comparable（可排序可 if）/
Fast / Calibrated / Self-consistent。

**8 步设计流程**的精华是第 4 步「**分解问题**」（官方自称「本指南最重要的概念」）：

- 垃圾邮件检测：坏＝一个 `is_spam`；好＝**6 个原子问**（索要凭据/意外中奖/制造紧迫/
  发件人身份不符/链接域名不符/伪装链接目的地），各指向具体 state 字段；
- 工具调用轨迹核查：坏＝「这轨迹对吗」；好＝**每个工具调用 9 问**（工具相关性/参数位置匹配/
  schema 合规/结果 ID 链接/坐标使用/日期单位一致…）。

**官方 Agent Skill**（github.com/typesafe-ai/skills，1.3k★）：给 Claude Code/Codex 等
编码代理注入 TypeSafe 全量上下文。安装：`claude plugin marketplace add typesafe-ai/skills` +
`claude plugin install typesafe@typesafe-ai`，或 `npx skills add typesafe-ai/skills --skill typesafe-ai`。
Skill 页最有用的三条实战规则：①**「Agents aren't great at writing questions」**——问题措辞
要人机协作打磨，全部问题+阈值常量收进一个可评审的文件；②只需要最优选项时**直接取最高概率，
别滥用置信度阈值**（阈值是为副作用 gating 用的）；③有明确统计算法诉求时**用概率而非置信度**。
（TCER 的经验同构：Jev 题面由人写死在 `llm_prompts`，不让任何 agent 现场发挥。）

---

## 7. 社区生态剖析（awesome-jev-projects，2026-09-20 快照）

雷达站（logicrw 维护，源码-backed：每条收录都「已核对固定版本源码」，三语对照、标注证据 URL；
性能数据未独立复测）。**发布 6 天收录 358 个开源项目**——生态速度本身是信号。

### 7.1 分类分布（358 项）

| 类别 | 数量 | 类别 | 数量 |
|---|---|---|---|
| SDK & Decision Frameworks | 65 | Domain & Vertical Tools | 22 |
| High-Frequency & Simulation | 31 | Data & Search | 20 |
| Evaluation & Observability | 29 | MCP & Integrations | 19 |
| Browser & OS Action | 27 | Codebase & Graph Pathfinding | 12 |
| Routing & Cost Optimization | 27 | Decision Tools | 12 |
| Security & Guardrails | 26 | Creative Tools | 11 |
| CLI & Pipelines | 23 | Voice & Conversation | 4 |
| Context GC & Filter | 22 | Classification & Taxonomy | 2 |

标签 Top：typed-decisions(195) · multilanguage-sdk(75) · classification-ranking(70) ·
**coding-agents(68)** · evaluation-benchmarks(61) · mcp-integrations(47) · cli-git-gates(46) ·
games-simulation(38) · llm-routing-cost(32) · security-guardrails(31) · browser-automation(26) ·
context-compaction(23)。语言：TypeScript 99 / Python 70 / JavaScript 33 / Rust 14 / Go 11。

### 7.2 头部项目与六大形态（按星标 + 决策点归纳）

| 项目 | 星 | 形态 | Jev 决策点 |
|---|---|---|---|
| langchain（`TypeSafeClassifier`） | 146.7k | SDK 集成 | state+类型化问题发 `/v1/systemone`，同步/异步 Runnable |
| ai-hedge-fund | 63.6k | 垂直 | 策略问题转 System One 请求，原生答案转回项目统一格式 |
| litellm | 59.2k | **模型路由** | 请求映射到预设复杂度类别，路由策略选后端 |
| oh-my-pi | 32.1k | **编码代理** | Agent 状态+有界问题发 Jev，解析结构化回答 |
| jev-model-router | 30.9k | 模型路由 | 评任务级别/推理需求/生产风险→本地策略映射调用配置 |
| composio | 30.3k | SDK | 工具/操作条件转结构化问题，读答案走本地调用逻辑 |
| cua（computer-use） | 25.1k | 浏览器操作 | 读 DOM/视觉描述，**只返回已提供的候选动作 ID** |
| pydantic-ai（`TypeSafeModel`） | 20.1k | SDK | 把 output_type 编译成类型化问题，请求后还原输出 |
| eliza | 19.4k | SDK | 仅业务代码显式调 systemOne 才发请求 |
| json-render | 17.2k | 创意 | 经 Vercel AI Gateway 评估组件配置→界面规格 |
| jev-ultrafast（browser-use） | 11.5k | 浏览器 | 一次请求选操作+控件，**文本模型负责生成输入内容** |
| openchamber | 10.2k | 编码代理 | Choice 判任务类别；Noul 判自动批准权限是否该留人工确认 |
| fast-jev-compaction | 5.0k | **上下文 GC** | 分别判断工具调用与完整结果是否还需保留，本地规则执行保留/截短/删除 |
| agentgateway | 4.9k | 安全护栏 | 越狱/有害/泄密评分，按阈值或评估错误拒绝请求 |
| latitude-llm | 4.7k | 评估观测 | 判各检查适用性，满足阈值与限流才补检查任务 |

归纳**六大应用形态**（358 项的去重抽象）：

1. **智能 if 语句**（分类/检测/评分/路由塞进现有管线）——占比最大；
2. **Agent harness 内嵌判定**（68 个 coding-agents 项目）：模型路由、工具风险门控、
   权限确认、上下文压缩——LangChain 官方博客归纳的 `ModelRouterMiddleware`（选便宜够用的模型）
   与 `AutoModeMiddleware`（bash 等危险工具执行前 Jev 拦截）是范式样板；
3. **选择而非生成**（browser-use/cua：LLM 定目标，Jev 从 DOM 候选中选控件——「发牌让它挑，
   别让它报牌名」）；
4. **验证与守门**（引用核查、越狱检测、PR 风险矩阵、git-gates 46 个）；
5. **高频实时**（游戏 38 个：Doom ~10 查询/秒≈$7/小时、逐键语气评分、流式过滤）；
6. **语义特征工厂**（自然语言→概率特征喂 CatBoost 等经典 ML；18→38 问迭代出 67 列特征）。

### 7.3 雷达站本身的工程做法（值得借鉴）

纯静态站 + `projects.json` 单文件数据（含 GitHub 元数据定时同步快照 `metadataFetchedAt`）；
每条收录带 `claimStatus`（三语）声明「已核对固定版本公开源码；本站未运行/未测性能/未做安全审计」；
`jevDecisionPoint`（Jev 决策点）与 `highlightBenefit` 字段把「这项目里 Jev 到底判什么」
结构化成一等公民——比单纯罗列 README 强得多。

### 7.4 开源复刻浪潮（发布 48 小时内爆发，第二轮新增）

Latent.Space 标题即结论：「**2 天 6 个克隆**」。全景目录见 OmniJev/awesome-jev
（papers + open reproductions 专列）。三条技术路线 + 代表：

| 路线 | 代表 | 做法 | 与 Jev 的差距 |
|---|---|---|---|
| 冻结 LLM 读 logits | **OpenJev/SemIf**（Qwen3.5 4B/35B + 三类 NLI 头）、mini-jev | 选择题喂小模型、跳过生成，取选项 token 的 logits 归一为概率 | 无 RLCD，概率非校准 |
| 从头训选项评分器 | **jevlike**（40K byte embedding + option-attention，Doom/象棋 demo）、**Laya**（ModernBERT-large + RLCD 判决头，38ms/次） | 「文本 + N 选项进、每选项一概率出」的专用小模型 | 规模/智能远小于 Jev |
| 换解码引擎 | **Qwen-2.5-1B-RLCD**（Harsha Gundala，KV-cache 广播 + logit 切片，Apple Silicon 5.6–7×）、**vLLM PR 57250**（DiffusionGemma 的 Jev 模式）、**Bespoke Nimble**（Qwen3.5-9B LoRA + 对比数据，评测 66%→**90%**，Jev 93%，H100 100ms）、Kev-0.5B / LFM2.5-2.6B-RLCD / rlcd-modernbert-151m / openjev-sglang | 保留开源基座，全部 schema 字段一次 prefill 并行打分 | 最接近；HF 社区复现追踪器报告与 Jev **~74% 一致率** |

- 复刻生态的自我评价（Apidog 深读）：「**None of them reproduce RLCD. That's the honest headline.**」
- r/LocalLLaMA 有帖称一年前已开源同型架构（模型+数据集+论文）却无人问津——「frontier lab 做了
  横向化的东西就有支持」的开源社区怨气。
- MParakhin（前 Bing CEO）的观察：post-ChatGPT 用户当启示，pre-GPT 的 ML 人对热度困惑——
  「判别式打分头」在 ML 史上并不新（GLiNER 一脉）。
- **对 Jev 定位的反向确认**：复刻能拿到快与类型安全，拿不到校准——Jev 的差异化承诺全部押在
  RLCD 校准上，这也是 §8 批判的靶心。

### 7.5 大厂与中国厂商动向 + 框架集成层（第三轮新增）

**官方级跟进（风向标）**：
- **谷歌 Gemma 官方账号发布「DiffusionGemma as Jev」**（36氪报道）：把自家扩散模型一步去噪、
  一次性并行给出全部选项概率，并顺手补上**视觉能力**——大厂正式入场 System One 范式的信号。
- **微软 agent-framework** 提案一等支持（issue #8556）：集成面明确「低置信与陈旧/捏造 ID
  **fail closed**（向失败关闭）」；**AWS awslabs/aidlc-workflows**（issue #1271）以
  `noul > 0.8 → escalate` 为例提案集成层。

**中国厂商（APUS，全球最早一批独立复现，央广网 2026-09-20）**：
- APUS AI 实验室开源 **fast-browser-use** Agent Skill（MIT，三平台）：从 Jev 公开文档逆向出
  「跳过自回归解码、**隐状态直接打分**」核心逻辑，本地 Qwen3.5-9B 单次前向完成浏览器
  「点哪里、选哪个」决策，复现 KV-Cache 广播与并发批量评估；Apple M2 Pro 离线跑真实
  维基检索**中位 18s**、表单填报 **3s**、单任务仅 4 次打分、零云端零费用——
  从机制上消除错误选择器与格式幻觉。
- 澎湃报道**具身智能**从业者关注：环境状态→类型化动作决策（含概率与安全约束）正是
  具身「脊髓反射」层缺的构件。

**框架集成层的工程共识**（firecrawl developer 检索）：
- **pydantic-ai**（`TypeSafeModel`，官方文档页）：confidence 语义的最深第三方阐释——
  对布尔字段它是**离决策阈值的 margin**（概率 0.01 答 False → confidence 0.98；阈值 0.75 时
  0.8 的 yes 报 0.2 而非 0.6）；对 Choice/Score 是 Jev 原生分布数字；**多字段输出取最不确定
  字段的值**；float 字段无 confidence（概率即答案）；低置信 fallback 到 LLM 的链路要
  **监控 fallback 触发率**——「全 handoff = 全价，触发率是唯一暴露它的数字」。
- **jevcal**（社区 CLI）：在**自有标注数据**上把逐问题阈值拟合到目标准确率、留出集验证、
  估算仍需 fallback 的流量占比、CI 里重检锁定阈值——**少于 ~100 行标注的拟合不可信**。
- **typia**：点破适配器本质——OpenAI/Anthropic/Google 经 System One 适配器返回的「概率」
  是让 LLM 自己写数字，**无分布无校准**，只有 Jev 等原生 evaluation 模型才有真校准。
- **Langfuse**（eval 集成博客）：三路分流（高置信 act / 中间带人工 / 其余丢弃）；短板提醒
  「要自己设计逃生口，校准你的判官之前先验证」。
- **AutoGPT**（TypeSafe Yes/No block）：min_confidence → unsure 分支；暴露 request/response
  verbatim、latency、token、**truncation 标志**（oversized state 截断有告警字段）。

---

## 8. 批判性评估（第三方一致强调的点 + HN 509 评论提炼）

**官方基准站全量数字（evals.typesafe.ai，第二轮取回）**——4 workflow 等权平均，
参考标签 = GPT-6 Astra + Claude Fable 5.1 高思考均值（**模型共识标签，非人工真值**，
官方明示的设计选择；Gemini 全程缺席评分）：

| 模型 | 准确率 workflow→prompt | 成本 wf→prompt | 耗时 wf→prompt |
|---|---|---|---|
| haiku 4.5 | 53.6% → 18.1% | $0.0195 → $0.0363 | 12.5s → 21.2s |
| sonnet 5 | 67.8% → 60.4% | $0.1174 → $0.2251 | 78.1s → 149.2s |
| opus 5 | 73.1% → 64.8% | $0.1761 → $0.3417 | 37.8s → 70.5s |
| DS v4 flash | 64.4% → 59.3% | $0.0059 → $0.0132 | 51.9s → 120.1s |
| DS v4 pro | 65.5% → 59.7% | $0.0413 → $0.0907 | 86.5s → 192.1s |
| luna | 66.8% → 51.9% | $0.0033 → $0.0079 | 12.9s → 27.3s |
| sol | 74.1% → 63.4% | $0.0836 → $0.2005 | 23.3s → 48.6s |
| terra | 67.9% → 61.6% | $0.0304 → $0.0750 | 10.1s → 25.1s |
| **Jev（仅 workflow）** | **67.8%** | **$0.0004** | **0.4s** |

分任务：Jev 客服 76.0%（全场第二，仅次 sol 78.3%）、安全 61.7%、代理轨迹 71.6%、
**发票 61.8%（Jev 最弱，全场最低分项之一）**。sol 赢 3 项、opus 5 赢安全项（66.2%）。
换算：vs sonnet 5 **同分**但便宜 294×、快 195×；vs opus 5 少 5.3 分但便宜 ~440×、快 ~95×；
vs DS v4 flash 反而**准 3.4 分**且便宜 15×。193.6×/444.6× 头条的具体出处：0.114s vs 8.566s
（jevai.wiki 核对）。官方承认的反例：DS v4 flash 安全任务 prompt 优于 workflow（44.6% vs 37.9%）
等 4 处——「workflow 恒好」在 4 任务均值上成立、单任务有例外。

**批判清单**（第三方 + HN 高赞，按权重排序）：

1. **基准全部自评自裁**。上表数字全部来自官方自建评测；参考答案是「GPT-6 Astra 与 Fable 5.1
   预测的平均」（偏向 OpenAI/Anthropic 风格，官方自认可能**低估** Jev，但同样是自辩）；
   workflow 由自家模型能力团队构建（偏差可能）；LLM 对照组套的是官方 System One 适配器
   （比无约束模式更慢更贵）；**Jev 无 prompt 基线**——「workflow vs prompt」主张对 Jev 本身未测。
   官方在 HN 承认「刻意不公布公开基准上的成绩」，社区反问（jceg）：「分数好的话他们肯定就发了」。
2. **校准是最核心也最未经检验的声明**。「更高置信→更高准确」的群体性质至今无独立复测；
   TrueFoundry：「校准正是需要独立测试的那个声明」；开源复刻生态（§7.4）拼尽全力也只复现了
   类型安全与速度，**74% 一致率**是社区复现的上限——校准差距未被量化。在自家领域验证前，
   阈值只能当超参数调。
3. **「不会幻觉」营销话术的社区反弹**（HN 最长争论串）：WhitneyLand「类型安全 ≠ 事实正确」；
   nkozyra「把狗标成猫不能靠『那不算幻觉』开脱」；bigglebear 实例——用户要的是「明天回电」，
   模型只能答 Yes/No，**无法限定条件或弃权**（答案空间里没有 abstain 语义，除非你显式加选项）。
   CEO 本人在 HN 承认模型可以「confidently wrong」。发布帖标题发布后被修改
   （原「40-400x cheaper and 20-200x faster」被指误导）进一步伤信誉。
4. **Ronacher 的转嫁论**（Armin Ronacher，Earendil CTO / Pi 作者，TechCrunch）：Jev
   「把幻觉问题部分转嫁给用户」——用户要自己判断 50% 是掷硬币还是 95% 可行动；
   LLM 便宜又被补贴，行业此前没有动力做这类创新，「我们应该更早看到这个方向」。
5. **客户证言是公司转述**：Vercel（Jev 换掉 OpenAI 分类器，**5–18× 更快且更准**）、
   Bryo AI（vs Gemini 略准但贵 10–20×）均出自 TechCrunch 引述公司口径，非独立审计。
6. **定价可持续性存疑**。$0.042/MTok 可能是早期补贴价（官方自己说「无法证明不是补贴价，
   但预期价格只会降」）；限流动态调整、可无预告变更。
7. **节省上限由「判定类占比」封顶**（flaviocopes 的账）：月 $10k 账单、其中 $6k 是分类判定类，
   换成 5% 成本→省 $5.7k（57%）；若判定只占 10%，节省上限就是 10%。**按「每解决一个任务的
   成本」算账，别按每 token**——重试、人工复核、仍需的生成步骤都会吃掉节省。
8. **文本输入边界**：无图像/音频；资产类工作流要先转文本，多一道转换成本。
   HN 用户拿它画图测试（「Jev Can't See. I Made It Guess What I Drew Anyway」）属玩具边界。
9. **行为漂移**：`jev-latest` 别名随发版漂移；版本+问题+阈值要**三元组一起钉版本并重放标注集**
   （flaviocopes），TCER `typesafe_client` 固定发送版本化 model ID 是同一纪律。
10. **术语门槛**：官方生造词 noul（= **Bernoulli** 缩写，HN 社区后来才破译）在文档未定义，
    发布首日造成理解摩擦；Doom demo 则是最大信誉加分项（「forget LLM benchmaxxing sidequests」）。
11. **中文媒体的批判性分析**（36氪/腾讯科技，第三轮）：①「用竞争对手的下一代模型当裁判，
    再宣布自己在性价比上大获全胜——**自证闭环自带公关色彩**」；②定价「低到让人怀疑在烧
    4000 万融资打价格战」，高并发真实企业流量下能否维持超低延迟+收支平衡存疑；③Reddit 调侃
    「行业又重新发现了分类模型」，反方则认为「带 LLM 级语义理解的通用分类器，若成本速度
    数据成立意义不小」；④**最有价值的批评是给出了正确的实验设计**——应把 Jev、
    Flash 小模型+Constrained Decoding、传统分类器、Embedding 分类器放进同一批真实生产任务，
    统一比较 Accuracy / **Calibration** / Latency / Cost / **OOD 鲁棒性** / **批处理扩展**
    六轴——「目前还没有这类测试」。B 站中文实测另确认一个官方文档没直说的行为：
    **输入乱码时 Choice 仍会选出一个选项**（分布机制使然），不确定性只体现在概率数字里。
12. **RLCD 的必要性已有学术旁证**（firecrawl 论文检索，第三轮）：arxiv 2601.13284 实测
    **RLVR 产出「极端过度自信」的模型**（准确率升、校准崩）；2410.09724 证明 RLHF 的
    口头过度自信源自 PPO 奖励模型；CATTO（2601.23096）等 2026 工作专门做「校准感知训练
    目标」——「偏好对齐会切断预测概率与正确性的联系」已是共识问题，TypeSafe 是把该问题
    当**主目标**而非修补项的第一家模型厂商。

---

## 9. 与 TCER 的关联与印证（v1.9.2 双引擎实践）

TCER 的 `core/typesafe_client.py` + `llm_prompts`（两阶段级联 `evaluate_dynamics_cascade`）
与本次调研结论逐条对上：

| 调研结论（官方/社区） | TCER 实践（CLAUDE.md #38） |
|---|---|
| 英语为主要训练语言、CJK 准确率降低（models.md） | 题面英文、用户素材保原文；A/B 实测英文 confidence 更高且省 20–35% input token |
| 扇出：一请求多问题并行，几乎不加延迟（fan-out） | 单请求扇出 choice/noul/score 多题型；实测 516 回合会话端到端 **1.87s**、16 里程碑 |
| 问题瘦身：编码代理爱犯「一问一调」（primitives.md 点名） | 砍掉 debt_t{n} 16 问/请求，「先查后改」改为本地确定性推导 `_local_debt_kind` |
| 「select instead of generate」——候选给定再选（skill 指南） | `turnaround_pick`：转折点由候选回合号+none 的 choice 指认，不开放生成 |
| 逐字段验证 + max 门聚合（SDE cascade） | Pass 1 宏观相拓扑 + Pass 2 锁定 T_crit 微观裁决的两阶段级联；Pass 2 失败优雅降级回 Pass 1 |
| 校准是群体性质、单次不保证（ML primer）；低置信建议升级 System 2（→通用 LLM） | 主概率<0.6 或 confidence<0.7 → 报告自动建议切「通用大模型」深挖（只建议不自动请求）；`evidence_tension` 交叉校验官方「单次判定不保证正确」的告诫 |
| typed ≠ correct，判定与叙事分离（TrueFoundry 纠偏） | Jev 只返回类型化判定，报告全文本地模板合成，头部标注「判定数值由 Jev 返回、成文由 TCER 本地合成」 |
| 错误码四分支 + 529 过载（api.md） | `_send_request` 401/422/429/529 细分转译为人类可读错误 |
| 阈值/带宽是领域超参数，需自家数据校准（confidence.md） | 责任占比（blame_ai/user/env）归一化为份额；跨语言 A/B 验证判定稳定性 |

一个反向印证：官方 jaggedness #8「结构不变量不保证」提醒——TCER 若未来给 Jev 加
「同一事实的两种问法交叉验证」，不能假设两个 Noul 概率互补（实测可和为 1.19），
必须各自独立定阈值。

---

## 10. 工程落地清单

### 10.1 六问适配测试（pjburnhill gist，社区流传最广的方法论）

一个任务适合 Jev 与否，逐项打分（是=强信号）：

1. **判断而非创造**？（决定 vs 产出内容）
2. **有界**？（答案空间能预先穷举）
3. **原子**？（单一聚焦判断，不是复合推理）
4. **上下文自含**？（所需信息放得进 64k state）
5. **快人**？（专家 5 秒能答，不用查资料）
6. **机器消费**？（软件直接用结果分支）

6 问全中=优秀候选；3–4 中=先拆解，Jev 接其中判定件；≤2 中=换技术。
最短检验：需求能否表述为「给定这个 state，告诉我 X」，且 X 是 Choice/Score/概率三型之一。

### 10.2 七步渐进上线（flaviocopes，shadow-mode 优先）

1. 先把确定性版本写完 → 2. 找出一个真正需要语义理解的决策点 → 3. 调用前先写全候选答案 →
4. 收集 ≥20 条真实输入+期望答案 → 5. **影子运行**（Jev 并行跑、只记录不改行为）→
6. 复盘错误、调问题措辞 → 7. 只自动化低风险分支。
灰度口诀：**"one branch at a time"**（一次接管一个分支）；高风险路径永远留给置信门+人工。

### 10.3 反模式速查（官方 jaggedness + 社区综合）

- ❌ 让 Jev 算术/计数/比大小（含版本号比较、日期窗口）→ 代码算，模型只抽取枚举分量
- ❌ Score 期望值当连续物理量插值 → 只做阈值/排名
- ❌ 一问多判断（「愤怒且要退款」）→ 拆两问代码合并
- ❌ Noul 0.5 当「中等程度」→ 程度用 Score
- ❌ state 里堆无关材料（context rot）→ 代码先过滤
- ❌ 链式 Choice 逼它生成文本 → 候选生成给正则/LLM，Jev 只选
- ❌ Choice 不设逃生口/候选覆盖不全 → 模型选不了没列的值，漏检在调用方
- ❌ 跨问题假设概率恒等式（否定的 Noul 和=1）→ 实测 1.19
- ❌ 钉了阈值却用 `jev-latest` 别名 → 版本+问题+阈值三元组一起钉，重放标注集

### 10.4 阈值工程纪律（第三轮从框架集成层提炼）

- **阈值拟合要数据**：jevcal 的经验线——**<100 行标注拟合出的逐问题阈值不可信**；
  拟合→留出集验证→估算 fallback 流量占比→CI 重检，四步缺一不可。
- **confidence 语义分字段读**（pydantic-ai）：布尔字段是离**你所用阈值**的 margin
  （不是离 0.5）；多字段输出取**最不确定字段**；float 无 confidence，概率即答案。
- **监控 fallback 触发率**：Jev→LLM 级联的省钱效益 = 1 − 触发率；只看准确率会掩盖
  「全 handoff = 全价」的失败模式。
- **fail closed**：低置信、陈旧/捏造 ID 一律向安全侧失败（微软 agent-framework 提案原则）。
- **只选最优就别设阈值**（官方 agent skill）：排序场景直接取最高概率，阈值是为副作用
  gating 用的，不是到处都挂。

---

## 11. 参考来源

**官方一手**
- 发布博客：typesafe.ai/blog/introducing-system-one-models-and-jev（2026-09-15，Diogo Almeida）
- 官方新闻稿：businesswire.com（TypeSafe AI Emerges From Stealth With $40M，2026-09-15；
  Yahoo Finance 转载；seedtable/dealroom 数据库条目）
- 手册索引：docs.typesafe.ai/llms.txt（Mintlify，页面加 `.md` 直读）
- 手册核心页：introduction / concepts(system-one·state·use-case-map·how-to-build) /
  primitives(总览·choice·score·noul·advanced) / confidence / models / api /
  model-jaggedness/jev-1.13 / patterns(fan-out·confidence-routing·composite-scoring·intent-routing) /
  agent-skill / demos(smart-home)
- 官方基准站：evals.typesafe.ai（4 workflow 全量数字，§8 表）
- GitHub org：github.com/typesafe-ai（skills 1.3k★ / system-one-adapter-python 209★ /
  typesafe-sdk-js 191★ / typesafe-sdk-python 162★ / **LLaDA fork** / vllm fork / daggerverse /
  Overwatch / pulumi-clickhouse）
- Cookbooks：parallel_questions · hierarchical_classification · sde_cascade ·
  classification_using_confidence · consistency_noul · citation_check（其余 12 篇见索引）

**社区生态**
- awesome-jev-projects 雷达站：logicrw.github.io/awesome-jev-projects/（数据端点 projects.json，
  2026-09-20 快照 358 项；仓库 github.com/logicrw/awesome-jev-projects）
- OmniJev/awesome-jev（**开源复现专列**：papers + reproductions 全景）
- Anil-matcha/awesome-jev-by-typesafe（GitHub，用例/模式/起步代码合集）
- pjburnhill 综合参考 gist（六问测试、Observe→Judge→Reason→Act 架构）
- HN 发布帖 1929 分/509 评论：news.ycombinator.com/item?id=49717558（经 hn.algolia.com
  API 全量提炼；CEO CompleteSkeptic / 员工 zenlikethat 直接参与）
- jevai.wiki（独立社区 wiki：模型卡跟踪/事实核查/三通道发行 confirmation）

**第三方独立分析**
- techcrunch.com/2026/09/18/a-new-kind-of-ai-model-from-a-chatgpt-inventor-is-thrilling-developers
  （**独家采访**：纯合成数据、架构保密、Vercel/Bryo 证言、Ronacher 批判）
- flaviocopes.com/jev（最全面的独立深读：边界/成本账/七步上线）
- langchain.com/blog/building-a-harness-with-jev（harness 集成范式）
- datacamp.com/blog/system-one-models-jev（第三方复测基准数字）
- truefoundry.com/blog/typesafe-ai-jev（对「零幻觉」与校准声明的批判性解读）
- latent.space AINews 两期（「2 天 6 克隆」盘点 + 发布反应；MParakhin 观察）
- apidog.com/blog/openjev-open-source-jev-alternatives（三条复刻路线对比，
  「None of them reproduce RLCD」结论）
- huggingface.co/spaces/multimodalart/jev-reproductions-tracker（复现追踪：~74% 一致率）
- HF harshatheg/Qwen-2.5-1B-RLCD（并行受限解码白皮书式 README）
- dev.to/valyuai（实操指南：限流/版本钉扎）、requesty.ai、mindstudio.ai、marktechpost、
  braintrust.dev（eval 集成）、vercel.com/kb/guide/typesafe-jev-and-ai-sdk（官方 KB）、
  r/LocalLLaMA（同型架构先行者争议帖）

**中文社区（第三轮，tinyfish/tavily 检索）**
- 36氪/腾讯科技三篇：「一个『不说话』的AI刷屏，Jev真是新范式吗？」
  （m.36kr.com/p/3988164509711361，六轴对照实验设计）、「那个教 ChatGPT 说话的人，
  做了一个『哑巴』模型」（估值 ~2 亿美元）、「Jev爆火硅谷，哑巴AI卷疯14万开发者」
  （36 小时 14 万开发者、谷歌 Gemma 官方跟进、审 PR 成本对比）
- 央广网/搜狐转载：「APUS 交出全球首批跨平台开源复现」（fast-browser-use，
  Qwen3.5-9B 本地复现 KV-Cache 广播，M2 Pro 离线 18s）
- 知乎专栏 2084426129818100841（红绿灯/判卷老师类比）；澎湃/东方财富（具身智能视角）；
  B 站实测视频（playground 全流程：乱码仍必选、概率承不确定性）

**框架集成与开发者索引（第三轮，firecrawl developer_search）**
- pydantic-ai 官方文档 docs/models/typesafe.md（confidence=margin 语义、fallback 触发率）
- jevcal CLI（killop/anything_about_game 收录：标注拟合阈值、<100 行不可信）
- microsoft/agent-framework#8556、awslabs/aidlc-workflows#1271（fail closed 集成提案）
- typia evaluation()（适配器无校准论）、Langfuse 博客（eval 三路分流）、
  significant-gravitas/autogpt（Yes/No block + truncation 字段）、basedhardware/omi#14835

**学术脉络（arxiv / semanticscholar 第二轮 + firecrawl 论文检索第三轮）**
- LLaDA 族（github fork 线索的学术背景）：LLaDA（2502.09992，未直接检索但族系如下）、
  LLaDA-V（2505.16933）、LLaDA-MoE（2509.24389）、LLaDA-MoE v2（2608.03457）、
  LLaDA 1.5（2505.19223，**扩散 LM 的偏好对齐/方差缩减 RL**——RLCD 的学术近邻）
- **RLCD 问题域的学术实证**（第三轮）：2601.13284（RLVR 产出极端过度自信模型，SFT/RLVR
  校准系统研究）、2410.09724（RLHF 口头过度自信源于 PPO 奖励模型）、CATTO 2601.23096
  （校准感知 token 级训练目标）、2404.02655（忠实度分解置信度）、2502.11028（干扰项缓解
  失准）、2510.20369（奖励模型不确定时路由强判官）、2605.09702（Calibrate, Don't Curate：
  校准全面板优于择优面板）、2604.13717（LLM-as-Judge 四种改进技术）
- Discrete Stochastic Localization for Self-Correcting Diffusion LMs（semanticscholar）
- 扩散 LM 变长生成（2602.07546）、扩散理论教程（2605.22586）
- 注：TypeSafe 自身无论文；RLCD 机制仅有 CEO 推文与 HN 转述，待官方发表后修订

**TCER 内部对照**
- `tcer/core/typesafe_client.py`（v1.9.2 实现）· `tcer/core/llm_prompts.py`（级联题面与
  里程碑提取）· CLAUDE.md 注意事项 #38（双引擎设计决策全文）
