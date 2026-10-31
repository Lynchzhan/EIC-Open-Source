"""核对批准门引用的原始生产记录，不接受仅有 passed 标记的摘要。"""
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
import re


REQUIRED_KINDS = frozenset({
    "real_model_quality", "real_database_and_dependencies", "fresh_lock_build",
    "frontend_browser_acceptance", "database_backup_restore",
    "capacity_and_budget_approval", "human_release_approval",
})


def contained_file(root, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError("证据路径为空或类型无效")
    path = PurePosixPath(relative)
    if (path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative
            or path.as_posix() != relative):
        raise ValueError("证据路径必须位于门文件目录内")
    target = root.joinpath(*path.parts)
    if (any(parent.is_symlink() or getattr(parent, "is_junction", lambda: False)() for parent in (target, *target.parents))
            or not target.resolve().is_relative_to(root)):
        raise ValueError("证据路径越界或含链接")
    if not target.is_file() or target.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("证据缺失或超过大小限制")
    return target


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_records(gate_path, gate, source_members, image_digests):
    from harness.boundaries import canonical, read_json
    root = gate_path.resolve().parent
    source = hashlib.sha256(canonical(source_members).encode()).hexdigest()
    images = image_digests or gate.get("imageDigests")
    run_id = gate.get("runId")
    if (not isinstance(images, dict) or not images or gate.get("imageDigests") != images
            or any(not isinstance(key, str) or not key or not isinstance(value, str)
                   or not re.fullmatch(r"sha256:[a-f0-9]{64}", value) for key, value in images.items())
            or not isinstance(run_id, str) or not run_id):
        raise ValueError("完整门缺少镜像和运行绑定")
    production = gate.get("productionEvidence")
    if not isinstance(production, dict) or set(production) != REQUIRED_KINDS:
        raise ValueError("原始生产证据种类不完整")
    now = datetime.now(UTC)
    for kind, reference in production.items():
        if (not isinstance(reference, dict) or reference.get("passed") is not True
                or not isinstance(reference.get("recordSha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", reference["recordSha256"])):
            raise ValueError("原始生产证据引用无效: " + kind)
        record_path = contained_file(root, reference.get("recordPath", "evidence/" + reference["recordSha256"] + ".json"))
        if digest(record_path) != reference["recordSha256"]:
            raise ValueError("原始证据摘要不匹配: " + kind)
        record = read_json(record_path.read_bytes())
        if (record.get("schemaVersion") != 1 or record.get("kind") != kind or record.get("passed") is not True
                or record.get("scope") != "production_verified" or record.get("sourceMembersSha256") != source
                or record.get("imageDigests") != images or record.get("runId") != run_id):
            raise ValueError("原始证据范围或版本不匹配: " + kind)
        started = datetime.fromisoformat(record["startedAt"])
        finished = datetime.fromisoformat(record["finishedAt"])
        expires = datetime.fromisoformat(record["expiresAt"])
        if (any(value.tzinfo is None for value in (started, finished, expires))
                or not started <= finished <= now + timedelta(minutes=5)
                or expires <= now or expires <= finished):
            raise ValueError("原始证据时间无效或已到期: " + kind)
        artifacts = record.get("artifacts")
        if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 128:
            raise ValueError("原始证据没有可核对的产物: " + kind)
        seen = set()
        for artifact in artifacts:
            if (not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str)
                    or artifact["path"] in seen):
                raise ValueError("原始产物路径无效或重复: " + kind)
            seen.add(artifact["path"])
            if digest(contained_file(root, artifact["path"])) != artifact.get("sha256"):
                raise ValueError("原始产物摘要不匹配: " + kind)
        if kind in {"human_release_approval", "capacity_and_budget_approval"}:
            approval = record.get("approval", {})
            if (approval.get("decision") != "approved" or not approval.get("actor")
                    or not isinstance(approval.get("actor"), str) or not approval["actor"].strip()
                    or approval.get("sourceMembersSha256") != source or approval.get("imageDigests") != images):
                raise ValueError("人工批准范围不匹配: " + kind)


def referenced_paths(gate_path):
    from harness.boundaries import read_json
    root = gate_path.resolve().parent
    gate = read_json(gate_path.read_bytes())
    paths = {}
    for reference in gate["productionEvidence"].values():
        relative = reference.get("recordPath", "evidence/" + reference["recordSha256"] + ".json")
        record = contained_file(root, relative)
        paths[relative] = record
        for artifact in read_json(record.read_bytes())["artifacts"]:
            paths[artifact["path"]] = contained_file(root, artifact["path"])
    return paths
