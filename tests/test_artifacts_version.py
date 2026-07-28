"""ArtifactManager 版本管理测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.artifacts.version import (
    ArtifactContentStore,
    ArtifactManager,
    ArtifactVersionError,
)
from core.knowledge.schema import ArtifactType


# ===== ArtifactContentStore =====


def test_content_store_creates_dir_on_init(tmp_path: Path):
    """ArtifactContentStore 初始化时应创建根目录。"""
    target = tmp_path / "artifacts"
    assert not target.exists()

    ArtifactContentStore(artifacts_dir=target)

    assert target.exists()


def test_content_store_write_text_content(tmp_path: Path):
    """write_text_content 应写入文本并返回路径。"""
    store = ArtifactContentStore(artifacts_dir=tmp_path / "artifacts")

    path = store.write_text_content(
        artifact_group="g1", version=1, content="hello world", suffix=".txt"
    )

    assert path.exists()
    assert path.read_text(encoding="utf-8") == "hello world"
    assert path.name == "v1.txt"


def test_content_store_write_binary_content(tmp_path: Path):
    """write_binary_content 应写入二进制并返回路径。"""
    store = ArtifactContentStore(artifacts_dir=tmp_path / "artifacts")

    path = store.write_binary_content(
        artifact_group="g1", version=1, content=b"\x00\x01\x02", suffix=".bin"
    )

    assert path.exists()
    assert path.read_bytes() == b"\x00\x01\x02"


def test_content_store_import_file(tmp_path: Path):
    """import_file 应复制外部文件到 artifact 目录。"""
    store = ArtifactContentStore(artifacts_dir=tmp_path / "artifacts")
    src = tmp_path / "src.txt"
    src.write_text("源文件内容", encoding="utf-8")

    dst = store.import_file(
        artifact_group="g1", version=1, src_path=src, suffix=".txt"
    )

    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "源文件内容"


def test_content_store_read_text(tmp_path: Path):
    """read_text 应读取之前写入的文本。"""
    store = ArtifactContentStore(artifacts_dir=tmp_path / "artifacts")
    store.write_text_content("g1", 1, "内容", suffix=".md")

    text = store.read_text("g1", 1, suffix=".md")
    assert text == "内容"


def test_content_store_content_path_returns_expected(tmp_path: Path):
    """content_path 应返回 group_dir/v{version}{suffix} 路径。"""
    store = ArtifactContentStore(artifacts_dir=tmp_path / "artifacts")
    path = store.content_path("g1", 2, suffix=".tex")

    assert path.name == "v2.tex"
    assert path.parent.name == "g1"


# ===== ArtifactManager.create_artifact =====


def test_create_artifact_returns_v1_artifact(artifact_manager: ArtifactManager):
    """create_artifact 应创建新 group 与 v1。"""
    art = artifact_manager.create_artifact(
        artifact_type=ArtifactType.METHOD_DOC,
        title="方法文档",
        content="方法内容",
        source_stage="design",
        created_by="agent",
    )

    assert art.version == 1
    assert art.artifact_group == art.artifact_id or art.artifact_group  # group 已设置
    assert art.title == "方法文档"
    # 小内容（< 8192）直接存数据库
    assert art.content == "方法内容"
    assert art.content_path is None


def test_create_artifact_with_citations(artifact_manager: ArtifactManager):
    """create_artifact 应携带 cites_claim_ids / cites_experiment_ids。"""
    art = artifact_manager.create_artifact(
        artifact_type=ArtifactType.PAPER_DRAFT,
        title="论文稿",
        content="内容",
        cites_claim_ids=["c1", "c2"],
        cites_experiment_ids=["e1"],
    )

    assert art.cites_claim_ids == ["c1", "c2"]
    assert art.cites_experiment_ids == ["e1"]


def test_create_artifact_large_content_goes_to_file(
    artifact_manager: ArtifactManager, tmp_path: Path
):
    """大内容（>= 8192 字符）应写入文件，content_path 非 None。"""
    large_content = "x" * 9000

    art = artifact_manager.create_artifact(
        artifact_type=ArtifactType.PAPER_DRAFT,
        title="大文档",
        content=large_content,
    )

    assert art.content is None
    assert art.content_path is not None
    # 文件应存在
    assert Path(art.content_path).exists()


# ===== ArtifactManager.new_version =====


def test_new_version_increments_version_number(artifact_manager: ArtifactManager):
    """new_version 应在已有 group 下递增版本号。"""
    v1 = artifact_manager.create_artifact(
        artifact_type=ArtifactType.METHOD_DOC,
        title="方法",
        content="v1 内容",
    )

    v2 = artifact_manager.new_version(
        artifact_group=v1.artifact_group,
        content="v2 内容",
    )

    assert v2.version == 2
    assert v2.artifact_group == v1.artifact_group
    assert v2.parent_version_id == v1.artifact_id


def test_new_version_inherits_artifact_type_and_title(
    artifact_manager: ArtifactManager,
):
    """new_version 应继承前一版本的 artifact_type 与 title（若未指定）。"""
    v1 = artifact_manager.create_artifact(
        artifact_type=ArtifactType.FORMULA,
        title="公式 v1",
        content="a+b",
    )

    v2 = artifact_manager.new_version(
        artifact_group=v1.artifact_group,
        content="a+b+c",
    )

    assert v2.artifact_type == ArtifactType.FORMULA
    assert v2.title == "公式 v1"


def test_new_version_inherits_citations_when_not_specified(
    artifact_manager: ArtifactManager,
):
    """new_version 未传 cites_* 时应继承前一版本。"""
    v1 = artifact_manager.create_artifact(
        artifact_type=ArtifactType.PAPER_DRAFT,
        title="t",
        content="c",
        cites_claim_ids=["c1"],
    )

    v2 = artifact_manager.new_version(
        artifact_group=v1.artifact_group,
        content="v2",
    )

    assert v2.cites_claim_ids == ["c1"]


def test_new_version_rejects_unknown_group(artifact_manager: ArtifactManager):
    """对不存在的 group 创建新版本应抛 ArtifactVersionError。"""
    with pytest.raises(ArtifactVersionError):
        artifact_manager.new_version(
            artifact_group="nonexistent_group", content="x"
        )


# ===== 查询 =====


def test_get_returns_artifact_by_id(artifact_manager: ArtifactManager):
    """get 应按 artifact_id 返回 Artifact。"""
    art = artifact_manager.create_artifact(
        artifact_type=ArtifactType.METHOD_DOC,
        title="t",
        content="c",
    )

    loaded = artifact_manager.get(art.artifact_id)
    assert loaded.artifact_id == art.artifact_id
    assert loaded.title == "t"


def test_list_versions_returns_sorted(artifact_manager: ArtifactManager):
    """list_versions 应按 version 升序返回。"""
    v1 = artifact_manager.create_artifact(
        artifact_type=ArtifactType.METHOD_DOC, title="t", content="v1"
    )
    v2 = artifact_manager.new_version(v1.artifact_group, content="v2")
    v3 = artifact_manager.new_version(v1.artifact_group, content="v3")

    versions = artifact_manager.list_versions(v1.artifact_group)
    assert [v.version for v in versions] == [1, 2, 3]


def test_latest_version_returns_highest(artifact_manager: ArtifactManager):
    """latest_version 应返回 version 最大的 Artifact。"""
    v1 = artifact_manager.create_artifact(
        artifact_type=ArtifactType.METHOD_DOC, title="t", content="v1"
    )
    artifact_manager.new_version(v1.artifact_group, content="v2")

    latest = artifact_manager.latest_version(v1.artifact_group)
    assert latest is not None
    assert latest.version == 2


def test_latest_version_returns_none_for_empty_group(
    artifact_manager: ArtifactManager,
):
    """空 group 的 latest_version 应返回 None。"""
    assert artifact_manager.latest_version("empty_group") is None


# ===== read_content =====


def test_read_content_from_inline_content(artifact_manager: ArtifactManager):
    """read_content 应优先从 content 字段读。"""
    art = artifact_manager.create_artifact(
        artifact_type=ArtifactType.METHOD_DOC, title="t", content="inline 内容"
    )

    assert artifact_manager.read_content(art) == "inline 内容"


def test_read_content_from_file(artifact_manager: ArtifactManager):
    """content 为空时，read_content 应从 content_path 读。"""
    large = "y" * 9000
    art = artifact_manager.create_artifact(
        artifact_type=ArtifactType.PAPER_DRAFT, title="t", content=large
    )
    # 大内容应已写入文件
    assert art.content is None
    assert art.content_path is not None

    text = artifact_manager.read_content(art)
    assert text == large


def test_read_content_returns_none_when_no_content(artifact_manager: ArtifactManager):
    """无 content 与 content_path 时 read_content 应返回 None。"""
    art = artifact_manager.create_artifact(
        artifact_type=ArtifactType.DIAGRAM, title="t"  # 无 content
    )
    assert art.content is None
    assert art.content_path is None

    assert artifact_manager.read_content(art) is None
