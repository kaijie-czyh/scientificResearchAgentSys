---
title: SciFinder-Agent（材构发现智能体）
emoji: 🧪
colorFrom: indigo
colorTo: cyan
sdk: docker
app_port: 7860
pinned: false
---

# 材构发现智能体 · SciFinder-Agent

> **基于物理硬筛与证据链的多源构效关系自动化发现平台**
> 2026 GOAI 世界人工智能开源大赛 · 赛道三「前沿探索（AI for Research）」参赛作品
> 项目方向：**材料科学文献驱动的科学发现智能体**

SciFinder-Agent 是一个面向**小数据-强物理约束体系**（热电材料 / 催化剂 / 高熵合金）的多 Agent 科研自动化系统。它以 **5 阶段流水线 + 1 个独立构效关系发现模块** 覆盖科研全生命周期：研究主题挖掘 → 文献检索 → 思路生成 → 方案设计 → 实验执行 → 论文撰写。系统的核心设计理念是 **LLM 发散生成 + MCTS 约束收敛** —— 大模型负责语义判断与创新性发散，规则引擎提供物理常识兜底（甚至有一票否决权），从源头抑制"Novec-7100 流体被当热电材料"这类物理幻觉。

## 📋 项目状态

| 维度 | 状态 |
|------|------|
| 节点数 | **43 个**（37 个流水线节点 + 6 个 Discovery 子模块节点） |
| 测试 | **326 passed**（pytest 单元 + 集成 + 端到端） |
| 部署 | **三层**：GitHub（源码） / Replit（后端） / HuggingFace Space（前端镜像） |
| LLM Provider | 默认 **MiniMax-M3**（兼容 OpenAI API，可切换 Anthropic / vLLM / 自托管） |
| 数据源 | **8 个外部源**：arXiv / Semantic Scholar / Sciverse / Materials Project / OQMD / NOMAD / MinerU / LLM Provider |
| 许可证 | 暂未开源（赛后考虑 Apache 2.0 / MIT）|

## 🏗️ 架构一览

系统分为 **6 层**（详见 [`docs/architecture.svg`](docs/architecture.svg)）：

```
① 应用层        Web Dashboard / CLI / HuggingFace Space / Replit 后端
② 编排层        5 阶段流水线（Research / Ideation / Design / Experiment / Writing）+ Discovery 子模块
③ 工具与 LLM    任务路由 / 结构化输出 / 向量检索 / 物理一致性 / 5 维评分 / 材料 CV / 冲突裁决 / 指标聚合
④ 数据与知识    KV 表（31 关键字段）+ Paper / Material+Property / Claim+Idea+Gap / Conflict / Evidence Log
⑤ 外部数据源    arXiv / S2 / Sciverse / MP / OQMD / MinerU / NOMAD / LLM (6 Provider)
⑥ 系统级指标    9 类指标自动聚合（节点完成率 / KV 覆盖率 / 文献抓取率 / 5 维评分 / Gap / CV / 证据链 / 降级 / 效率）
```

完整节点拓扑（43 节点）见 [`节点必要性与通信设计.md`](节点必要性与通信设计.md)。

## 🚀 快速开始

### 方式一：HuggingFace Space 在线体验（推荐）

