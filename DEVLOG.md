# DEVLOG — 科研 Agent 系统开发推进日志

> 本文档统一记录每次核心推进，便于开发与调整溯源。
> 格式：每轮推进一个章节，含「目标 / 改动 / 验证 / 问题与修复 / 下一步」。

---

## 2026-07-31 第三轮：writing 阶段启用 + 端到端真实运行 + 4 个关键 Bug 修复

### 目标
1. 启用 writing 阶段 5 个 Agent 的真实 LLM 调用（此前 research/ideation/design/experiment 已启用）
2. 端到端真实运行验证（research→experiment，定位并修复问题）
3. 为后续「自动化科研」目标打牢管线基座

### 改动清单

#### 1. `stages/writing/agents.py` — writing 阶段真实调用启用
- 新增 Pydantic 结构化输出 schema：
  - `StyleLearnSchema`（语态/术语密度/章节结构/引用风格/段落长度）
  - `OutlineSchema` + `SectionPlan`（层级式大纲，每章关联 claim_ids）
  - `SectionDraftList` + `SectionDraftItem`（按章填充）
  - `ReviewSchema` + `ReviewDimension`（结构/引证/表达三维度审稿）
- 4 个 Agent 启用 `dry_run` 分支 + try/except 回退占位：
  - `StyleLearnAgent`：装载 EXPERIMENT_RESULT artifact 摘要作为风格学习样本
  - `OutlineAgent`：装载 Claim briefs，规划 5-7 章；新增 `_ensure_claim_coverage` 兜底，自动把未引用 Claim 挂到 Method 章
  - `SectionDraftAgent`：装载全部 Claim/Experiment 素材，按大纲逐章生成；新增章节补齐兜底
  - `ReviewAgent`：三维度审稿，`_format_review` 把结构化结果转 Markdown
- `ReviseHuman._build_output_from_response`：真实模式经 `ArtifactManager.create_artifact` 产出 `PAPER_DRAFT` Artifact（含版本管理），dry_run 用静态 ID 占位

#### 2. `core/orchestration/graph.py` — **关键 Bug 修复**
- **问题**：`GraphRunner.resume_after_human` 只调用 `_advance()` 一次，未继续 `_step()` 循环
- **后果**：人工节点后的所有节点（如 research 阶段的 paper_fetch/paper_filter/paper_ingest/cross_validate）全部不执行，导致 brainstorm 显示「基于 0 篇调研论文」
- **修复**：在 `_advance()` 后追加 `self._step()`，让后续节点继续跑完
- **验证**：dry_run 后 history 出现 paper_fetch(12篇) → paper_filter → paper_ingest → cross_validate(consensus=6)，brainstorm 改为「consensus=6」

