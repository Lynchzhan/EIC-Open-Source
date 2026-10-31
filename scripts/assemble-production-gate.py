"""组装已有真实证据；不运行评测、不生成批准，也不代替外部 gate 哈希批准。"""
from __future__ import annotations

import argparse
from pathlib import Path
import re

from harness.boundaries import canonical, digest, read_json, validate_lock
from production_evidence import REQUIRED_KINDS, contained_file, verify_records
from release_policy import public_source_sha256


FREEZE_DEPENDENCIES = {
    "scripts/assemble-production-gate.py", "scripts/production_evidence.py",
    "scripts/source_secrets.py", "scripts/release_policy.py",
    "scripts/release_deploy.py", "scripts/existing_host_release.py",
    "scripts/deploy-cloud.py",
}


def load_document(path):
    path = Path(path)
    if (any(item.is_symlink() or getattr(item, "is_junction", lambda: False)()
            for item in (path, *path.parents)) or not path.is_file()
            or path.stat().st_size > 16 * 1024 * 1024):
        raise ValueError("输入文件缺失、超限或含链接")
    raw = path.read_bytes()
    value = read_json(raw)
    if not isinstance(value, dict):
        raise ValueError("输入必须是 JSON 对象")
    return value, digest(raw)


def assemble(args):
    root = args.project_root.absolute()
    output = args.output.absolute()
    if (not output.parent.is_dir() or output.exists() or output.is_symlink()
            or output.resolve().is_relative_to(root.resolve())
            or any(item.is_symlink() or getattr(item, "is_junction", lambda: False)()
                   for item in output.parents)):
        raise ValueError("输出须为项目外既有证据目录中的新文件，且不能含链接")
    lock = validate_lock(root, args.lock, args.expected_lock_sha256)
    if not FREEZE_DEPENDENCIES <= lock["files"].keys():
        raise ValueError("新冻结清单未覆盖生产门组装及发布依赖")
    members, _ = load_document(args.source_members)
    if (not members or any(not isinstance(name, str) or not isinstance(value, str)
                           or not re.fullmatch(r"[a-f0-9]{64}", value) for name, value in members.items())):
        raise ValueError("源码成员清单无效")
    source_sha = digest(canonical(members).encode())
    if public_source_sha256(root) != source_sha:
        raise ValueError("源码成员清单与当前干净候选不一致")
    for name, expected in lock["files"].items():
        if members.get(name) != expected:
            raise ValueError("源码成员未绑定新冻结依赖: " + name)
    if members.get("evals/LOCK.json") != args.expected_lock_sha256:
        raise ValueError("源码清单未包含本次批准的正式冻结文件")
    isolated, isolated_sha = load_document(args.isolated_gate)
    if (isolated_sha != args.expected_isolated_sha256 or isolated.get("schemaVersion") != 1
            or isolated.get("isolatedGate") is not True or isolated.get("fullReleaseGate") is not False
            or isolated.get("scope") != "deterministic_synthetic_not_real_model_quality"
            or isolated.get("sourceMembersSha256") != source_sha
            or isolated.get("lockSha256") != args.expected_lock_sha256
            or isolated.get("comparison", {}).get("passed") is not True
            or isolated.get("deterministicTests", {}).get("passed") is not True):
        raise ValueError("隔离门未通过或不属于本候选和新冻结")
    images, _ = load_document(args.image_digests)
    records, _ = load_document(args.records_index)
    if set(records) != REQUIRED_KINDS or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", args.run_id):
        raise ValueError("七类原始记录或 run ID 无效")
    evidence_root = output.parent.resolve()
    references = {}
    for kind, relative in records.items():
        record_path = contained_file(evidence_root, relative)
        if record_path.resolve() == output.resolve():
            raise ValueError("门文件不能引用自身")
        references[kind] = {"passed": True, "recordPath": relative,
                            "recordSha256": digest(record_path.read_bytes())}
    gate = {**isolated, "scope": "production_verified", "fullReleaseGate": True,
            "missingProductionEvidence": [], "productionEvidence": references,
            "imageDigests": images, "runId": args.run_id, "isolatedGateSha256": isolated_sha}
    # 只在所有原始记录及产物均核对成功后写出，不改写输入或生成任何原始通过记录。
    verify_records(output, gate, members, images)
    data = (canonical(gate) + "\n").encode()
    with output.open("xb") as stream:
        stream.write(data)
    print("已组装候选完整门；仍须独立人工审查并批准此 gate SHA256: " + digest(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--expected-lock-sha256", required=True)
    parser.add_argument("--isolated-gate", type=Path, required=True)
    parser.add_argument("--expected-isolated-sha256", required=True)
    parser.add_argument("--source-members", type=Path, required=True)
    parser.add_argument("--image-digests", type=Path, required=True)
    parser.add_argument("--records-index", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        assemble(args)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(2, "生产证据组装已拒绝: " + str(error) + "\n")


if __name__ == "__main__":
    main()
