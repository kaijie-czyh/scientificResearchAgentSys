# 科研论文 Agent 系统（Scientific Research Agent System）

一个面向科研全生命周期的多 Agent 系统，覆盖 **研究主题挖掘 → 文献查找 → 方法设计 → 实验设计 → 论文写作** 五阶段，配以版本管理、溯源校验、方法↔代码对齐可视化，让科研人员在人机交互中倍感赋能。

## 核心设计理念

针对当前 AI 辅助 research 的典型痛点：

| 痛点 | 本系统应对 |
|------|-----------|
| 结果不可验证 | 溯源链硬校验 + Claim 证据关联 + inspect 工具透明化全部产出 |
| 工作不够创新 | 原子概念分解 + 公式↔代码双向映射，避免论文写一套代码做一套 |
| 写作不可参考 | 层级式论文生成（大纲→按章填充→审稿），每章显式关联 Claim |
| 实验失败强行写论文 | experiment_failed 自然停止，诚实评估「实验是否验证核心 Claim」 |

## 快速开始

### 1. 环境准备

```bash
pip install -r requirements.txt
```

### 2. 配置 MiniMax API

复制 `.env.example` 为 `.env`，填入你的 MiniMax Token Plan key：

```bash
cp .env.example .env
# 编辑 .env，填入：
# MINIMAX_API_KEY=sk-cp-xxxxxxxxxxxx
# MINIMAX_BASE_URL=https://api.minimaxi.com/v1
```

> MiniMax Plus ¥49/月套餐即可（6亿+ token/月，1M 上下文）。key 与 endpoint 必须匹配（国内版用 `api.minimaxi.com`）。

### 3. dry_run 验证架构（默认，不调 API）

```bash
python -m runtime.cli run --topic "联邦学习中的公平激励机制设计" --force-writing --verbose
```

dry_run 模式用占位数据跑通全 5 阶段 24 节点，验证架构完整性，不消耗 token。

### 4. 真实运行

```bash
# 全流程（research→writing）
python -m runtime.cli run --topic "联邦学习中的公平激励机制设计" --no-dry-run

# 只跑到实验阶段（跳过写作，验证研究链路）
python -m runtime.cli run --topic "..." --no-dry-run --stop-before writing

# 恢复中断的项目
python -m runtime.cli resume --project-id proj_xxx

# 查看项目状态
python -m runtime.cli status --project-id proj_xxx
```

真实模式下，遇到人工节点会交互式询问（`input()`）。自动化迭代可用 `smoke_test.py`：

```bash
# 自动确认人工节点，跑 research→experiment
$env:SRA_SMOKE_REAL='1'; python smoke_test.py

# 自动确认人工节点，跑含写作的完整流程
$env:SRA_SMOKE_REAL='1'; $env:SRA_SMOKE_INCLUDE_WRITING='1'; python smoke_test.py
```

## 项目检查与可视化

```bash
# 项目概览（论文/思路/Claim/实验计数）
python -m tools.inspect_project --project-id proj_xxx --section overview

# 论文清单
python -m tools.inspect_project --project-id proj_xxx --section papers

# Claim 与证据链
python -m tools.inspect_project --project-id proj_xxx --section claims

# 实验结果
python -m tools.inspect_project --project-id proj_xxx --section experiments

# 方法文档
python -m tools.inspect_project --project-id proj_xxx --section method

# 方法↔代码对齐（公式是否在代码中落地）
python -m tools.inspect_project --project-id proj_xxx --section alignment

# 全部信息
python -m tools.inspect_project --project-id proj_xxx
```

## 架构概览

```
research   → ideation  → design    → experiment → writing(可选)
6 节点        5 节点       6 节点      8 节点       6 节点
```

### 关键设计决策

1. **实验失败是科研常态**：experiment 阶段产出 `EXPERIMENT_OUTCOME`，success=False 时不进入 writing，避免「为写论文而写论文」
2. **dry_run 默认开启**：不调 API 用占位数据验证架构，`SRA_DRY_RUN=false` 启用真实调用
3. **原子概念分解（AI-Researcher）**：方法拆为最小可独立验证概念，建立「公式↔代码」双向映射
4. **层级式论文生成（AI-Researcher）**：大纲→按章填充→审稿三阶段，避免一次性生成全文的结构松散
5. **人工节点回调**：CLI 用 `input()`，自动化迭代用 `auto_human_callback`
6. **溯源链硬校验**：writing 前强制校验 Claim 已 VERIFIED + Experiment 已 COMPLETED

### 文件结构

```
core/
  config.py                    # 全局配置 + dry_run 开关
  llm/                         # LLM 适配层（MiniMax/OpenAI 兼容）
  orchestration/               # 图编排引擎（Graph/Runner/Node）
  knowledge/                   # SQLite 知识库（Paper/Claim/Experiment/Artifact）
  artifacts/                   # Artifact 版本管理 + 溯源校验
  tools/                       # arxiv/S2 检索 + 代码沙盒运行
stages/
  research/    # 主题精炼→子问题分解→论文抓取→相关性筛选→入库→交叉验证
  ideation/    # 思路生成→人工探讨→三维度验证→Claim 草拟
  design/      # 原子概念分解→方法形式化→Claim 证据关联→方法 Artifact
  experiment/  # 实验配置→代码生成→导师审查→运行→异常检测→Claim 验证→成败评估
  writing/     # 溯源校验→风格学习→大纲→按章撰写→审稿→终稿确认
runtime/
  pipeline.py                  # 端到端编排
  cli.py                       # CLI 入口
tools/
  inspect_project.py           # 项目检查与可视化
config/tasks.yaml              # 任务路由（全 minimax MiniMax-M3）
```

## 配置说明

### 任务路由（config/tasks.yaml）

所有 task 路由到 `minimax` provider，model 统一 `MiniMax-M3`。切换 provider 只需改 yaml：

```yaml
tasks:
  writing_section_draft:
    provider: mimo           # 切换到 MiMo
    model: MiMo-V2-Pro
    temperature: 0.4
```

### dry_run 开关

- `.env` 中 `SRA_DRY_RUN=true`（默认）：占位数据，不调 API
- `.env` 中 `SRA_DRY_RUN=false`：真实调用 MiniMax API
- CLI `--no-dry-run` 覆盖环境变量

## 开发溯源

核心推进记录在 [DEVLOG.md](DEVLOG.md)，每轮含「目标 / 改动 / 验证 / 问题与修复 / 下一步」。

## 已知限制

1. **Semantic Scholar 限流**：免费 API 429 限流严重，arxiv 已足够；S2 失败时优雅降级
2. **嵌入模型缺失**：MiniMax 无嵌入 API，未配 `OPENAI_API_KEY` 时向量检索降级为关键词检索
3. **长中文 JSON 脆弱**：MiniMax M3 生成 1000+ 字中文 reason 时 JSON 转义偶发失败，各 agent try/except 回退占位
4. **实验代码质量**：LLM 生成的实验代码可能跑不通，由 CodeReview 多轮迭代 + experiment_failed 自然停止应对

## 测试

```bash
pytest tests/ -v
```