#### 3. `stages/experiment/agents.py` — CodeGenerate 改用 complete
- **问题**：`CodeGenerateAgent` 用 `structured_output(CodeArtifactSchema)`，MiniMax M3 在代码生成场景倾向返回纯代码/markdown 代码块，强制 `json_object` 触发 JSON 解析失败
- **修复**：改用 `registry.complete()` + system prompt 要求 ```` ```python ```` 代码块包裹；新增 `_extract_code_block` 静态方法提取代码
- **验证**：首轮真实运行 code_generate 仅 578 字符（占位回退），第二轮预期生成真实代码

#### 4. `core/llm/providers/openai_provider.py` — schema-echo 检测
- **问题**：MiniMax M3 偶尔把输入的 JSON Schema 原样返回（含 `$defs`/`$schema`/`properties`），而非生成实例数据，导致 `BrainstormSchema` 等校验失败
- **修复**：
  - 新增 `_looks_like_schema(text)` 检测顶层 schema 标志键（`$defs`/`$schema`/`$id`/`definitions`），命中则抛 LLMError
  - 改进 `_build_messages_for_structured` prompt：明确「生成实例数据，不要返回 schema 元字段」+ 列出需填充的字段名作为强提示
- **配套**：`core/llm/registry.py` 的 `structured_output` 加一次重试：首次失败若为 schema-echo 或 schema 不符，追加「请返回实例数据」强提示重试一次

#### 5. `stages/research/agents.py` — paper_filter 安全网
- **问题**：`PaperRelevanceFilterAgent` 阈值 0.5，LLM 打分过严时可能全剔除，导致下游 paper_ingest/cross_validate 饿死
- **修复**：若 `filtered` 为空且 `rejected` 非空，按 `relevance_score` 降序保留 top 3 篇，打上「安全网」标记
- **设计依据**：一次 LLM 打分失误不应饿死整条研究链路

### 端到端验证结果

#### dry_run 全 5 阶段（force_writing）
- status: **completed**
- completed_stages: research / ideation / design / experiment / writing
- 24 个节点全部 success（5 Checkpoint + 19 Agent/Tool/Human）
- experiment_outcome.success=False（dry_run 占位数据诚实返回）
- writing 5 节点跑通：provenance_check → style_learn → outline(5章) → section_draft(7500字) → review → revise

#### 真实运行 research→experiment（stop_before=writing）
- status: **experiment_failed**（符合「实验失败是科研常态」设计，不强行写论文）
- completed_stages: research / ideation / design / experiment
- 关键真实产出：
  - topic_refine: 10 个真实关键词（federated learning incentive mechanism 等）
  - paper_fetch: 5 篇真实 arxiv 论文入库（S2 因 429 限流优雅降级）
  - atom_decompose: 3 个原子概念 + 3 条公式↔代码映射
  - method_artifact: 真实创建方法 Artifact（v1）
  - claim_evidence_link: 4 个 Claim 关联证据
  - code_generate: 8446 字符真实代码（首轮因 JSON 失败回退 578 字符占位，本轮修复后预期真实生成）
  - experiment_run: 2 个真实实验运行
  - experiment_outcome_assess: 「建议中止」（2/2 实验有异常，0 Claim 验证）

### 已知遗留问题
1. **Semantic Scholar 429 限流**：免费 API 限流严重，arxiv 已足够；S2 失败时优雅降级，不阻塞
2. **实验代码质量**：LLM 生成的实验代码可能跑不通（依赖缺失/语法错误），这是 AI-Researcher 范式的固有挑战，CodeReview 多轮迭代 + experiment_failed 自然停止是预期应对
3. **嵌入模型缺失**：MiniMax 无嵌入 API，未配 OPENAI_API_KEY 时向量检索降级为关键词检索（不阻塞）

### 下一步
- 第二轮真实运行验证 4 个修复（进行中）
- 完善 README + 方法设计↔代码对齐可视化工具
- 探索 MCP/Skill 插件配备

### 第二轮真实运行验证结果（4 个修复全部生效）

| 指标 | 首轮（修复前） | 二轮（修复后） |
|------|------|------|
| paper_fetch | 5 篇 | **36 篇** |
| paper_filter 保留 | 0 篇（全被剔除） | **6 篇**（自然保留，安全网未触发） |
| cross_validate | confidence=0.50, 共识 0 | **confidence=0.53, 共识 14, 冲突 8, 缺口 7** |
| brainstorm | 3 思路(占位, gaps=0) | **5 思路(真实, gaps=7, conflicts=8, consensus=14)** |
| idea_validate | 0/3 通过 | **4/5 通过** |
| claim_draft | 0 个 | **8 个 draft Claim** |
| code_generate | 578 字符(占位回退) | **7932 字符(真实代码，complete+extract 修复生效)** |
| experiment_run | 2 个 | 3 个 |
| 最终 status | experiment_failed | experiment_failed（3/3 实验有异常，诚实中止） |

**关键验证点**：
1. ✅ GraphRunner 修复：paper_fetch/filter/ingest/cross_validate 全部执行（首轮被跳过）
2. ✅ CodeGenerate 修复：7932 字符真实代码（首轮仅 578 字符占位）
3. ✅ paper_filter：6 篇自然保留（首轮 0 篇，安全网未触发）
4. ✅ Brainstorm：基于真实 gaps=7/conflicts=8/consensus=14 生成 5 思路（首轮全 0）

**真实产出物**：
- 36 篇 arxiv 论文抓取，6 篇入库
- 5 个研究思路（含 contract theory + VCG 拍卖混合机制等）
- 4 个思路通过验证，8 个 draft Claim
- 方法 Artifact（v1，ID dce0cc1a）
- 7932 字符实验代码
- 3 个实验运行（3/3 有异常，符合 LLM 生成代码的预期）

**遗留问题（非阻塞，优雅回退已覆盖）**：
- AtomDecompose/IdeaValidate/cross_validate 的长中文 JSON 偶发解析失败（LLM 生成 1000+ 字中文 reason 时 JSON 转义脆弱）→ 各 agent try/except 回退占位，流程继续
- 实验代码 3/3 有异常 → experiment_failed 诚实中止，符合「实验失败是科研常态」设计

**结论**：管线架构稳固，4 个关键修复全部生效。研究链路从「0 篇论文/0 思路/0 Claim/占位代码」提升到「36 篇论文/5 思路/8 Claim/真实代码」。实验失败是 LLM 生成代码的固有问题，由 experiment_failed 自然处理。

---

## 2026-07-31 第四轮：README + 项目检查工具 + 清理

### 目标
- 完善项目运行指南（README），让科研人员能独立上手
- 提供方法设计↔代码对齐可视化工具，解决「结果不可验证」痛点

### 改动清单

#### 1. `README.md` — 项目运行指南（新建）
- 核心设计理念（4 痛点 → 4 应对）
- 快速开始：环境准备 → MiniMax 配置 → dry_run → 真实运行
- 项目检查与可视化命令清单
- 架构概览 + 文件结构
- 配置说明（任务路由 / dry_run 开关）
- 已知限制（S2 限流 / 嵌入缺失 / 长 JSON 脆弱 / 实验代码质量）

#### 2. `tools/inspect_project.py` — 项目检查与可视化工具（新建）
- `--section overview`：论文/思路/Claim/实验计数 + 状态分布
- `--section papers`：论文清单（标题/年份/摘要）
- `--section claims`：Claim 清单（状态/角色/证据数）
- `--section experiments`：实验结果（状态/异常/验证的 Claim）
- `--section method`：方法 Artifact 内容预览
- `--section alignment`：方法↔代码对齐（从方法抽取 LaTeX 公式，与实验代码做关键词匹配，标注 mapped/partial/missing）
- 验证：proj_smoke_real 项目概览显示 11 论文/8 思路/16 Claim/5 实验

#### 3. `.gitignore` — 增加临时日志忽略
- 新增 `realrun*.txt` / `run_log.txt` / `_test_api.py`
- 删除 5 个临时日志文件

### 下一步
- 探索 MCP/Skill 插件配备（用户提到的「相关开源mcpskill等工具配备等插件」）
- 优化长中文 JSON 的结构化输出稳定性（考虑 complete + 解析的混合策略）
- 验证 writing 阶段真实运行（需先有实验成功的项目）

---

## 2026-07-31 第五轮：GOAI 大赛方向三适配 + 前端 + Sciverse 集成

### 目标
- 参加 GOAI 世界人工智能开源大赛·赛道三·方向三（材料科学文献驱动的科学发现智能体）
- 构建「强劲工程内核 + 有用前端界面」的完整科研 Agent 系统
- 完成初赛方案说明文档（8.16 截止）

### 改动清单

#### 1. `core/tools/sciverse_search.py` — Sciverse API 工具（新建）
- 赛题明确推荐的科学智能数据库（465M records, 证据片段级检索）
- 5 个接口：`meta_catalog`（字段发现）/ `agentic_search`（语义证据检索）/ `meta_search`（结构化过滤）/ `read_content`（原文回读）/ `is_available`（token 检测）
- 无 token 时优雅降级返回空列表（不阻塞流程）
- `SciverseEvidence` dataclass：片段级证据（含 doc_id + offset，可回读原文）

#### 2. `stages/research/agents.py` — PaperFetchAgent 集成 Sciverse
- 在 `_real_fetch` 的 `fetch_one` 中新增 Sciverse 作为第三数据源（arxiv + S2 + Sciverse）
- `sciverse_is_available()` 检测：无 token 时跳过，有 token 时检索 3 篇证据片段

#### 3. `core/tools/__init__.py` — 导出 Sciverse 工具
- 新增 `SciverseEvidence` / `sciverse_agentic_search` / `sciverse_meta_catalog` / `sciverse_meta_search` / `sciverse_read_content` / `sciverse_is_available`

#### 4. `.env.example` — 新增 Sciverse 配置
- `SCIVERSE_API_TOKEN`（可选，获取地址 sciverse.opendatalab.com/tokens）

#### 5. `web/` — 前端 Web UI（新建，subagent 构建）
- `web/api.py`：FastAPI 后端，9 个 REST 接口，异步执行 Pipeline，HumanCallbackBridge 处理人工节点
- `web/static/index.html` + `app.js` + `style.css`：单页应用，7 个页面（项目创建/研究进度/论文/Claim/实验/灵感笔记/人工交互）
- 学术风格（深蓝/灰白），响应式布局，无构建工具依赖
- 端到端验证通过：创建项目→启动→人工节点交互→阶段推进→产出物计数

#### 6. `docs/competition_proposal.md` — 初赛方案说明文档（新建）
- 方向选择（方向三·材料科学文献驱动智能体）
- 科学问题理解（知识抽取结构化 + Research Gap 可证伪识别 + 构效关系可信发现）
- 技术方案（5 阶段生命周期 + 基本任务 + 路线 A）
- 关键创新点（溯源链硬校验 + 原子概念分解 + 实验失败自然停止 + 多源证据片段级检索）
- 初步实验（联邦学习 36 篇论文 + 材料科学 10 篇论文验证）

#### 7. `materials_run.py` — 材料科学运行脚本（新建）

### 材料科学文献调研验证结果

以「热电材料的构效关系与性能优化」为主题，真实运行 research 阶段：

| 节点 | 产出 |
|------|------|
| topic_refine | 10 个材料领域关键词（thermoelectric materials, ZT optimization, ML materials discovery 等） |
| subquery_decompose | 8 个子问题（热电构效关系、文献挖掘NLP、LLM4Mat、知识图谱、公开数据集等） |
| paper_fetch | 10 篇候选论文（arxiv 检索成功，S2 限流） |
| paper_filter | 保留 1 篇，剔除 9 篇 |
| paper_ingest | 入库 1 篇（"ARIA: A Causal-Aware Framework for Rescuing LLM Reasoning in Trustworthy Materials Discovery" 2026） |
| cross_validate | **7 个 Research Gap** + 2 处冲突 + 4 条共识 + confidence=0.53 |

**基本任务验证结论**：系统在材料科学领域可端到端完成文献调研 Agent 全流程（检索→筛选→入库→交叉验证→Research Gap 识别），产出符合赛题要求。

### 当前系统总览

```
科研 Agent 系统 v0.5
├── 工程内核（5 阶段 24 节点）
│   ├── research:    6 节点（topic→subquery→fetch→filter→ingest→cross_validate）
│   ├── ideation:    5 节点（brainstorm→discuss→validate→claim_draft）
│   ├── design:      6 节点（atom_decompose→formalize→claim_evidence→artifact）
│   ├── experiment:  8 节点（config→code_gen→review→run→anomaly→verify→assess）
│   └── writing:     6 节点（provenance→style→outline→draft→review→revise）
├── 工具层
│   ├── arxiv_search + semantic_scholar + sciverse_search（三源融合）
│   ├── code_runner（沙盒执行）
│   └── text_split（chunk 化）
├── 前端（web/）
│   ├── FastAPI 后端（9 接口 + 人工节点 Event 桥）
│   └── 单页前端（7 页面，学术风格）
├── 检查工具（tools/inspect_project.py）
│   └── 6 section（overview/papers/claims/experiments/method/alignment）
└── 文档
    ├── DEVLOG.md（开发溯源）
    ├── README.md（运行指南）
    └── docs/competition_proposal.md（初赛方案）
