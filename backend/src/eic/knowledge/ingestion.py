from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from eic.knowledge.documents import page_numbers_in, parent_child_chunks
from eic.knowledge.text_splitters import validate_chunk_settings


def _marked_pages(text: str) -> tuple[tuple[int, str], ...]:
    """仅供显式 MinerU 页标记协议使用，不推测普通正文的真实页码。"""
    pages = []
    matches = list(re.finditer(r"(?m)^\[第(\d+)页\]\n", text))
    if not matches or matches[0].start() != 0:
        raise ValueError("missing_pdf_page_markers")
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages.append((int(match.group(1)), text[match.end():end].strip()))
    return tuple(pages)


@dataclass(frozen=True)
class IngestionChunk:
    parent_index: int
    child_index: int
    content: str
    content_hash: str
    metadata: dict[str, object]


def build_ingestion_chunks(
    text: str,
    *,
    source_name: str,
    parent_size: int = 2400,
    child_size: int = 900,
    child_overlap: int = 120,
    page_aware: bool = False,
    trusted_pages: tuple[tuple[int, str], ...] = (),
) -> list[IngestionChunk]:
    """Create deterministic chunk metadata before calling an embedding provider."""
    validate_chunk_settings(parent_size, 0)
    validate_chunk_settings(child_size, child_overlap)
    if parent_size < child_size:
        raise ValueError("parent_size must be at least child_size")
    if page_aware and not trusted_pages:
        trusted_pages = _marked_pages(text)
    if trusted_pages:
        chunks = []
        parent_offset = 0
        for page_number, page_text in trusted_pages:
            if type(page_number) is not int or page_number < 1:
                raise ValueError("page numbers must be positive integers")
            if not isinstance(page_text, str):
                raise TypeError("page content must be a string")
            prefix = f"[第{page_number}页]\n"
            content = page_text.removeprefix(prefix)
            reserve = len(prefix)
            if child_size <= reserve or child_overlap >= child_size - reserve:
                raise ValueError("page_marker_exceeds_chunk_budget")
            page_chunks = build_ingestion_chunks(
                content, source_name=source_name, parent_size=parent_size - reserve,
                child_size=child_size - reserve, child_overlap=child_overlap,
            )
            for chunk in page_chunks:
                content = prefix + chunk.content
                chunks.append(IngestionChunk(
                    parent_index=parent_offset + chunk.parent_index,
                    child_index=len(chunks), content=content,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    metadata={**chunk.metadata, "pageNumbers": [page_number],
                              "parent_content": prefix + str(chunk.metadata["parent_content"]),
                              "parent_page_numbers": [page_number]},
                ))
            if page_chunks:
                parent_offset += page_chunks[-1].parent_index + 1
        return chunks
    return [
        IngestionChunk(
            parent_index=draft.parent_index,
            child_index=draft.child_index,
            content=draft.content,
            content_hash=hashlib.sha256(draft.content.encode("utf-8")).hexdigest(),
            metadata={
                "source_name": source_name,
                "parent_content": draft.parent_content,
                "pageNumbers": page_numbers_in(draft.content),
                "parent_page_numbers": page_numbers_in(draft.parent_content),
            },
        )
        for draft in parent_child_chunks(
            text,
            parent_size=parent_size,
            child_size=child_size,
            child_overlap=child_overlap,
        )
    ]
