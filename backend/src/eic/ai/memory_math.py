"""记忆关键词、摘要哈希与余弦运算；不依赖召回编排或模型客户端。"""
import hashlib
import math
import re


def content_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def keywords(value):
    words = set(re.findall(r"[a-z0-9_]+", value.lower()))
    for segment in re.findall(r"[\u4e00-\u9fff]+", value):
        words.update(segment[i:i + 2] for i in range(max(1, len(segment) - 1)))
    return words


def cosine(left, right):
    if not left or len(left) != len(right) or any(not math.isfinite(v) for v in [*left, *right]):
        raise ValueError("invalid memory vector")
    norm = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("invalid memory vector norm")
    return sum(a * b for a, b in zip(left, right)) / norm