```

### 下一步
- 配置 Sciverse API Token（用户需注册 sciverse.opendatalab.com）以启用证据片段级检索
- 增加每子问题抓取数 + 调优 paper_filter 阈值（材料科学 10 篇仅保留 1 篇偏严）
- ~~实现路线 A（构效关系发现）：MCTS/贝叶斯优化 + LLM 融合~~ ✅ 第六轮完成
- 持久化 cross_validation_report（含 Research Gaps）到知识库，便于 inspect 工具展示
- 增强 PaperIngestAgent 抽取材料特定实体（成分/结构/性能/合成条件）

---

## 2026-07-31 第六轮：路线 A 构效关系发现实现 + 前端 discovery 工作流

### 目标
- 实现路线 A（构效关系发现）：MCTS 启发式搜索 + LLM 深度融合 + 文献代理模型
- 前端支持 discovery 工作流 + 强化灵感笔记功能
- 满足赛题「LLM 深度参与搜索过程，而非仅生成搜索代码」的进阶要求

### 改动清单

#### 1. `stages/common.py` — 新增 discovery 阶段域键
- `DISCOVERY_HYPOTHESES` / `DISCOVERY_SEARCH_SPACE` / `DISCOVERY_CANDIDATES` / `DISCOVERY_RELATIONSHIPS` / `DISCOVERY_REPORT_ARTIFACT_ID`
- discovery 不属于标准 5 阶段生命周期，作为 research 之后的可选扩展

#### 2. `config/tasks.yaml` — 新增 5 个 discovery task 类型
- `discovery_hypothesis_seed`（temperature 0.5，生成候选假设）
- `discovery_search_space`（temperature 0.2，定义搜索空间+抽取数据点）
- `discovery_llm_guided_search`（temperature 0.4，核心创新：LLM 评估+剪枝）
- `discovery_validate`（temperature 0.2，文献交叉验证+新颖性评估）
- `discovery_report`（temperature 0.3，结构化报告+物理机制）

#### 3. `core/tools/materials_search.py` — 搜索工具（新建）
- `SearchVariable`：搜索变量定义（continuous/discrete/categorical，含定义域采样）
- `LiteraturePoint`：文献抽取的 (结构, 性能) 数据点（关联 paper_id/chunk_id）
- `SurrogateModel`：文献数据代理模型（加权最近邻插值，纯 Python 无重依赖）
- `MCTSSearcher`：MCTS 启发式搜索器（UCB1 选择 + 候选池 + top-N 排序）
- `perturb_config`：物理合法的配置扰动（MCTS 扩展阶段）
- 设计依据：LLM 是主搜索引擎，代理模型提供文献证据密度的量化参考

#### 4. `stages/discovery/` — 构效关系发现阶段模块（新建）
- `__init__.py` / `io_schema.py` / `agents.py` / `graph.py`
- 5 个节点 + 1 个检查点：
  - `HypothesisSeedAgent`：从 Research Gap 生成候选构效关系假设（搜索种子）
  - `SearchSpaceAgent`：定义搜索空间 + 从论文 chunk 抽取文献数据点
  - `StageCheckpoint`：搜索前快照（便于回滚）
  - `LLMGuidedSearchAgent`（核心创新）：MCTS 循环 + LLM 评估科学合理性 + 剪枝 + 机制解释
  - `DiscoveryValidateAgent`：文献交叉验证 + 新颖性评估（novel/partially_known/known）+ 证据链关联 + Claim 入库
  - `DiscoveryReportAgent`：结构化发现报告 + Artifact
- 核心创新：LLM 不只生成搜索代码，而是参与搜索过程（评估中间结果、引导剪枝、给物理机制）

#### 5. `runtime/pipeline.py` — 新增 `run_discovery` 方法
- 流程：research 阶段（文献调研）→ discovery 子图（构效关系发现）
- 复用 research 产出（论文 + 交叉验证报告），discovery 作为可选扩展
- 支持 resume（复用已完成 research 阶段）

#### 6. `web/api.py` — 新增 discovery API
- `POST /api/projects/{id}/run-discovery`：异步启动构效关系发现
- `GET /api/projects/{id}/discoveries`：获取发现产出（summary + relationships）
- `_run_discovery_thread`：工作线程执行 run_discovery
- `ProjectState` 新增 `discovery` / `run_mode` 字段

#### 7. `web/static/` — 前端 discovery 工作流 + 灵感笔记强化
- 新增「启动构效关系发现」按钮（调用 run-discovery）
- 新增「构效关系发现」结果展示页（计数卡片 + 节点时间线 + relationships 列表）
- 侧边栏常驻「科研灵感记录」组件（多行输入 + Ctrl+Enter + 最近笔记列表）
- 轮询联动：run_mode=discovery 时自动刷新 discoveries

#### 8. `discovery_run.py` — 材料构效关系发现运行脚本（新建）
- 支持 `--real`（真实调用）/ `--resume`（复用 research）/ `--project`（指定项目）

### 验证结果

#### dry_run 端到端验证（discovery_run.py）
- status: **completed**
- 节点历史全 success：topic_refine → subquery_decompose → topic_confirm → paper_fetch(12篇) → paper_filter → paper_ingest → cross_validate(共识6) → hypothesis_seed(1假设) → search_space(2变量,目标ZT) → cp_before_search → llm_guided_search(1候选) → discovery_validate(1发现) → discovery_report(Artifact 61633ff5)
- summary: 构效关系发现完成：1 个假设 → 1 个搜索候选 → 1 条验证发现（0 条 novel）

#### 模块导入验证
- discovery graph 6 节点（5 Agent + 1 Checkpoint）全部加载
- Pipeline.run_discovery 方法可用
- web API discovery 路由注册成功

### 路线 A 核心创新点（区别于 LLM4Mat/ChemCrow）
1. **LLM 深度参与搜索过程**：不只是生成搜索代码，而是评估中间结果科学合理性、引导剪枝、给物理机制
2. **文献代理模型**：从论文 chunk 抽取 (结构, 性能) 数据点构建代理模型，证据可追溯到 paper_id
3. **新颖性显式评估**：novel/partially_known/known 三级，known 的发现置信度低
4. **证据链硬关联**：每条发现关联 paper_id，满足赛题「文献溯源完整性与可信度」

### 下一步
- 真实运行验证（配置 Sciverse Token + MiniMax API）：在热电材料领域跑通 discovery 全流程
- 增加每子问题抓取数 + 调优 paper_filter 阈值，让 discovery 有更丰富的文献数据点
- 持久化 cross_validation_report 到知识库，让 discovery 的 hypothesis_seed 有更扎实的 Gap 依据
- 探索与 Materials Project API 的交叉验证（赛题路线 A 要求）

---

## 2026-07-31 第二轮：MiniMax 配置 + dry_run 全流程验证（前序工作摘要）

### 目标
- 仅开通 MiniMax Plus（¥49/月），完成 API 配置但不执行真实调用，完备前置代码与架构

### 核心改动
- `config/tasks.yaml`：全部 task 路由到 `minimax` provider，model 统一 `MiniMax-M3`
- `.env.example`：MiniMax 国内版 endpoint `https://api.minimaxi.com/v1`，强调 key 与 endpoint 匹配
- `core/llm/registry.py`：仅当 `api_key` 存在才注册 provider（dry_run 无 key 不报错）
- `core/orchestration/context.py`：`snapshot()` 排除 `system.*` 与 `checkpoint.*` 前缀，避免不可序列化对象深拷贝
- `stages/writing/agents.py`：`ProvenanceCheckTool` dry_run 宽松通过
- `runtime/pipeline.py`：`--force-writing` 标志绕过实验成败判断，验证写作架构
- `ExperimentOutcomeAssessAgent`：dry_run 诚实返回 success=False，验证失败处理

