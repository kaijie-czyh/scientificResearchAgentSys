"""全局配置与路径管理。

设计原则：
- 所有路径通过 ProjectPaths 统一管理，避免散落的硬编码路径
- 配置可被环境变量覆盖，便于不同机器部署
- LLM/向量库等外部依赖配置走 YAML，代码层不绑定具体实现
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# 项目根目录（core/config.py 的上两级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ProjectPaths:
    """项目内统一路径管理。所有路径均为绝对路径。"""

    root: Path
    core: Path
    stages: Path
    projects: Path  # 具体科研项目数据存放处（git忽略）
    tests: Path

    @staticmethod
    def from_root(root: Path | str) -> "ProjectPaths":
        root = Path(root).resolve()
        return ProjectPaths(
            root=root,
            core=root / "core",
            stages=root / "stages",
            projects=root / "projects",
            tests=root / "tests",
        )

    @staticmethod
    def default() -> "ProjectPaths":
        return ProjectPaths.from_root(PROJECT_ROOT)

    def project_dir(self, project_id: str) -> Path:
        """单个科研项目的数据目录。"""
        return self.projects / project_id

    def project_db(self, project_id: str) -> Path:
        """单个项目的 SQLite 知识库路径。"""
        return self.project_dir(project_id) / "knowledge.db"

    def project_vector_store(self, project_id: str) -> Path:
        """单个项目的向量库目录。"""
        return self.project_dir(project_id) / "vectors"

    def project_snapshots(self, project_id: str) -> Path:
        """单个项目的快照目录。"""
        return self.project_dir(project_id) / "snapshots"

    def project_artifacts(self, project_id: str) -> Path:
        """单个项目的产出物目录。"""
        return self.project_dir(project_id) / "artifacts"


@dataclass
class LLMConfig:
    """LLM 适配层配置。具体 provider 凭据从环境变量读取。

    MiniMax / MiMo / DeepSeek 均兼容 OpenAI 协议，复用 OpenAIProvider，
    通过不同的 base_url + api_key 区分。
    """

    tasks_config_path: Path  # tasks.yaml 路径
    openai_api_key_env: str = "OPENAI_API_KEY"
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    local_model_base_url_env: str = "LOCAL_LLM_BASE_URL"  # 兼容 OpenAI 协议的本地服务

    # MiniMax Token Plan（兼容 OpenAI 协议）
    minimax_api_key_env: str = "MINIMAX_API_KEY"
    minimax_base_url_env: str = "MINIMAX_BASE_URL"
    minimax_base_url_default: str = "https://api.minimaxi.com/v1"

    # 小米 MiMo（兼容 OpenAI 协议）
    mimo_api_key_env: str = "MIMO_API_KEY"
    mimo_base_url_env: str = "MIMO_BASE_URL"
    mimo_base_url_default: str = "https://api.mimo.xiaomi.com/v1"

    # DeepSeek（兼容 OpenAI 协议）
    deepseek_api_key_env: str = "DEEPSEEK_API_KEY"
    deepseek_base_url_env: str = "DEEPSEEK_BASE_URL"
    deepseek_base_url_default: str = "https://api.deepseek.com/v1"

    default_timeout_seconds: int = 120
    max_retries: int = 3


@dataclass
class VectorStoreConfig:
    """向量库配置（ChromaDB 起步）。"""

    collection_prefix: str = "paper_chunks_"  # 每个项目独立 collection
    embedding_dim: int = 1536  # 默认 OpenAI text-embedding-3-small 维度
    distance_metric: str = "cosine"


@dataclass
class GlobalConfig:
    """全局配置聚合。"""

    paths: ProjectPaths = field(default_factory=ProjectPaths.default)
    llm: LLMConfig = field(
        default_factory=lambda: LLMConfig(
            tasks_config_path=PROJECT_ROOT / "config" / "tasks.yaml"
        )
    )
    vector_store: VectorStoreConfig = field(default_factory=VectorStoreConfig)
    # dry_run=True 时不执行真实 LLM 调用，全部用占位数据返回（默认 True）
    # 用户配置好 .env 且确认调用后，设为 False 或设环境变量 SRA_DRY_RUN=false
    dry_run: bool = True

    @classmethod
    def load(cls) -> "GlobalConfig":
        """从环境变量加载配置。"""
        cfg = cls()
        # 允许通过环境变量覆盖 tasks 配置路径
        env_tasks_path = os.environ.get("SRA_TASKS_CONFIG_PATH")
        if env_tasks_path:
            cfg.llm.tasks_config_path = Path(env_tasks_path)
        # dry_run 开关：默认 true，设 SRA_DRY_RUN=false 关闭
        env_dry_run = os.environ.get("SRA_DRY_RUN", "true").strip().lower()
        cfg.dry_run = env_dry_run not in ("false", "0", "no", "off")
        return cfg


# 默认全局配置实例（懒加载）
_default_config: Optional[GlobalConfig] = None


def get_config() -> GlobalConfig:
    """获取默认全局配置（懒加载单例）。"""
    global _default_config
    if _default_config is None:
        _default_config = GlobalConfig.load()
    return _default_config
