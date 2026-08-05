"""文本切分工具。

供 PaperIngestAgent 把论文 abstract/正文切分为 chunk。
设计要点：
- 按段落切分（保留语义完整性）
- 每 chunk 控制在 max_tokens 上限内（粗略估算：1 token ≈ 4 char 英文 / 1.5 char 中文）
- chunk 之间可重叠 overlap（保证跨段落语义不丢）
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextChunk:
    """切分后的 chunk。"""

    text: str
    index: int
    start_char: int = 0
    end_char: int = 0


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（英文为主，4 char/token）。"""
    # 简化估算：对中文按 1.5 char/token 加权
    # 实际场景下若精度要求高，应换用 tiktoken
    en_chars = sum(1 for c in text if ord(c) < 128)
    zh_chars = len(text) - en_chars
    return en_chars // 4 + int(zh_chars / 1.5)


def split_into_chunks(
    text: str,
    max_tokens: int = 500,
    overlap_tokens: int = 50,
    min_chunk_tokens: int = 30,
) -> list[TextChunk]:
    """按段落 + token 上限切分文本。

    Args:
        text: 原始文本
        max_tokens: 单 chunk 最大 token 数
        overlap_tokens: chunk 间重叠 token 数（保留上下文）
        min_chunk_tokens: 最小 chunk token 数（小于此数则合并到上一 chunk）

    Returns:
        list[TextChunk]，按文档顺序
    """
    if not text or not text.strip():
        return []

    # 按段落切分（保留段落内语义）
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]

    chunks: list[TextChunk] = []
    current_parts: list[str] = []
    current_tokens = 0
    current_start = 0
    cursor = 0

    for para in paragraphs:
        para_tokens = _estimate_tokens(para)

        # 单段落就超长：硬切分
        if para_tokens > max_tokens:
            # 先 flush 当前累积
            if current_parts and current_tokens >= min_chunk_tokens:
                chunk_text = "\n\n".join(current_parts)
                chunks.append(TextChunk(
                    text=chunk_text,
                    index=len(chunks),
                    start_char=current_start,
                    end_char=cursor,
                ))
                current_parts = []
                current_tokens = 0

            # 硬切超长段落（按句号/空格切）
            words = para.split(" ")
            sub: list[str] = []
            sub_tokens = 0
            for w in words:
                w_tokens = _estimate_tokens(w)
                if sub_tokens + w_tokens > max_tokens and sub:
                    sub_text = " ".join(sub)
                    chunks.append(TextChunk(
                        text=sub_text,
                        index=len(chunks),
                        start_char=cursor,
                        end_char=cursor + len(sub_text),
                    ))
                    # overlap：保留末尾若干词
                    overlap_words = sub[-min(5, len(sub)):] if overlap_tokens > 0 else []
                    sub = list(overlap_words)
                    sub_tokens = sum(_estimate_tokens(x) for x in sub)
                sub.append(w)
                sub_tokens += w_tokens
            if sub:
                sub_text = " ".join(sub)
                chunks.append(TextChunk(
                    text=sub_text,
                    index=len(chunks),
                    start_char=cursor,
                    end_char=cursor + len(sub_text),
                ))
            cursor += len(para) + 2  # \n\n
            continue

        # 常规情况：累积到 max_tokens
        if current_tokens + para_tokens > max_tokens and current_parts:
            # flush 当前 chunk
            chunk_text = "\n\n".join(current_parts)
            chunks.append(TextChunk(
                text=chunk_text,
                index=len(chunks),
                start_char=current_start,
                end_char=cursor,
            ))
            # overlap：保留上一 chunk 末尾段落
            if overlap_tokens > 0 and current_parts:
                last_para = current_parts[-1]
                if _estimate_tokens(last_para) <= overlap_tokens:
                    current_parts = [last_para]
                    current_tokens = _estimate_tokens(last_para)
                else:
                    current_parts = []
                    current_tokens = 0
            else:
                current_parts = []
                current_tokens = 0
            current_start = cursor

        if not current_parts:
            current_start = cursor

        current_parts.append(para)
        current_tokens += para_tokens
        cursor += len(para) + 2  # \n\n

    # flush 末尾
    if current_parts and current_tokens >= min_chunk_tokens:
        chunk_text = "\n\n".join(current_parts)
        chunks.append(TextChunk(
            text=chunk_text,
            index=len(chunks),
            start_char=current_start,
            end_char=cursor,
        ))
    elif current_parts and chunks:
        # 末尾过短，合并到上一 chunk
        last = chunks[-1]
        last.text = last.text + "\n\n" + "\n\n".join(current_parts)
        last.end_char = cursor
    elif current_parts:
        # 整段就一个 chunk 且过短，仍保留
        chunks.append(TextChunk(
            text="\n\n".join(current_parts),
            index=len(chunks),
            start_char=current_start,
            end_char=cursor,
        ))

    return chunks