### 验证
- dry_run 默认路径（research→experiment）跑通
- dry_run `--force-writing` 全 5 阶段 7 写作节点跑通
- 34 节点（29 Agent/Human/Tool + 5 Checkpoint）IO schema 完整，导入正常

### 启用真实 API
- 复制 `.env.example` 为 `.env`，填 `MINIMAX_API_KEY`
- `SRA_DRY_RUN=false` 或 CLI `--no-dry-run`

---

## 架构基线（持续维护）

### 5 阶段生命周期
```
research   → ideation  → design    → experiment → writing(可选)
6 节点        5 节点       6 节点      8 节点       6 节点
```

### 关键设计决策
1. **实验失败是科研常态**：experiment 阶段产出 `EXPERIMENT_OUTCOME`，success=False 时不进入 writing，避免「为写论文而写论文」
2. **dry_run 默认开启**：不调 API 用占位数据验证架构，`SRA_DRY_RUN=false` 启用真实调用
3. **原子概念分解（AI-Researcher）**：方法拆为最小可独立验证概念，建立「公式↔代码」双向映射，避免论文写一套代码做一套
4. **层级式论文生成（AI-Researcher）**：大纲→按章填充→审稿三阶段，避免一次性生成全文的结构松散
5. **人工节点回调**：CLI 用 `input()`，自动化迭代用 `auto_human_callback`，Web 可换 WebSocket
6. **溯源链硬校验**：writing 前强制校验 Claim 已 VERIFIED + Experiment 已 COMPLETED，未通过一律拒绝

