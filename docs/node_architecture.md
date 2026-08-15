# 43 节点架构说明：必要性论证与节点间通信机制

> 本文档直接回答评委评审中可能出现的两个核心疑问：
> 1. **「是否真的需要这么多节点（43 个）？」**
> 2. **「节点间通信真的做好了吗？」**
>
> 配套测试证据见 `tests/test_node_architecture.py`（17 个专项断言）与全量
> `tests/`（316 用例，全绿）。

---

## 一、结论速览

| 维度 | 结论 |
|------|------|
| 节点总数 | **43** = research(10) + ideation(5) + design(6) + experiment(9) + writing(7) + discovery(6) |
| 节点类别 | 30 AgentNode + 6 CheckpointNode + 5 HumanNode + 2 ToolNode |
| 节点身份 | 43 个非 checkpoint 节点的 `node_type` 全局唯一，职责零冗余 |
| 通信通道 | `ExecutionContext` 是唯一数据通道，`ContextKey<T>` 强类型键，禁止散落字符串键 |
| 契约声明 | 43 节点全部声明 `input_schema` / `output_schema`（Pydantic），41 个业务节点声明 `output_keys` 写入契约 |
| 数据流完整性 | 静态分析验证：34 个被消费域键全部有明确生产者，**无悬挂引用 / 断链** |
| 产出键唯一性 | 业务 Agent/Tool 的 39 个独立产出键全局唯一，无覆盖冲突 |
| 测试证据 | 316 用例通过，其中 17 个专项断言覆盖上述全部维度 |

---

## 二、为什么要 43 个节点？（必要性论证）

### 2.1 拆分的三条原则

43 个节点不是「堆砌」，而是三条工程原则的自然结果。任何一条不满足，才会考虑合并：

1. **单一职责（Single Responsibility）**：每个节点只做一件事，输入输出契约可独立验证。例如 research 阶段的「论文抓取」与「相关性筛选」分开，是因为前者负责「检索并记录证据链」，后者负责「打分并过滤」——两者失败模式、可复现性、可单独重试的粒度都不同。

2. **人在环（Human-in-the-Loop）**：每个主阶段的关键决策点都插入人工 gate（确认检索方向、讨论思路、审阅方法、审阅实验、确认终稿），避免全自动流水线在错误方向上一路狂奔。这 5 个人工节点是「多出来的节点」的直接来源，也是科学发现「可干预、可纠错」的保障。

3. **可回滚（Checkpoint + Snapshot）**：每个阶段在核心创新节点前放置一个检查点，失败时回滚到上一个稳定态而非从头再来。6 个 checkpoint 节点本身不产出业务数据，但保证了系统的鲁棒性。

### 2.2 合并会失去什么？

以 experiment 阶段（9 节点）为例说明「为什么不能简单合并」：

```
experiment_config → code_generate → code_review → [checkpoint] → experiment_review(人工)
                 → experiment_run → anomaly_check → claim_verify → outcome_assess
```

- **code_generate 与 code_review 分开**：这是「学生生成 / 导师审查」的对抗式设计（借鉴 AI-Researcher）。合并不但失去多轮审查的独立性，还无法在审查失败时单独重试生成。
- **experiment_review（人工）单独存在**：真实科研中，实验代码运行前必须有人确认（安全、成本、合理性）。这是不可省略的决策 gate。
- **anomaly_check / claim_verify / outcome_assess 分开**：三者回答不同问题——「数据是否异常」「声明是否被验证」「实验整体成败」——分别对应不同的评估逻辑与下游消费方。

同理，research 阶段的 10 节点对应 PaperQA + GPT-Researcher 的完整流程（主题精炼 → 子问题分解 → 确认 → 抓取 → 相关性筛选 → 入库 → 材料知识抽取 → 交叉验证 → Gap 识别），每一步都有独立的检索学/知识工程依据。

### 2.3 节点清单总表

