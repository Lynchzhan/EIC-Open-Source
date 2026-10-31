from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tarfile
from datetime import datetime
from pathlib import Path
from release_policy import excluded_source_path
from source_secrets import require_public_content

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT.parent / ".packages" / f"Enterprise-Intelligence-Center-cloud-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"

def should_skip(path: Path, root: Path = ROOT) -> bool:
    rel = path.relative_to(root)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if reparse_point and path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & reparse_point:
        return True
    return excluded_source_path(rel.as_posix())


def package_source(root: Path, output: Path) -> dict:
    root, output = root.resolve(), output.resolve()
    manifest_path = output.with_name(output.name + ".manifest.json")
    if not root.is_dir() or output.exists() or manifest_path.exists():
        raise ValueError("Source must exist and output/manifest must not already exist")
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(name for name in dirs if not should_skip(base / name, root))
        for name in sorted(names):
            path = base / name
            if path in {output, manifest_path} or should_skip(path, root) or not path.is_file():
                continue
            if not path.resolve().is_relative_to(root):
                raise ValueError("Package source escapes the selected root")
            require_public_content(path.relative_to(root).as_posix(), path.read_bytes())
            files.append(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    records = []
    with tarfile.open(output, "x:gz", dereference=False) as tar:
        for path in files:
            source = path.read_bytes()
            require_public_content(path.relative_to(root).as_posix(), source)
            before = hashlib.sha256(source).hexdigest()
            tar.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
            if hashlib.sha256(path.read_bytes()).hexdigest() != before:
                raise ValueError("Source changed during packaging; do not publish this archive")
            records.append({"member": path.relative_to(root).as_posix(), "sha256": before})
    with tarfile.open(output, "r:gz") as tar:
        members = tar.getmembers()
        fingerprints = {record["member"]: record["sha256"] for record in records}
        if len(members) != len(records) or [entry.name for entry in members] != [entry["member"] for entry in records]:
            raise ValueError("Archive member inventory does not match")
        for member in members:
            relative = Path(member.name)
            if not member.isfile() or relative.is_absolute() or ".." in relative.parts or should_skip(root / relative, root):
                raise ValueError("Archive contains an excluded or non-regular member")
            archived = tar.extractfile(member)
            if archived is None:
                raise ValueError("Archive member missing")
            content = archived.read()
            if hashlib.sha256(content).hexdigest() != fingerprints[member.name]:
                raise ValueError("Archive content does not match source fingerprint")
            require_public_content(member.name, content)
    report = {"artifact": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "size_bytes": output.stat().st_size, "members": records,
              "scope": "source_filename_policy_member_integrity_and_high_confidence_secret_scan_not_exhaustive"}
    manifest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    report = package_source(args.root, args.output)
    print(report["artifact"])
    print("files", len(report["members"]))
    digest = report["sha256"]
    print("sha256", digest)
    print("size", report["size_bytes"])


if __name__ == "__main__":
    main()