### 文件结构（核心）
```
core/
  config.py                    # 全局配置 + dry_run 开关
  llm/
    registry.py                # LLM 统一入口 + structured_output 重试
    providers/openai_provider.py  # MiniMax/OpenAI 兼容 provider
  orchestration/
    graph.py                   # GraphRunner（含 resume_after_human 修复）
    context.py                 # ExecutionContext（快照排除 system/checkpoint）
    node.py                    # AgentNode/HumanNode/ToolNode/CheckpointNode
  knowledge/                   # SQLite 知识库（Paper/Claim/Experiment/Artifact）
  artifacts/version.py         # Artifact 版本管理
  tools/
    arxiv_search.py            # arxiv API 检索
    semantic_scholar.py        # S2 API 检索（限流优雅降级）
    code_runner.py             # 沙盒代码运行
    text_split.py              # 文本切分
stages/
  research/    agents.py graph.py io_schema.py
  ideation/    agents.py graph.py io_schema.py
  design/      agents.py graph.py io_schema.py
  experiment/  agents.py graph.py io_schema.py
  writing/     agents.py graph.py io_schema.py
runtime/
  pipeline.py                  # 端到端编排
  cli.py                       # CLI 入口
config/tasks.yaml              # 任务路由（全 minimax MiniMax-M3）
```

---

## 2026-08-05 第六轮：Sciverse 升级为检索主源 + 证据链审计轨迹（Task 1）

### 目标
按「文献查找优化最优解」（Sciverse 主源 + 证据链优先）落地：把 Sciverse 从第三数据源升级为主源，调用记录持久化为可审计证据链（赛题手册明确要求文献调研可溯源）。

### 改动清单（commit `3a7037e`）

