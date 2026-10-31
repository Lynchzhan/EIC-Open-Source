from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from eic.knowledge.text_splitters import split_text, validate_chunk_settings

_OCR_ENGINE = None
_PAGE_NUMBER_RE = re.compile(r"第(\d+)页")
_MIN_OCR_EDGE = 48
# Third-party parser and OCR failures are represented as partial extraction results.


@dataclass(frozen=True)
class ExtractedDocument:
    """Extracted searchable text plus a compact ingestion report."""

    text: str
    report: dict[str, Any]
    pages: tuple[tuple[int, str], ...] = ()


def _ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise ValueError("OCR requires rapidocr_onnxruntime") from exc
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _ocr_image(image: object) -> str:
    """Run optional CPU-only RapidOCR and return stable text-only output."""
    if hasattr(image, "convert"):
        import numpy as np

        image = np.asarray(image.convert("RGB"))
    result, _ = _ocr_engine()(image)
    if not result:
        return ""
    return "\n".join(str(item[1]).strip() for item in result if len(item) >= 2 and str(item[1]).strip())


def _image_from_bytes(payload: bytes):
    from PIL import Image

    with Image.open(BytesIO(payload)) as image:
        return image.convert("RGB")


def _ocr_pixmap(pixmap) -> str:
    from PIL import Image

    return _ocr_image(Image.open(BytesIO(pixmap.tobytes("png"))))


def _ocr_pdf_page(page) -> str:
    import pymupdf as fitz

    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    return _ocr_pixmap(pixmap)


def _ocr_pdf(path: Path) -> str:
    import pymupdf as fitz

    pages = []
    with fitz.open(path) as document:
        for page in document:
            pages.append(_ocr_pdf_page(page))
    return "\n".join(page for page in pages if page.strip())