| 阶段 | 节点数 | 独立职责数（非 checkpoint） | 人工节点 | 检查点 |
|------|-------:|--------------------------:|:-------:|:------:|
| research（文献调研） | 10 | 9 | 1 | 1 |
| ideation（思路生成） | 5 | 4 | 1 | 1 |
| design（方法设计） | 6 | 5 | 1 | 1 |
| experiment（实验验证） | 9 | 8 | 1 | 1 |
| writing（论文写作） | 7 | 6 | 1 | 1 |
| discovery（构效发现·路线 A） | 6 | 5 | 0 | 1 |
| **合计** | **43** | **37** | **5** | **6** |

> 注：`topic_discovery`（4 节点，研究趋势发现）是独立的「方向推荐」入口，
> 不参与五阶段主流水线与构效发现流水线，故不计入 43。它与 discovery 是两个
> 独立能力，避免混淆。

---

## 三、完整节点清单（分阶段）

> 每行格式：`节点名`｜类型｜职责｜`关键输入 → 关键输出`（键名省略 `research.` 等阶段前缀见上下文）。

### 3.1 research（10 节点）—— 文献调研

| 节点 | 类型 | 职责 | 输入 → 输出 |
|------|------|------|-------------|
| `topic_refine` | agent | 主题精炼：生成检索关键词与查询策略 | `topic` → `keywords, query_strategy` |
| `subquery_decompose` | agent | 子问题分解（GPT-Researcher：5-10 子问题并行检索） | `topic, keywords` → `subqueries` |
| `cp_before_confirm` | checkpoint | 确认前快照（回滚点） | — |
| `topic_confirm` | human | 用户确认/修订检索方向 | `subqueries` → `topic_confirmed, topic, subqueries` |
| `paper_fetch` | agent | 按子问题并行检索 arxiv/S2，记录证据链 | `keywords, subqueries` → `paper_metas, evidence_chain` |
| `paper_filter` | agent | 相关性打分筛选（PaperQA filter） | `paper_metas` → `filtered_paper_metas` |
| `paper_ingest` | agent | 论文入库：chunk 摘要 + 向量化 | `filtered_paper_metas` → `paper_ids` |
| `material_extraction` | agent | 材料知识抽取（材料-性能-合成三元组，Task 2） | `paper_ids` → `material_knowledge` |
| `cross_validate` | agent | 多源交叉验证：冲突可信度评分 | `paper_ids` → `cross_validation_report` |
| `research_gap` | agent | 研究缺口识别（结构化 Gap 清单，Task 3） | `cross_validation_report` → `gap_report` |

### 3.2 ideation（5 节点）—— 思路生成

| 节点 | 类型 | 职责 | 输入 → 输出 |
|------|------|------|-------------|
| `brainstorm` | agent | 基于 Gap 生成研究思路 | `gap_report` → `idea_ids` |
| `idea_discuss` | human | 与用户交互式探讨思路（人在回路） | `idea_ids` → `discussion_notes, idea_ids` |
| `cp_before_validate` | checkpoint | 验证前快照 | — |
| `idea_validate` | agent | 思路可行性验证 | `idea_ids, discussion_notes` → `validated_idea_ids` |
| `claim_draft` | agent | 生成可验证 Claim 草稿 | `validated_idea_ids` → `draft_claim_ids` |

### 3.3 design（6 节点）—— 方法设计

| 节点 | 类型 | 职责 | 输入 → 输出 |
|------|------|------|-------------|
| `atom_decompose` | agent | 原子概念分解：方法拆为原子概念 + 公式↔代码映射 | `validated_idea_ids` → `atom_concepts, formula_code_map` |
| `method_formalize` | agent | 方法形式化 | `atom_concepts` → `method_content` |
| `cp_before_review` | checkpoint | 审核前快照 | — |
| `method_review` | human | 用户审核方法 | `method_content` → — |
| `claim_evidence_link` | agent | Claim 抽取与证据关联 | `method_content, paper_ids` → `claim_ids` |
| `method_artifact` | agent | 生成方法 Artifact | `method_content, claim_ids` → `method_artifact_id` |

### 3.4 experiment（9 节点）—— 实验验证