#### 1. `core/tools/sciverse_search.py` — 修复解析器（关键 Bug）
- **根因**：真实 API 顶层返回 `hits` 键，旧代码只找 `results`/`data` → Sciverse 一直静默返回空（从未真正生效）
- **修复**：支持 `hits`/`results`/`data` 三种键；`author` 为 `["a|b|c"]` 分隔串需拆分；补充真实字段映射（`abstract` 独立于 `chunk`、`citation_count`、`publication_venue_name_unified`、`publication_published_year`、`page_no`、`primary_topic`）
- `SciverseEvidence` 新增 `abstract`/`citation_count`/`page_no`/`primary_topic` 字段；`to_meta_dict` 摘要优先用 abstract

#### 2. `stages/research/agents.py` — PaperFetchAgent 主源策略 + 证据链
- 数据源策略：Sciverse 主源（每子问题 5 条证据）+ arxiv（3）+ S2（2）补充
- `_real_fetch` 返回 `(paper_metas, evidence_chain)`：每次检索命中同步写入证据链（subquery/source/title/external_id/offset/evidence_score/snippet/paper_id）
- `PaperIngestAgent`：paper.metadata 持久化 `source`/`doc_id`/`offset`/`evidence_score`；证据链条目按 doc_id→arxiv_id→title 关联 paper_id 落库；未入库命中（被筛选/去重剔除）也保留，构成完整审计轨迹
- 空结果失败提示改为 Sciverse 优先表述

#### 3. `core/knowledge/store.py` — evidence_log 表
- 新表：`evidence_log(log_id, subquery, source, paper_id, title, external_id, offset, evidence_score, snippet, created_at)`
- 方法：`log_evidence` / `list_evidence`（按 paper_id 过滤）/ `evidence_stats`（total + by_source + linked）

#### 4. `stages/common.py` + `io_schema.py`
- 新增 `RESEARCH_EVIDENCE_CHAIN` ContextKey；`PaperFetchOutput` 增加 `evidence_chain` 字段

#### 5. `web/api.py` — 证据链对外暴露
- `/papers` 返回 `source`/`source_subquery`/`doc_id`/`offset`/`evidence_score`/`relevance_score`
- 新增 `/api/projects/{pid}/evidence`：entries + stats
- `/status` counts 增加 `evidence`

#### 6. `web/static/` — 证据链前端展示
- 论文浏览页新增「检索证据链 · 审计轨迹」卡片：按子问题分组、来源徽章（sciverse/arxiv/s2 三色）、证据分、doc_id/偏移、已入库标记
- 论文卡片头部加来源徽章（SC 证据分 / arxiv / s2），详情页展示 doc_id、偏移、证据分、相关度

### 验证结果
- 逻辑单测 14 项全过（落库往返 / doc_id-offset-score 持久化 / 旧库 schema 迁移 / 关联逻辑 / 未入库轨迹保留 / metadata 证据字段）
- Sciverse 探针：修复前 `agentic_search returned: 0`；修复后返回 10 条真实证据（含 doc_id/score）；`read_content` 原文回读 OK
- 真实模式（卤化物钙钛矿缺陷钝化主题）：
  - 修复前：证据链 30 条全 arxiv，Sciverse 零命中
  - 修复后：**证据链 130 条（Sciverse 100 + arxiv 30），74 条关联论文；入库 67 篇中 57 篇来自 Sciverse（85%）**，带 doc_id/证据分 0.8-0.9/真实引用数
- 交叉验证阶段遇到 MiniMax M3 长 JSON 结构化输出解析失败（Extra data）——已知稳定性问题，非本次改动引入

### 已知事项
- S2 无 key 限流 429 常见（优雅降级为空，不影响主链路）
- 交叉验证长 JSON 解析失败会让 cross_validate 节点将该子问题记入 gaps——后续可考虑 complete 输出 + 容错解析（DEVLOG 已有 TODO）
- 8001 服务曾因后台任务管理被终止，已用托管 venv 重启

---

## 2026-08-06 第七轮：Task 2 材料知识抽取节点（材料-性能-合成三元组）

### 目标
按赛题「知识抽取」要求落地：research 阶段新增 material_extraction 节点，从入库论文摘要中抽取结构化材料知识（材料成分/晶体结构/性能指标/合成条件），落库为可溯源三元组，Web 新增「材料知识」页展示。

### 改动清单

#### 1. `core/knowledge/schema.py` — 材料知识三实体 + 关系
- `EntityType` 新增 `MATERIAL_EXTRACTED_FROM_PAPER`；`RelationType` 新增 `MATERIAL_HAS_PROPERTY` / `MATERIAL_HAS_SYNTHESIS`
- 新增 `Material`（name/formula/crystal_structure/space_group/lattice_parameters/symmetry/composition/norm_name/confidence/source_snippet）
- 新增 `MaterialProperty`（property_name/name_cn/value/value_num/unit/condition）
- 新增 `MaterialSynthesis`（method/precursors/temperature/pressure/atmosphere/duration/steps）

#### 2. `core/knowledge/store.py` — 材料三表 + CRUD
- `_SCHEMA_SQL` 追加 `materials` / `material_properties` / `material_synthesis` 三表（`CREATE TABLE IF NOT EXISTS`，旧库自动补表）
- `save_material`：按 `norm_name` 查重，同名材料跨文献合并（新 paper_id 并入 `metadata.source_paper_ids`）→ 满足赛题「跨文献实体链接」
- `save_material_property` / `save_material_synthesis` / `list_*` / `material_stats`（含 complete_triples 统计）

