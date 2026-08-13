---
title: 科研论文 Agent 系统（GOAI 方向三）
emoji: 🧪
colorFrom: indigo
colorTo: blue
sdk: static
app_file: index.html
pinned: false
fullWidth: true
---

# 科研论文 Agent 系统（GOAI 方向三 · Web Demo）

材料科学文献驱动的科学发现智能体：**选题 → 文献检索 → 摘要 → 冲突裁决 → 构效关系发现 → 假设生成** 全流程。

## 使用说明

1. 点击右上角 **🖥️ Open in a new tab** 进入全屏模式（推荐）
2. 输入材料研究方向（如 *"half-Heusler 热电材料"* / *"钙钛矿太阳能电池"*）
3. 点击 **开始研究**，系统自动执行完整科研流水线

## 功能亮点

- 🔍 多渠道文献检索（Sciverse / arXiv / Semantic Scholar）
- ⚖️ 文献冲突自动裁决（支持/反对/存疑 + 量化依据）
- 🔗 材料间关联关系（同体系→同性质→同方法→同论文）
- 🧬 结构化构效关系发现（5 要素物理机制）
- 🎯 假设可验证性评分
- 🔐 全链路证据审计

## 架构说明

- 本 Space 托管**静态前端**；后端 FastAPI 部署于独立托管服务（如 Render）
- 前端通过 `index.html` 中的 `window.SRA_API_BASE` 指向后端地址

## 技术栈

Python 3.10 · FastAPI · SQLite · 原生 HTML/CSS/JS · MiniMax M3 · Sciverse/arXiv/S2