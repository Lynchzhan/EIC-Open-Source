"""体验附件的独立解析子进程；临时副本也限制在当前体验会话目录。"""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    from eic.ai.attachments import extract_payload, read_bounded
    from eic.core.config import get_settings
    from eic.experience_files import sessions_root

    path = Path(sys.argv[1]).resolve(strict=True)
    root = sessions_root().resolve()
    if path.parent.parent != root or path.is_symlink():
        raise ValueError("invalid experience parser path")
    settings = get_settings()
    # 只影响子进程；共享解析器生成的任何原始字节副本也随会话清理。
    tempfile.tempdir = str(path.parent)
    payload = read_bounded(path, settings.chat_attachment_max_bytes)
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        text = extract_payload(payload, sys.argv[2], settings)
    sys.stdout.buffer.write(json.dumps({"text": text}, ensure_ascii=True).encode("ascii"))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        sys.exit(1)