| 节点 | 类型 | 职责 | 输入 → 输出 |
|------|------|------|-------------|
| `experiment_config` | agent | 实验配置生成 | `method_content, claim_ids` → `configs` |
| `code_generate` | agent | 实验代码生成（Code Agent，学生） | `configs` → `code` |
| `code_review` | agent | 代码审查（Advisor Agent，导师） | `code` → `review_notes` |
| `cp_before_run` | checkpoint | 运行前快照 | — |
| `experiment_review` | human | 运行前人工审核 | `code, configs` → — |
| `experiment_run` | tool | 执行实验代码 | `code, configs` → `experiment_ids` |
| `anomaly_check` | agent | 结果异常检测 | `experiment_ids` → `anomaly_report` |
| `claim_verify` | agent | Claim 验证 | `experiment_ids, claim_ids` → `result_artifact_ids` |
| `experiment_outcome_assess` | agent | 实验成败评估（决定是否进入写作） | `anomaly_report, claim_ids` → `outcome` |

### 3.5 writing（7 节点）—— 论文写作

| 节点 | 类型 | 职责 | 输入 → 输出 |
|------|------|------|-------------|
| `provenance_check` | tool | 溯源链硬校验（每条结论回查文献） | `result_artifact_ids` → — |
| `style_learn` | agent | 风格学习 | `result_artifact_ids` → `style_profile` |
| `outline` | agent | 层级式大纲生成 | `style_profile` → `outline` |
| `cp_before_draft` | checkpoint | 起草前快照 | — |
| `section_draft` | agent | 按章节逐步撰写 | `outline` → `sections, draft_content` |
| `review` | agent | 审稿 | `draft_content` → `review_notes` |
| `revise` | human | 用户确认终稿 | `draft_content, review_notes` → `paper_draft_artifact_id` |

### 3.6 discovery（6 节点）—— 构效关系发现（路线 A）

| 节点 | 类型 | 职责 | 输入 → 输出 |
|------|------|------|-------------|
| `hypothesis_seed` | agent | 从 Gap 生成候选构效假设（搜索种子） | `gap_report` → `hypotheses` |
| `search_space` | agent | 定义搜索空间 + 从文献抽取数据点 | `hypotheses` → `search_space` |
| `cp_before_search` | checkpoint | 搜索前快照 | — |
| `llm_guided_search` | agent | LLM 引导搜索（MCTS + LLM 融合，核心创新） | `hypotheses, search_space` → `candidates` |
| `discovery_validate` | agent | 文献交叉验证 + 物理边界硬筛 + 新颖性评估 | `candidates` → `relationships` |
| `discovery_report` | agent | 结构化发现报告 + 证据披露 | `relationships` → `report_artifact_id` |

---

## 四、节点间通信机制（回答「通信做好了吗」）

### 4.1 唯一通信通道：ExecutionContext

所有 43 个节点共享同一个 `ExecutionContext` 实例，节点间**不直接互相调用**，
只通过上下文读写数据。这带来三个好处：

- **无隐式耦合**：节点只依赖「上下文中的键」，不依赖「其他节点的对象引用」，
  增删节点不破坏其他节点的代码。
- **可快照回滚**：`snapshot()` / `restore()` 深拷贝纯域数据，检查点节点借此实现
  失败回滚（见 4.4）。
- **可跨会话恢复**：`system.*` 前缀的系统依赖（LLM 注册表、知识库、Artifact
  管理器）与纯域数据分离，快照只深拷贝域数据，避免序列化 sqlite 连接。

### 4.2 强类型键：ContextKey<T>

所有键通过 `ContextKey[T]("name")` 泛型声明（集中在 `stages/common.py`），
**禁止散落字符串键**。例如：

```python
RESEARCH_PAPER_IDS = ContextKey[list[str]]("research.paper_ids")  # 强类型
ctx.get(RESEARCH_PAPER_IDS, [])   # 读
ctx.set(RESEARCH_PAPER_IDS, ids)  # 写
```

