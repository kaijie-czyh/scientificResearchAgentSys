"""科研论文 Agent 系统 — 核心横切基础层。

模块组织：
- state: 生命周期状态机 + 快照 + 回滚
- knowledge: 统一知识库（5种实体 + 关系 + 检索）
- orchestration: Agent 编排（DAG + 人工节点 + 上下文）
- llm: LLM 适配（任务路由 + 可配置 provider）
- artifacts: 产出物版本管理 + 溯源链
"""

__version__ = "0.1.0"
