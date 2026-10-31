"""高置信秘密特征检查；错误只包含成员名，不输出匹配值。"""
import re

PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:sk|pk)-lf-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"),
    re.compile(rb"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{30,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
)


def require_public_content(member, data):
    if any(pattern.search(data) for pattern in PATTERNS):
        raise ValueError("源码含高置信私密材料，请移出公开包: " + member)