#### 3. `stages/research/agents.py` — MaterialKnowledgeExtractionAgent
- `node_type=research_material_extraction`，`task_type=material_knowledge_extract`
- 逐篇论文 LLM 结构化抽取（`MaterialExtractSchema`：每篇独立 prompt 避免跨论文信息混淆）→ 落库 → 写 context（`RESEARCH_MATERIAL_KNOWLEDGE`）
- dry_run / LLM 失败时占位兜底，不阻塞流程
- **关键 Bug 修复**（全量验证发现）：
  - `output_keys` 值误写为字符串 → `ctx.set` 抛 `AttributeError: 'str' object has no attribute 'name'`（节点必崩）。改为 `{}`，输出由 `_execute` 显式写入 context
  - LLM 返回的 `precursors` 可能是字符串 → `list(str)` 逐字符拆分。统一转 list
  - `steps` 可能是 list → join 为字符串；`value_num` 可能是空串/不可解析 → 容错转 float/None；`property_name` 可能为 None → 兜底 ""

#### 4. `config/tasks.yaml` — 注册 `material_knowledge_extract`
- provider minimax / MiniMax-M3 / temperature 0.0（此前缺失导致节点运行时 `TaskNotFoundError` 全部抽取降级为空）

#### 5. `stages/research/graph.py` — 拓扑
- `paper_ingest → material_extraction → cross_validate`

#### 6. `web/api.py` — `/materials` 端点 + 启动恢复机制
- 新增 `GET /api/projects/{pid}/materials`：按材料聚合性能/合成，含来源论文与证据片段
- `get_status` counts 增加 materials/properties/synthesis
- **新增 `_scan_existing_projects()`**：服务启动时扫描 projects/ 目录恢复内存项目状态（ProjectState 是进程单例，重启即失；恢复后旧项目数据可继续通过 Web 查看）。topic 不持久化，恢复项目 topic 为空 → run 接口加保护：topic 为空返回 400 提示

#### 7. `web/static/` — 材料知识页
- 导航新增「材料知识」项（badge-materials）；`renderMaterials` 渲染统计卡片 + 材料卡片（结构/组成、性能、合成、来源论文、跨文献徽章）

### 验证结果
- 空抽取结果路径（LLM 全失败）不再崩溃：修复前 `AttributeError: 'str' object has no attribute 'name'` → 修复后 SUCCESS（0 材料不报错）
- 10 篇小规模：24 种材料抽取、28 条性能、13 条合成、零落库失败
- **全量 100 篇真实 LLM 抽取（15.5 分钟）**：
  - **153 种材料抽取 → 141 种入库（同名合并去重，跨文献实体链接生效）**
  - **324 条性能指标、103 条合成方法、71 条完整三元组**
  - 3 篇抽取失败（MiniMax M3 非标准 JSON/非材料论文），per-paper 容错降级，节点整体 SUCCESS

### 已知事项
- MiniMax M3 结构化输出偶发非合法 JSON（属性名无引号、直接写分析文本）→ 该篇材料为空，不阻塞流程；后续可加 JSON 修复（如属性名补引号）提升召回
- verify_material_extract.py 为验证脚本（未纳入 git 或待归档）

---

## 2026-08-06 第八轮：进度页实时预告 + 材料知识整理美化 + 搜索跳转（需求 1-3）

### 目标
1. 需求 1：长任务运行时告知用户「当前在做什么 + 下一步做什么」，避免干等
2. 需求 2：材料知识页太乱 → 依据材料科学标准整理数据、美化展示（性能/合成方法统一）
3. 需求 3：材料知识页加搜索框，用户搜相关词语可实时过滤并直接跳转定位

### 改动清单

#### 1. 进度页「正在执行 / 下一步预告」（core/orchestration/graph.py + runtime/pipeline.py + web/api.py + app.js）
- GraphRunner 新增构造参数 on_node_started（每节点执行前回调，携带该节点 ID + graph.successors 下一步候选列表）；_step() 中 _notify_node_started
- run_pipeline / run_discovery / run_topic_discovery → run_stage 全链路传播 on_node_started
- ProjectState 新增 current_node（node_id + next_nodes + started_at）与 next_nodes；get_status 返回
- 前端新增 NODE_LABELS（约 35 节点 → 中文描述）+ nodeLabel()；renderCurrentNode(data) 渲染「正在执行：XXX」卡片 + 下一跳 chips（pending_human 显示「等待人工确认」）；时间线节点改用中文名
- 验证：verify_node_started.py dry_run 全 28 节点触发 started 事件且每事件带 next 候选

#### 2. 材料数据标准化（新建 core/knowledge/normalize.py + web/api.py /materials 改造）
- 性能指标归一化：PROPERTY_CANON 精确映射（约 31 个标准性能：ZT/功率因子/热导率/Seebeck/载流子/带隙/有效质量/态密度/晶格常数/格林艾森参数…）+ 中文名映射（property_name 与 property_name_cn 任一命中）+ 正则子串兜底 → 标准 key/cn/symbol/unit/category（电输运/热输运/热电优值/载流子/能带结构/稳定性/器件性能）
- 合成方法分类：10 类工艺正则（计算模拟/熔融法/固相法/球磨法/烧结法/电化学法/溶液法/薄膜法/纳米合成/未指定）
- 材料体系分类：19 类体系正则（顺序敏感；Half-Heusler 必须在方钴矿前，TiCoSb 含 CoSb 子串）；补 PbS/PbSe/Sb2Te3/化学式 (Se|S|Te)\d → 硫族化物，Fe2O4/SOFC → 氧化物，batter → 电池材料（兼容 batteries 复数）
- /materials 接口：每性能带 norm_key/norm_cn/symbol/unit/category，每合成带 method_category/method_label，每材料带 category；新增 aggregation（三类别计数）
- 修复 Half-Heusler 误判：正则顺序调整 + 补 ZrNiSn/HfNiSn 公式

