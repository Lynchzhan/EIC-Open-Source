"""已授权入库资源的标准文档加载与领域切分适配。"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import BaseDocumentTransformer, Document

from eic.knowledge.documents import ExtractedDocument, extract_document, page_numbers_in
from eic.knowledge.ingestion import IngestionChunk, build_ingestion_chunks
from eic.knowledge.mineru import extract_for_ingestion
from eic.knowledge.text_splitters import validate_chunk_settings


class IngestionDocumentLoader(BaseLoader):
    """只消费服务已核权的路径或正文；本适配器不授予文件访问权限。"""

    def __init__(self, *, source_name: str, text: str, report: dict[str, Any],
                 source: Path | None = None, metadata: dict[str, Any] | None = None,
                 local_extractor=extract_document):
        self.source_name = source_name
        self.text = text
        self.report = deepcopy(report)
        self.source = source
        self.metadata = deepcopy(metadata or {})
        self.local_extractor = local_extractor

    def _document(self, extracted: ExtractedDocument) -> Document:
        # 表格正文及页标记不拆散、不重新解释；完整抽取报告随文档流转。
        return Document(page_content=extracted.text, metadata={
            **deepcopy(self.metadata), "source": self.source_name,
            "source_name": self.source_name,
            "pageNumbers": ([number for number, _ in extracted.pages]
                            if extracted.pages else page_numbers_in(extracted.text)),
            "_trusted_pages": extracted.pages,
            "extraction_report": deepcopy(extracted.report),
            "tableCount": extracted.report.get("tableCount", 0),
        })

    def lazy_load(self) -> Iterator[Document]:
        if self.source is not None:
            # 同步入口不得绕过异步 MinerU 策略而改走本地解析。
            raise ValueError("file_ingestion_requires_async_loader")
        yield self._document(ExtractedDocument(self.text, self.report))

    async def alazy_load(self) -> AsyncIterator[Document]:
        extracted = (await extract_for_ingestion(self.source, local_extractor=self.local_extractor)
                     if self.source is not None else ExtractedDocument(self.text, self.report))
        yield self._document(extracted)


class ParentChildDocumentTransformer(BaseDocumentTransformer):
    """页码与父子索引适配；实际文本切分由官方 TextSplitter 完成。"""

    def __init__(self, *, parent_size: int = 2400, child_size: int = 900,
                 child_overlap: int = 120):
        validate_chunk_settings(parent_size, 0)
        validate_chunk_settings(child_size, child_overlap)
        if parent_size < child_size:
            raise ValueError("parent_size must be at least child_size")
        self.parent_size = parent_size
        self.child_size = child_size
        self.child_overlap = child_overlap

    def transform_documents(self, documents: Sequence[Document], **kwargs: Any) -> list[Document]:
        result = []
        parent_offset = 0
        for document in documents:
            report = document.metadata.get("extraction_report") or {}
            metadata = deepcopy(document.metadata)
            trusted_pages = metadata.pop("_trusted_pages", ())
            chunks = build_ingestion_chunks(
                document.page_content, source_name=document.metadata.get("source_name", ""),
                parent_size=self.parent_size, child_size=self.child_size,
                child_overlap=self.child_overlap,
                page_aware=(report.get("extractor") or {}).get("used") == "mineru",
                trusted_pages=trusted_pages,
            )
            for chunk in chunks:
                result.append(Document(page_content=chunk.content, metadata={
                    **deepcopy(metadata), **chunk.metadata,
                    "parent_index": parent_offset + chunk.parent_index,
                    "child_index": len(result), "chunk_index": len(result),
                    "content_hash": chunk.content_hash,
                }))
            if chunks:
                parent_offset += chunks[-1].parent_index + 1
        return result


def ingestion_chunk_from_document(document: Document) -> IngestionChunk:
    """在持久化边界恢复原结构，标准 Document 是切分阶段的实际载体。"""
    metadata = deepcopy(document.metadata)
    return IngestionChunk(
        parent_index=metadata.pop("parent_index"), child_index=metadata.pop("child_index"),
        content=document.page_content, content_hash=metadata.pop("content_hash"),
        metadata=metadata,
    )