打开 [https://huggingface.co/spaces/xindong09280929/scientific-research-agent-demo](https://huggingface.co/spaces/xindong09280929/scientific-research-agent-demo)，输入研究主题（如"half-Heusler 热电材料"），一键启动流水线，全程可视化。

> ⚠️ 演示环境为**内存驻留**：Space 实例休眠或重启后需重新创建项目。

### 方式二：本地完整复现

```bash
# 1. 克隆仓库
git clone https://github.com/kaijie-czyh/scientificResearchAgentSys.git
cd scientificResearchAgentSys

# 2. 安装依赖（推荐 Python 3.10+）
pip install -r requirements.txt

# 3. 配置 LLM Provider（编辑 .env，填入 MINIMAX_API_KEY 或自托管 vLLM 地址）
cp .env.example .env
# 编辑 .env，至少配置 MINIMAX_API_KEY

# 4. 启动 Web 服务
python -m web.api
# 浏览器访问 http://localhost:8000

# 5.（可选）启动无界面 CLI 模式
python -m runtime.cli run --topic "GeTe 热电材料" --no-dry-run
```

### 方式三：Replit 一键部署

仓库根目录包含 `.replit` 配置，登录 [Replit](https://replit.com) → Import from GitHub → 选本仓库即可。

## 🧪 跑测试

```bash
# 全量测试（~50 秒，326 项）
SRA_WEB_SKIP_GUARD=1 python -m pytest tests/ --no-header

# 仅跑 Golden Set（8 个固定查询的回归测试）
SRA_WEB_SKIP_GUARD=1 python -m pytest tests/test_golden_set.py -v

# 仅跑架构图 / 节点图测试
SRA_WEB_SKIP_GUARD=1 python -m pytest tests/test_stages_graphs.py tests/test_node_architecture.py -v
```

## 🔬 核心创新

### 1. 🛡 物理一致性硬筛（`core/physics_consistency/`）

杜绝 LLM 物理幻觉——在 Discovery 子模块的 `LLMGuidedSearchAgent` 与 `DiscoveryValidateAgent` **双层嵌入**硬筛：

| 检查维度 | 实现 |
|----------|------|
| 化学式合法性 | 118 元素周期表 + 解析器（含 MA / FA / PEA 等有机阳离子识别）|
| 电中性 | DFS 回溯价态组合 + 金属间化合物判定 + 18 电子规则（half-Heusler 等） |
| Goldschmidt 容忍因子 | 钙钛矿型 ABO3 计算 t，判定稳定区间 0.825~1.059 |
| 物理量范围 | 14 项性能指标（ZT / Seebeck / 热导 / 电导 / 带隙 / 形成能 ...）的物理可达范围 |
| 工艺范围 | 合成温度（0~4000K）、压力（0~200 GPa）的物理可达范围 |

**实测**：Bi₂Te₃ / SnSe / ZrNiSn / Cs₀.₀₅FA₀.₉₅PbI₃ 全部通过；Novec-7100（未知元素 No）、Bi₂Te₃ ZT=50（超范围）、合成温度 < 0K（违反热力学）一律拒绝。

### 2. 📊 5 维度可信度评分（`core/tools/discovery_metrics.py`）

对每条构效关系候选独立打 5 维分：

1. **外推安全**（0.20 权重）：候选与数据库已知样本的空间距离
2. **文献密度**（0.25 权重）：支撑该构效关系的文献数量 / 期刊等级 / 新旧加权
3. **机制完整**（0.20 权重）：物理原理 / 因果链等论证要素完备程度
4. **CV 一致性**（0.20 权重）：Materials Project 查询 vs内置规则校验的匹配度
5. **预测区间合理性**（0.15 权重）：性能预测值是否落在已知材料的物理边界内

### 3. ⚖️ 文献冲突自动裁决

按 **期刊影响因子 60% + 分区 20% + 发表时间 20%** 加权打分，给出支持/反对/存疑的判定与量化依据，支持人工复核修正。

### 4. 🔍 性质数据"证据等级 A-E"

| 等级 | 含义 |
|------|------|
| A | 多篇实验文献交叉验证 |
| B | 单篇高质量实验 |
| C | 多篇计算 / 综述交叉 |
| D | 单篇计算 |
| E | LLM 推断（标"暂无可靠文献数据"） |

## 📊 系统级指标面板（赛题 §4.2 强支撑）

通过 `GET /api/metrics/system` 自动聚合 9 类指标：

| 指标 | 字段 |
|------|------|
| 节点完成率 | success / total 节点数 |
| KV 覆盖率 | 31 关键字段的实际填充率 |
| 文献抓取率 | arXiv / S2 / Sciverse 三源命中率 |
| 5 维评分分布 | P25 / 中位数 / P75 |
| Gap 质量分布 | 4 维度 |
| CV 一致性 | Materials Project + 规则两路一致率 |
| 证据链 | 按阶段 manual / retrieved 占比 |
| 降级触发率 | 三路（MP / Sciverse / LLM）降级触发比例 |
| 流水线效率 | 平均耗时 / LLM 调用次数 |

**当前真实数据**（基于 202 个项目 / 289 papers）：569 个 claims / 41 个 gaps / 9 类指标均产出可计算数值。

导出一键：

```bash
python tools/export_metrics_report.py --prefix metrics_real
# 输出 metrics_real.json / .md / .csv / .html
```

## 🛠 技术栈

| 层 | 选型 | 理由 |
|----|------|------|
| 后端 | Python 3.10 + FastAPI + SQLAlchemy + SQLite | 异步友好、轻量部署、零外部数据库依赖 |
| 前端 | 原生 HTML/CSS/JS 单页应用 | 零构建、零框架依赖、文件可直接静态托管 |
| LLM | **MiniMax-M3** 默认（兼容 OpenAI API）| 国内可用 + 1M 上下文；可切换 Anthropic / vLLM / 自托管 |
| 向量检索 | ChromaDB 本地持久化 | 内置 SQLite 存储，免外部向量库 |
| 数值计算 | numpy + scipy | 物理规则计算的底层依赖 |
| 测试 | pytest | 326 项测试覆盖单元 + 集成 + 端到端 |

## 🔐 数据来源合规声明

所有外部数据来源均集中登记于 `core/tools/data_provenance.py`，包含来源名称、URL、数据类型、获取方式、许可证与最后访问时间。主办方发放的 **WAYB/WAYC 蛋白组学数据包**与本项目"材料科学文献驱动"方向不直接对接，故不纳入核心数据使用，仅在附录"未来工作：跨学科扩展候选"中列出。

| 来源 | 类型 | 许可证 |
|------|------|--------|
| arXiv | 学术预印本 | 公开 |
| Semantic Scholar | 学术检索 | 公开（可选 token）|
| Sciverse | 深度解析文献 | 学术 token |
| Materials Project | 材料结构与性能 | X-API-KEY |
| OQMD | 开放量子材料 | 公开 |
| NOMAD | 材料档案 | 公开 |
| MinerU | PDF 解析 | token |
| LLM Provider (6) | 文本生成 | OpenAI 兼容 |

## 🔧 部署自定义

- 本 Space 使用 **Docker SDK**，入口 `python -m web.api`，端口 `7860`
- LLM / 检索 API Key 通过 **Settings → Variables and secrets** 配置：
  - `MINIMAX_API_KEY`（必填，国内版须配 `MINIMAX_BASE_URL=https://api.minimaxi.com/v1`）
  - `SCIVERSE_API_TOKEN`（可选）
  - `MATERIALS_PROJECT_API_KEY`（可选）
- 未配置 API Key 时系统以 `SRA_DRY_RUN=true` 占位数据运行，完整流程仍可走通
- 三层降级路径：缺 MP → 走规则单路校验；缺 Sciverse → 退化为 arXiv+S2 双源；缺 LLM → 进入 `dry_run` 模式

## 📂 仓库目录

```
scientificResearchAgentSys/
├── README.md                  本文件（v2.2，含 43 节点 + 326 测试）
├── requirements.txt           Python 依赖
├── Dockerfile                 HF Space / Render 部署
├── .replit                    Replit 部署配置
├── render.yaml                Render.com 部署配置
├── runtime/cli.py             无界面批量执行
├── core/
│   ├── orchestration_graph.py 41 节点拓扑（含 5 阶段 + Discovery 子模块）
│   ├── physics_consistency/   物理硬筛（118 元素价态 + DFS + 18e + Goldschmidt）
│   ├── observability/         系统级指标聚合
│   ├── knowledge/             KV / Paper / Material / Claim / Conflict / Evidence
│   ├── llm/                   LLM Provider（OpenAI 兼容 + Anthropic + MiniMax）
│   └── tools/                 任务路由 / 向量检索 / 数据源合规
├── stages/
│   ├── research/agents.py     5 阶段流水线（research / ideation / design / experiment / writing）
│   └── discovery/agents.py    Discovery 子模块（含物理硬筛嵌入点）
├── web/
│   ├── api.py                 FastAPI 路由
│   └── static/                前端（原生 HTML/JS）
├── tests/                     326 项 pytest
├── tools/                     指标导出 / 架构图生成
├── docs/
│   ├── architecture.svg       6 层 + 41 节点架构图
│   └── node_architecture.md   节点拓扑文档
├── 节点必要性与通信设计.md     41 节点设计辩护
├── hf-space-deploy/           HF Space 静态镜像
└── 新推进材料存放暂时/         赛事提交包（不随仓库上传）
```

## 📜 开源计划

仓库与 HuggingFace Space 的源码、pytest 测试套件与介绍文档均公开访问。**暂未开放许可证**（赛后考虑 Apache 2.0 / MIT）。欢迎对本项目感兴趣的同伴通过 Issue / Discussion 进行开源社区友好交流，团队会及时维护与反馈。

## 📞 联系方式

- GitHub: [https://github.com/kaijie-czyh/scientificResearchAgentSys](https://github.com/kaijie-czyh/scientificResearchAgentSys)
- HuggingFace Space: [https://huggingface.co/spaces/xindong09280929/scientific-research-agent-demo](https://huggingface.co/spaces/xindong09280929/scientific-research-agent-demo)
- 赛事方向：2026 GOAI 世界人工智能开源大赛 · 赛道三「前沿探索（AI for Research）」

---

> 最后更新：2026-08-15（v2.2 · 合并 PR #4 feat/code-update-v2 后）
> 项目代号：`SciFinder-Agent`（英文品牌名）/ 材构发现智能体（中文品牌名）