def _unique_append(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _table_to_markdown(rows: list[list[Any]] | None) -> str:
    if not rows:
        return ""
    normalized: list[list[str]] = []
    for row in rows:
        cells = ["" if cell is None else re.sub(r"\s+", " ", str(cell)).strip() for cell in row]
        if any(cells):
            normalized.append(cells)
    if not normalized:
        return ""
    width = max(len(row) for row in normalized)
    for row in normalized:
        if len(row) < width:
            row.extend([""] * (width - len(row)))
    header = normalized[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _pdf_tables(page, path: Path, page_index: int) -> tuple[str, int]:
    markdowns: list[str] = []
    try:
        finder = page.find_tables()
        tables = list(getattr(finder, "tables", []) or [])
    except Exception:  # noqa: BLE001
        tables = []
    for table in tables:
        try:
            markdown = _table_to_markdown(table.extract())
        except Exception:  # noqa: BLE001
            markdown = ""
        if markdown:
            markdowns.append(markdown)
    if markdowns:
        return "\n\n".join(markdowns), len(markdowns)
    try:
        import pdfplumber
    except ImportError:
        return "", 0
    try:
        with pdfplumber.open(path) as document:
            if page_index >= len(document.pages):
                return "", 0
            plumber_tables = document.pages[page_index].extract_tables() or []
    except Exception:  # noqa: BLE001
        return "", 0
    for table in plumber_tables:
        markdown = _table_to_markdown(table)
        if markdown:
            markdowns.append(markdown)
    return "\n\n".join(markdowns), len(markdowns)


def _pdf_embedded_images(document, page) -> tuple[list[str], int, bool]:
    import pymupdf as fitz

    texts: list[str] = []
    image_count = 0
    failed = False
    seen: set[int] = set()
    for image in page.get_images(full=True) or []:
        xref = int(image[0])
        if xref in seen:
            continue
        seen.add(xref)
        try:
            pixmap = fitz.Pixmap(document, xref)
            if pixmap.n - pixmap.alpha > 3:
                pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
            if pixmap.width < _MIN_OCR_EDGE or pixmap.height < _MIN_OCR_EDGE:
                continue
            image_count += 1
            extracted = _ocr_pixmap(pixmap).strip()
            if extracted:
                texts.append(extracted)
        except Exception:  # noqa: BLE001
            failed = True
            image_count += 1
    return texts, image_count, failed


def _empty_report(**overrides: Any) -> dict[str, Any]:
    report = {
        "methods": [],
        "failedPages": [],
        "pageCount": 0,
        "imageCount": 0,
        "tableCount": 0,
        "notes": "",
    }
    report.update(overrides)
    return report


def _extract_pdf(path: Path) -> ExtractedDocument:
    import pymupdf as fitz

    methods: list[str] = []
    failed_pages: list[int] = []
    image_count = 0
    table_count = 0
    parts: list[str] = []
    with fitz.open(path) as document:
        pages: list[tuple[int, str]] = []
        page_count = document.page_count
        for index, page in enumerate(document, start=1):
            page_parts: list[str] = [f"[第{index}页]"]
            text_layer = (page.get_text("text") or "").strip()
            tables_md, n_tables = _pdf_tables(page, path, index - 1)
            table_count += n_tables
            if text_layer:
                _unique_append(methods, "text_layer")
                page_parts.append(text_layer)
            if tables_md:
                _unique_append(methods, "table_markdown")
                page_parts.append(f"[表 第{index}页]\n{tables_md}")
            if text_layer:
                image_texts, n_images, image_failed = _pdf_embedded_images(document, page)
                image_count += n_images
                if image_failed:
                    failed_pages.append(index)
                if image_texts:
                    _unique_append(methods, "embedded_image_ocr")
                    for image_text in image_texts:
                        page_parts.append(f"[图 第{index}页]\n{image_text}")
            else:
                try:
                    scanned = _ocr_pdf_page(page).strip()
                except Exception:  # noqa: BLE001
                    scanned = ""
                    failed_pages.append(index)
                if scanned:
                    _unique_append(methods, "scan_ocr")
                    page_parts.append(scanned)
                elif index not in failed_pages:
                    failed_pages.append(index)
            page_text = "\n".join(part for part in page_parts if str(part).strip())
            if page_text.strip() and page_text.strip() != f"[第{index}页]":
                parts.append(page_text)
                pages.append((index, page_text))
    return ExtractedDocument(
        text="\n\n".join(parts).strip(),
        pages=tuple(pages),
        report=_empty_report(
            methods=methods,
            failedPages=failed_pages,
            pageCount=page_count,
            imageCount=image_count,
            tableCount=table_count,
        ),
    )


def _docx_images_in(paragraph, document) -> list[object]:
    from docx.oxml.ns import qn

    images = []
    for blip in paragraph._element.xpath(".//a:blip"):
        rid = blip.get(qn("r:embed"))
        if not rid:
            continue
        part = document.part.related_parts.get(rid)
        if part is None or not getattr(part, "blob", None):
            continue
        try:
            images.append(_image_from_bytes(part.blob))
        except Exception:  # noqa: BLE001
            images.append(None)
    return images


def _docx_table_markdown(table) -> str:
    rows = []
    for row in table.rows:
        rows.append([cell.text for cell in row.cells])
    return _table_to_markdown(rows)


def _extract_docx(path: Path) -> ExtractedDocument:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(path)
    methods: list[str] = []
    parts: list[str] = []
    image_count = 0
    table_count = 0
    failed_pages: list[int] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if text:
                _unique_append(methods, "text_layer")
                parts.append(text)
            for image in _docx_images_in(paragraph, document):
                image_count += 1
                if image is None:
                    failed_pages.append(image_count)
                    continue
                try:
                    extracted = _ocr_image(image).strip()
                except Exception:  # noqa: BLE001
                    extracted = ""
                    failed_pages.append(image_count)
                if extracted:
                    _unique_append(methods, "embedded_image_ocr")
                    parts.append(f"[图]\n{extracted}")
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            markdown = _docx_table_markdown(table)
            if markdown.strip():
                table_count += 1
                _unique_append(methods, "table_markdown")
                parts.append(f"[表]\n{markdown}")
    return ExtractedDocument(
        text="\n\n".join(parts).strip(),
        report=_empty_report(
            methods=methods,
            failedPages=failed_pages,
            pageCount=0,
            imageCount=image_count,
            tableCount=table_count,
            notes="Word 无稳定页码，图片和表格仅标记 [图]/[表]",
        ),
    )


def _extract_plain(path: Path, suffix: str) -> ExtractedDocument:
    if suffix in {".txt", ".md", ".csv"}:
        text = path.read_text(encoding="utf-8", errors="replace")
        return ExtractedDocument(text=text, report=_empty_report(methods=["text_layer"] if text.strip() else []))
    if suffix in {".html", ".htm"}:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", path.read_text(encoding="utf-8", errors="replace"))).strip()
        return ExtractedDocument(text=text, report=_empty_report(methods=["text_layer"] if text else []))
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif", ".webp"}:
        try:
            from PIL import Image
        except ImportError as exc:
            raise ValueError("image OCR requires Pillow") from exc
        with Image.open(path) as image:
            text = _ocr_image(image.copy())
        return ExtractedDocument(
            text=text,
            report=_empty_report(methods=["scan_ocr"] if text.strip() else [], imageCount=1, pageCount=1),
        )
    if suffix in {".xlsx", ".xlsm"}:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ValueError("Excel parsing requires openpyxl") from exc
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            rows = []
            table_count = 0
            for sheet in workbook.worksheets:
                rows.append(f"# {sheet.title}")
                sheet_rows = []
                for row in sheet.iter_rows(values_only=True):
                    values = ["" if value is None else str(value).strip() for value in row]
                    if any(values):
                        sheet_rows.append(values)
                markdown = _table_to_markdown(sheet_rows)
                if markdown:
                    table_count += 1
                    rows.append(markdown)
            text = "\n".join(rows)
            return ExtractedDocument(
                text=text,
                report=_empty_report(methods=["table_markdown"] if table_count else [], tableCount=table_count),
            )
        finally:
            workbook.close()
    if suffix == ".xls":
        try:
            import xlrd
        except ImportError as exc:
            raise ValueError("legacy Excel parsing requires xlrd") from exc
        workbook = xlrd.open_workbook(path, on_demand=True)
        rows = []
        table_count = 0
        for sheet in workbook.sheets():
            rows.append(f"# {sheet.name}")
            sheet_rows = []
            for row in sheet.get_rows():
                values = [str(cell.value).strip() for cell in row]
                if any(values):
                    sheet_rows.append(values)
            markdown = _table_to_markdown(sheet_rows)
            if markdown:
                table_count += 1
                rows.append(markdown)
        text = "\n".join(rows)
        return ExtractedDocument(
            text=text,
            report=_empty_report(methods=["table_markdown"] if table_count else [], tableCount=table_count),
        )
    raise ValueError(f"unsupported document type: {suffix or '<none>'}")


def extract_document(path: Path) -> ExtractedDocument:
    """Extract searchable text and an extraction report from a local document."""
    suffix = path.suffix.lower()
    if suffix == ".doc":
        raise ValueError("unsupported document type: .doc; convert to .docx")
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        return _extract_docx(path)
    return _extract_plain(path, suffix)


def extract_text(path: Path) -> str:
    """Extract local text from supported formats without executing document content."""
    return extract_document(path).text


def page_numbers_in(text: str) -> list[int]:
    """Return unique page numbers referenced by L1 markers such as [第2页]."""
    found: list[int] = []
    for match in _PAGE_NUMBER_RE.finditer(text or ""):
        number = int(match.group(1))
        if number not in found:
            found.append(number)
    return found


def chunk_text(text: str, *, chunk_size: int = 1200, overlap: int = 180) -> list[str]:
    validate_chunk_settings(chunk_size, overlap)
    normalized = re.sub(r"[ \t]+", " ", str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if not normalized:
        return []
    return split_text(normalized, chunk_size=chunk_size, overlap=overlap)


@dataclass(frozen=True)
class ChunkDraft:
    """Parent/child chunk emitted before embeddings are generated."""

    parent_index: int
    child_index: int
    parent_content: str
    content: str


def to_langchain_document(
    content: str,
    *,
    tenant_id: int,
    knowledge_base_id: int,
    document_id: int,
    chunk_id: str,
    source_name: str = "",
    page_numbers: tuple[int, ...] | list[int] = (),
    parent_chunk_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Document:
    """Build a LangChain document with trusted EIC scope and citation metadata."""
    identifiers = (tenant_id, knowledge_base_id, document_id)
    if any(type(identifier) is not int or identifier < 1 for identifier in identifiers):
        raise ValueError("tenant, knowledge-base, and document IDs must be positive integers")
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        raise ValueError("chunk ID must be a non-empty string")
    if not isinstance(content, str):
        raise TypeError("document content must be a string")

    pages: list[int] = []
    for number in page_numbers:
        if type(number) is not int or number < 1:
            raise ValueError("page numbers must be positive integers")
        if number not in pages:
            pages.append(number)

    reserved_aliases = {
        "tenantId", "knowledgeBaseId", "documentId", "chunkId", "sourceName",
        "page_numbers", "parent_chunk_id", "parentChunkId",
    }
    document_metadata = {
        key: value for key, value in (metadata or {}).items() if key not in reserved_aliases
    }
    document_metadata.update({
        "tenant_id": tenant_id,
        "knowledge_base_id": knowledge_base_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "source_name": source_name,
        "pageNumbers": pages,
    })
    if parent_chunk_id is not None:
        document_metadata["parent_chunk_id"] = parent_chunk_id
    return Document(page_content=content, metadata=document_metadata)


def parent_child_chunks(
    text: str,
    *,
    parent_size: int = 2400,
    child_size: int = 900,
    child_overlap: int = 120,
) -> list[ChunkDraft]:
    validate_chunk_settings(parent_size, 0)
    validate_chunk_settings(child_size, child_overlap)
    if parent_size < child_size:
        raise ValueError("parent_size must be at least child_size")
    drafts: list[ChunkDraft] = []
    child_index = 0
    for parent_index, parent_content in enumerate(chunk_text(text, chunk_size=parent_size, overlap=0)):
        for content in chunk_text(parent_content, chunk_size=child_size, overlap=child_overlap):
            drafts.append(ChunkDraft(parent_index, child_index, parent_content, content))
            child_index += 1
    return drafts