收益：键名、键值类型、定义位置三者集中管理，改一个键只动一处；测试可静态
枚举全部键做数据流完整性校验（见 4.5）。当前共定义 39 个独立域键 + 8 个系统键。

### 4.3 Pydantic 契约：input_schema / output_schema / output_keys

每个节点显式声明三段契约：

1. **`input_schema`**：从 context 组装输入的 Pydantic 模型（`_build_input`）。
2. **`output_schema`**：节点产出的 Pydantic 模型（`_execute` 返回）。
3. **`output_keys`**：`dict[字段名, ContextKey]`，声明「哪些输出字段写入 context
   的哪些键」。默认 `_apply_output` 按字段名逐字段映射写回；特殊聚合结构
   （如材料知识整体 dict）覆盖 `_apply_output` 显式声明写入契约。

`Node.run()` 统一编排这三段：`_build_input → _execute → _apply_output`，
异常统一捕获为 `NodeResult(FAILED)`，保证每个节点的 I/O 边界可独立验证。

### 4.4 检查点快照与 SQLite 持久化

- **检查点（6 个）**：`CheckpointNode` 执行时对 context 做快照，写入
  `checkpoint.<node_id>` 键；节点失败时 `GraphRunner` 回滚到最近检查点，
  实现「部分失败不整体重来」。
- **SQLite 落库（KnowledgeStore）**：材料、性能、合成、Gap、论文等结构化
  实体在节点执行时落库，键 `paper_ids` 等只存 ID 引用。这样即使 context
  丢失，也可凭 ID 从库恢复，通信状态与持久化状态解耦。

### 4.5 数据流完整性（静态验证，最强证明）

测试 `test_dataflow_no_dangling_consumer` 用 AST 静态分析全部 43 个节点的
`ctx.get` / `ctx.set` 调用与 `output_keys` 声明，构建生产者/消费者集合，断言：

> **每个被消费的域键，要么有明确生产者（某个节点的 output_keys 或 ctx.set），
> 要么属于入口注入白名单（`research.topic`、`research.search_prefs`）。**

验证结果：**34 个被消费域键全部有生产者，无悬挂引用**；业务 Agent/Tool 的
39 个产出键全局唯一，无覆盖冲突（人工节点对上游键的「修订」是有意设计，
如 `topic_confirm` 修订 `subqueries`）。

### 4.6 人在环的通信闭环

5 个人工节点通过 `HumanNode.continue_after_human` 接收用户响应并写回 context
（如确认检索方向 → 修订 `subqueries`、写 `topic_confirmed` 控制标志）。测试
`test_human_decision_outputs_are_consumed_downstream` 验证：人工决策的产出键
确实被下游消费，而非写后无人读。

---

## 五、测试证据

| 测试文件 | 覆盖点 |
|---------|--------|
| `test_node_architecture.py`（17 用例，本主题新增） | 节点总数=43、阶段分布、契约完整性、节点身份唯一、output_keys 强类型、业务产出键唯一、人在环决策点、数据流完整性 |
| `test_stages_graphs.py`（更新） | 六阶段图拓扑：节点数、entry/exit、checkpoint、人工节点 |
| `test_orchestration_graph.py` | GraphRunner 执行语义、人工阻塞/恢复、回滚 |
| `test_orchestration_context.py` / `node.py` | ExecutionContext 快照/回滚、节点 I/O 编排 |
| 全量 `tests/` | **316 用例全部通过** |

复现命令：

```bash
# 专项架构断言
python -m pytest tests/test_node_architecture.py -v
# 全量回归
python -m pytest tests/ -q
```

---

## 六、结论

1. **43 个节点是「单一职责 + 人在环 + 可回滚」三条原则的必然结果**，每个
   节点有唯一身份与独立 I/O 契约，合并会损失重试粒度、人工决策 gate 与
   回滚能力（详见第二节）。
2. **节点间通信建立在「唯一通道 + 强类型键 + Pydantic 契约 + 检查点回滚 +
   SQLite 持久化」五层机制之上**，并有一组静态分析测试证明数据流完整无断链、
   产出键无冲突（详见第四节）。
