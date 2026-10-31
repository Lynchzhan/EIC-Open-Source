"""官方字符切分器入口；不提供静默降级算法。"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "；", "，", "、", ". ", " ", "\u200b", "")


def splitter_parameter_snapshot() -> dict[str, object]:
    """把算法、适配策略及实际安装版本纳入索引幂等输入。"""
    try:
        installed_version = version("langchain-text-splitters")
    except PackageNotFoundError:
        # 允许原入库流程记录失败；该哨兵不提供替代切分能力。
        installed_version = "unavailable"
    return {
        "algorithm": "langchain_text_splitters.RecursiveCharacterTextSplitter",
        "adapterVersion": 1,
        "distribution": "langchain-text-splitters",
        "distributionVersion": installed_version,
        "lengthFunction": "len",
        "separators": list(_SEPARATORS),
        "keepSeparator": "end",
        "isSeparatorRegex": False,
        "stripWhitespace": True,
        "normalization": "eic-whitespace-v1",
        "pagePolicy": "trusted-pages-first-prefix-budget-v1",
        "parentOverlap": 0,
    }


def validate_chunk_settings(chunk_size: int, overlap: int) -> None:
    if (type(chunk_size) is not int or type(overlap) is not int
            or chunk_size < 1 or overlap < 0 or overlap >= chunk_size):
        raise ValueError("chunk_size must be positive and overlap must be smaller than chunk_size")


def split_text(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    """按字符预算递归切分，重叠由官方合并逻辑控制。"""
    validate_chunk_settings(chunk_size, overlap)
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError as exc:
        raise ValueError(
            "official_text_splitter_unavailable: requires langchain-text-splitters>=1.0.0,<2.0.0"
        ) from exc
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        length_function=len,
        separators=list(_SEPARATORS),
        keep_separator="end",
        is_separator_regex=False,
        strip_whitespace=True,
    )
    return splitter.split_text(text)
