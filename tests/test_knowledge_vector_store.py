"""ChromaVectorStore 测试。

使用真实 ChromaDB 本地持久化（在 tmp_path 下），配合 fake_embedding 函数。
若运行环境未安装 chromadb，相关测试应被跳过。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.knowledge.vector_store import (
    ChromaVectorStore,
    VectorQueryResult,
    VectorRecord,
)

# 检测 chromadb 是否可用
chromadb_available = True
try:
    import chromadb  # noqa: F401
except ImportError:
    chromadb_available = False

skip_if_no_chroma = pytest.mark.skipif(
    not chromadb_available, reason="未安装 chromadb，跳过 ChromaVectorStore 测试"
)


def _make_record(
    chunk_id: str,
    paper_id: str,
    text: str,
    dim: int = 8,
) -> VectorRecord:
    """构造测试用向量记录，向量内容为固定值。"""
    embedding = [0.5] * dim
    return VectorRecord(
        chunk_id=chunk_id,
        paper_id=paper_id,
        text=text,
        embedding=embedding,
        metadata={"page": 1},
    )


@skip_if_no_chroma
def test_chroma_vector_store_add_and_count(tmp_path: Path):
    """add 后 count 应返回正确数量。"""
    store = ChromaVectorStore(
        persist_dir=tmp_path / "vectors",
        project_id="proj_1",
        distance_metric="cosine",
    )

    assert store.count() == 0

    records = [
        _make_record("c1", "p1", "text 1"),
        _make_record("c2", "p1", "text 2"),
        _make_record("c3", "p2", "text 3"),
    ]
    store.add(records)

    assert store.count() == 3


@skip_if_no_chroma
def test_chroma_vector_store_query_returns_top_k(tmp_path: Path):
    """query 应返回最多 top_k 条结果。"""
    store = ChromaVectorStore(
        persist_dir=tmp_path / "vectors",
        project_id="proj_2",
    )
    records = [_make_record(f"c{i}", "p1", f"text {i}") for i in range(5)]
    store.add(records)

    # 用相同向量查询，应能返回 top 3
    results = store.query(query_embedding=[0.5] * 8, top_k=3)

    assert len(results) <= 3
    assert all(isinstance(r, VectorQueryResult) for r in results)
    # 每条结果应有 chunk_id / paper_id / text / score
    for r in results:
        assert r.chunk_id
        assert r.paper_id == "p1"
        assert r.text


@skip_if_no_chroma
def test_chroma_vector_store_query_with_paper_id_filter(tmp_path: Path):
    """query 加 paper_id_filter 应只返回该 paper 的 chunk。"""
    store = ChromaVectorStore(
        persist_dir=tmp_path / "vectors",
        project_id="proj_3",
    )
    store.add(
        [
            _make_record("c1", "p1", "text 1"),
            _make_record("c2", "p2", "text 2"),
            _make_record("c3", "p1", "text 3"),
        ]
    )

    # 只查 p1 的 chunk
    results = store.query(
        query_embedding=[0.5] * 8,
        top_k=10,
        paper_id_filter="p1",
    )
    assert all(r.paper_id == "p1" for r in results)
    assert {r.chunk_id for r in results} == {"c1", "c3"}


@skip_if_no_chroma
def test_chroma_vector_store_delete_by_paper(tmp_path: Path):
    """delete_by_paper 应删除指定 paper 的所有 chunk，返回删除条数。"""
    store = ChromaVectorStore(
        persist_dir=tmp_path / "vectors",
        project_id="proj_4",
    )
    store.add(
        [
            _make_record("c1", "p1", "text 1"),
            _make_record("c2", "p1", "text 2"),
            _make_record("c3", "p2", "text 3"),
        ]
    )
    assert store.count() == 3

    deleted = store.delete_by_paper("p1")
    assert deleted == 2
    assert store.count() == 1

    # p2 的 chunk 仍在
    results = store.query(query_embedding=[0.5] * 8, top_k=10)
    assert len(results) == 1
    assert results[0].paper_id == "p2"


@skip_if_no_chroma
def test_chroma_vector_store_add_empty_records_is_noop(tmp_path: Path):
    """add 空列表应为 no-op，不抛异常。"""
    store = ChromaVectorStore(
        persist_dir=tmp_path / "vectors",
        project_id="proj_5",
    )
    store.add([])
    assert store.count() == 0


@skip_if_no_chroma
def test_chroma_vector_store_upsert_replaces_existing(tmp_path: Path):
    """相同 chunk_id 的 add 应 upsert（替换而非追加）。"""
    store = ChromaVectorStore(
        persist_dir=tmp_path / "vectors",
        project_id="proj_6",
    )
    store.add([_make_record("c1", "p1", "original")])
    assert store.count() == 1

    # 再次 add 同一 chunk_id，应替换
    store.add([_make_record("c1", "p1", "updated")])
    assert store.count() == 1


@skip_if_no_chroma
def test_chroma_vector_store_uses_collection_prefix(tmp_path: Path):
    """collection_prefix + project_id 应作为 collection 名。"""
    store = ChromaVectorStore(
        persist_dir=tmp_path / "vectors",
        project_id="proj_x",
        collection_prefix="paper_chunks_",
    )
    # 内部 collection 名应符合命名约定
    assert store._collection_name == "paper_chunks_proj_x"


@skip_if_no_chroma
def test_chroma_vector_store_persists_across_instances(tmp_path: Path):
    """重新打开同一目录的 store 应能读到之前写入的数据。"""
    persist_dir = tmp_path / "vectors"
    store1 = ChromaVectorStore(
        persist_dir=persist_dir,
        project_id="proj_persist",
    )
    store1.add([_make_record("c1", "p1", "text")])
    assert store1.count() == 1

    # 新实例
    store2 = ChromaVectorStore(
        persist_dir=persist_dir,
        project_id="proj_persist",
    )
    assert store2.count() == 1