#### 3. 材料知识页美化 + 搜索（web/static/app.js + style.css）
- 页面结构：搜索条 → 统计卡片（材料/性能/合成/三元组）→ 知识结构总览（材料体系 chips 可点击筛选，性能/工艺分布统计）→ 材料卡片列表
- 材料卡片：头部编号 + 名称 + 化学式徽章 + 体系彩色徽章（哈希 8 色）+ 置信度 + 跨文献徽章 + 性能/合成计数；结构/组成一行；性能按类别分组（标准符号 + 标准中文名 + 原文 + 值/单位/条件 + 来源论文）；合成按工艺类别分组（工艺 chip + 原文 + 温度/压力/气氛/时长/前驱体）
- 搜索框：实时过滤（匹配材料名/化学式/体系/性能/合成方法/条件）；回车或「定位」按钮滚动到首个命中卡片并高亮闪烁（mat-flash）；「重置」清空；聚合 chips 点击按体系筛选

### 验证结果
- _verify_normalize2.py：54 项全部通过（性能归一化含中文名 6 项、方法分类 17 项、材料分类含 Half-Heusler 修复 20+ 项）
- 服务重启后 /materials 复查（141 材料 / 324 性能 / 103 合成 / 71 三元组）：
  - 材料体系未识别 60 → 35（硫族化物 13→33，氧化物 6→10；Half-Heusler 正确识别 2 种，方钴矿 1 种）
  - 未归一化性能 59 → 40（有效质量/态密度/晶格常数/格林艾森参数等已并入标准集，其余为 AUC 等真特殊指标）
  - 聚合统计：性能类别 8 类、合成工艺 11 类、材料体系 20 类
- 前端静态资源已确认加载新代码（app.js/style.css 均含搜索与分组类）

### 已知事项
- 材料体系仍有 35 种归「其他」（Thermoelectric nanocomposite / MXene / Spiro-OMeTAD 等通用或器件名，正则无法可靠归类）；性能「其他」40 种多为 ML 指标/描述性指标
- 临时验证脚本（_verify_normalize2.py 等）因系统删除钩子路径解析 bug 无法删除，保持不纳入 git


---

## 2026-08-06 第八轮修订：材料知识页问题修复（用户反馈后）

### 背景
用户反馈材料页「内容不全、美化没做到、定位/重置挡住文字且无效、搜索框不齐全」。
排查后确认根因：**浏览器缓存旧版 app.js/style.css**（静态文件无缓存控制头，
新 HTML 结构套旧样式导致布局错乱）；另发现窄屏真实布局 bug 与交互细节缺陷。

### 修复清单
1. **静态资源 no-cache**（web/api.py）：移除 StaticFiles 挂载，改自定义路由加
   Cache-Control: no-store + Pragma: no-cache + Expires: 0，根除缓存错乱。
2. **URL 深链**：?project=<id>&page=materials 直达材料页（init() 解析 URL 参数），
   便于分享/调试/无头浏览器验证。
3. **搜索条重构**：放大镜图标 + 输入框 + 实时命中计数徽章（命中 X / 总数）+ 定位/重置
   按钮（flex-shrink:0 不被压缩）+ Esc 清空 + 空关键词定位 toast 提示。
4. **搜索高亮**：材料名/性能/合成方法/条件中的关键词 <mark class=mat-hl> 高亮。
5. **材料卡片**：性能按类别分组（类别色点 + 浅色标题条，--cat-color 变量）、合成按工艺
   分组；无性能/无合成显示「暂无记录」占位（99 处与数据缺口精确对应：36 无性能 +
   63 无合成）；聚合类别超 10 类折叠「等 N 类」。
6. **窄屏(<720px)修复**：卡片头计数徽章 flex-basis:100% 换行右对齐（原 margin-left:auto
   与 flex-shrink 导致与徽章重叠 85px）；搜索条元素换行（input order:-1 占首行）。
7. **体系 chip active 同步**：点击筛选后遍历更新所有可点击 chip 的 active class。

### 验证（CDP 驱动真实 Edge 无头浏览器）
- 交互：初始 141 卡片 → 搜索「球磨」命中 5/141 + 6 个 mark 高亮 → 定位 flash+toast
  → 重置恢复 141 → 体系 chip 筛选 35 卡片 + active 正确 → 空词定位 toast 提示
- 布局（1280px）：搜索条 5 元素、卡片头 6 元素两两无重叠
- 布局（676px）：修复后卡片头计数换行第二行，全页无重叠
- DOM 结构：mat-empty 99 处、mat-cat-dot 285 处、聚合折叠 2 处均正确

### 工具沉淀
- **CDP 驱动无头 Edge 验证方案**：msedge --headless --remote-debugging-port=<p>
  --remote-allow-origins=* + websocket-client 调 Runtime.evaluate 执行真实交互断言。
  注意：Windows 上 Edge 单实例复用会导致调试端口被旧实例占用，需独立
  --user-data-dir 且先清理残留进程；websocket 需 --remote-allow-origins=* 否则 403。
