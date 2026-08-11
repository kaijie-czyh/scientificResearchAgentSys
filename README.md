---
title: 科研论文 Agent 系统
emoji: 🧪
colorFrom: indigo
colorTo: cyan
sdk: docker
app_port: 7860
pinned: false
---

# 科研论文 Agent 系统（GOAI 方向三 · Web Demo）

材料科学文献驱动的科学发现智能体，一键完成 **选题 → 检索 → 摘要 → 冲突裁决 → 构效关系发现 → 假设生成** 的完整科研流程。

## 快速开始

1. 点击右上角 **🖥️ Open in a new tab** 进入全屏模式（推荐）
2. 输入一个感兴趣的材料方向，例如：*"half-Heusler 热电材料"*、*"钙钛矿太阳能电池"*、*"NMC 正极材料"*
3. 点击 **开始研究**，系统将自动执行完整科研流水线

## 功能亮点

- 🔍 **多渠道文献检索**：Sciverse（证据片段级）/ arXiv / Semantic Scholar
- ⚖️ **文献冲突裁决**：自动生成支持/反对/存疑判定与量化依据
- 🔗 **材料间关联关系**：同体系 → 同性质 → 同方法 → 同论文的关联链
- 🧬 **构效关系发现**：结构化 5 要素物理机制（结构-性质-工艺-成分-环境）
- 🎯 **假设可验证性评分**：可复现性 / 可证伪性 / 新意 / 实践可行 / 数据可得
- 🔐 **全链路证据审计**：检索/入库/未入库/手动补录统计全程可追溯

## 技术栈

- **后端**：Python 3.10 + FastAPI + SQLAlchemy + SQLite
- **前端**：原生 HTML/CSS/JS 单页应用（无框架、零构建）
- **LLM**：MiniMax M3（1M 上下文，可替换为任意 OpenAI 兼容接口）
- **检索**：Sciverse / arXiv / Semantic Scholar

## 公开数据源合规声明

所有外部数据来源均集中登记于 `core/tools/data_provenance.py`，包含来源名称、URL、数据类型、获取方式、许可证与最后访问时间，支撑赛题 §5.3「外部数据源合规」要求。

## 部署自定义

- 本 Space 使用 **Docker SDK**，入口 `python -m web.api`，端口 `7860`
- LLM / 检索 API Key 通过 **Settings → Variables and secrets** 配置：
  - `MINIMAX_API_KEY`（必填，国内版 key 须配合 `MINIMAX_BASE_URL=https://api.minimaxi.com/v1`）
  - `SCIVERSE_API_TOKEN`（可选）
  - `MATERIALS_PROJECT_API_KEY`（可选）
- 未配置 API Key 时系统以 `SRA_DRY_RUN=true` 占位数据运行，完整流程仍可走通

> ⚠️ 演示环境状态为内存驻留：Space 实例休眠或重启后需重新创建项目。