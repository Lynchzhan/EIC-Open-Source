"""唯一云发布入口：不可变源码、显式目标、镜像 ID、schema 和发布门禁。"""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import tarfile


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_helpers():
    spec = importlib.util.spec_from_file_location("release_helpers", Path(__file__).with_name("deploy-cloud.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_image_archive(path, images):
    """核验 docker save 中实际 config 指纹，不依赖可漂移 tag。"""
    required = {item["digest"] for item in images.values()}
    with tarfile.open(path, "r:*") as archive:
        entries = archive.getmembers()
        names = [entry.name for entry in entries]
        if len(set(names)) != len(names):
            raise ValueError("镜像归档含重复成员")
        for entry in entries:
            name = PurePosixPath(entry.name)
            if name.is_absolute() or ".." in name.parts or "\\" in entry.name or not (entry.isfile() or entry.isdir()):
                raise ValueError("镜像归档成员不安全")
        source = archive.extractfile("manifest.json")
        if source is None:
            raise ValueError("镜像归档缺少 docker save 清单")
        rows = json.load(source)
        found = set()
        for row in rows:
            config = archive.extractfile(row["Config"])
            if config is None:
                raise ValueError("镜像归档缺少 config")
            found.add("sha256:" + hashlib.sha256(config.read()).hexdigest())
        if not required.issubset(found):
            raise ValueError("镜像归档未包含发布清单要求的 image ID")


def source_schema_head(source):
    revisions = {}
    with tarfile.open(source, "r:*") as archive:
        for member in archive.getmembers():
            if not (member.isfile() and member.name.startswith("backend/migrations/versions/")
                    and member.name.endswith(".py") and not member.name.endswith("/__init__.py")):
                continue
            values = {}
            for node in ast.parse(archive.extractfile(member).read().decode("utf-8-sig")).body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                            values[target.id] = ast.literal_eval(node.value)
            revision, parent = values.get("revision"), values.get("down_revision")
            if not isinstance(revision, str) or not revision or revision in revisions:
                raise ValueError("源码迁移 revision 缺失或重复")
            parents = [] if parent is None else [parent] if isinstance(parent, str) else list(parent)
            if any(not isinstance(item, str) for item in parents):
                raise ValueError("源码迁移 parent 无效")
            revisions[revision] = parents
    parents = {item for values in revisions.values() for item in values}
    heads = set(revisions) - parents
    if len(heads) != 1 or parents - set(revisions):
        raise ValueError("源码迁移不是完整单 head")
    head = next(iter(heads))
    seen, active = set(), set()
    def walk(revision):
        if revision in active:
            raise ValueError("源码迁移存在环")
        if revision in seen:
            return
        active.add(revision)
        for parent in revisions[revision]:
            walk(parent)
        active.remove(revision)
        seen.add(revision)
    walk(head)
    if seen != set(revisions):
        raise ValueError("源码迁移存在不可达 revision")
    return head


def require_harness_release(gate_path, expected_sha256, source_members, image_digests=None):
    from harness.boundaries import canonical, digest, read_json
    if not gate_path or not expected_sha256 or file_digest(gate_path) != expected_sha256:
        raise ValueError("发布需要外部批准哈希绑定的 Agent Harness 门禁")
    gate = read_json(Path(gate_path).read_bytes())
    if (gate.get("schemaVersion") != 1 or gate.get("isolatedGate") is not True
            or gate.get("fullReleaseGate") is not True or gate.get("scope") != "production_verified"
            or gate.get("missingProductionEvidence") != []
            or gate.get("sourceMembersSha256") != digest(canonical(source_members).encode())):
        raise ValueError("Agent Harness 完整发布门未通过或与源码未绑定")
    if (gate.get("comparison", {}).get("passed") is not True
            or gate.get("deterministicTests", {}).get("passed") is not True):
        raise ValueError("Agent Harness 基线比较或确定性证据未通过")
    from production_evidence import REQUIRED_KINDS, verify_records
    required = REQUIRED_KINDS
    production = gate.get("productionEvidence", {})
    if set(production) != required or any(
        value.get("passed") is not True or not re.fullmatch(r"[a-f0-9]{64}", value.get("recordSha256", ""))
        for value in production.values()
    ):
        raise ValueError("Agent Harness 真实生产证据和人工审批记录缺失")
    verify_records(Path(gate_path), gate, source_members, image_digests)
    return gate


def validate_contract(source, images, manifest, remote_dir, project, compose):
    if not re.fullmatch(r"/opt/[A-Za-z0-9][A-Za-z0-9_/-]*", remote_dir) or ".." in PurePosixPath(remote_dir).parts:
        raise ValueError("远端目录必须是明确的 /opt 项目路径")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", project):
        raise ValueError("Compose project 名称无效")
    if compose not in {"docker-compose.cloud-lite.yml", "docker-compose.prod.yml", "existing-host-bind"}:
        raise ValueError("仅支持已审查的部署配置")
    helpers = load_helpers()
    helpers.validate_source_archive(source)
    record = json.loads(source.with_name(source.name + ".manifest.json").read_text())
    members = {item["member"]: item["sha256"] for item in record["members"]}
    if (manifest["source_archive_sha256"] != record["sha256"]
            or manifest["source_members"] != members
            or manifest["images_archive_sha256"] != file_digest(images)):
        raise ValueError("发布清单与源码/镜像归档不一致")
    locks = {name: digest for name, digest in members.items() if name.endswith(("package-lock.json", "uv.lock"))}
    if not locks or manifest["lockfiles"] != locks:
        raise ValueError("锁文件来源不一致")
    modules = {name[len("backend/src/eic/"):]: digest for name, digest in members.items()
               if name.startswith("backend/src/eic/") and name.endswith(".py")}
    backend, frontend = manifest["images"]["backend"], manifest["images"]["frontend"]
    if backend["runtime_modules"] != modules or not frontend["assets"] or manifest["schema_head"] != source_schema_head(source):
        raise ValueError("运行源码、前端资产或 schema 证明缺失")
    frontend_sources = {name: digest for name, digest in members.items() if name.startswith("frontend/")}
    build = manifest.get("frontend_build", {})
    if (build.get("exit_code") != 0 or build.get("source_members") != frontend_sources
            or build.get("assets") != frontend["assets"]):
        raise ValueError("前端构建源码与实际资产未绑定")
    for item in (backend, frontend):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", item["digest"]):
            raise ValueError("镜像必须绑定完整 digest/本地 image ID")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]+", item["reference"]):
            raise ValueError("镜像引用无效")
    if 'schema_transition' in manifest:
        if compose != 'existing-host-bind':
            raise ValueError("schema迁移只支持经审查的既有目录布局")
        from schema_transition import validate_transition
        validate_transition(manifest['schema_transition'], manifest)
    validate_image_archive(images, manifest["images"])
    return helpers


def deployment_script(remote_dir, project, compose, stamp, package, image_archive,
                      environment, source_digest, images_digest, manifest):
    q = shlex.quote
    release = remote_dir + "/releases/" + stamp
    backend, frontend = manifest["images"]["backend"], manifest["images"]["frontend"]
    command = f"docker compose -p {q(project)} -f {q(compose)}"
    # 不移动现有 checkout，不生成替代密钥，不收缩 schema，不删除卷。
    return f"""
set -euo pipefail
umask 077
printf '%s  %s\\n' {q(source_digest)} {q(package)} | sha256sum -c -
printf '%s  %s\\n' {q(images_digest)} {q(image_archive)} | sha256sum -c -
test ! -e {q(release)}
mkdir -p {q(remote_dir + '/releases')}
mkdir -m 700 {q(release)}
tar -xzf {q(package)} -C {q(release)}
mv {q(environment)} {q(release + '/deploy/.env')}
docker load -i {q(image_archive)}
test "$(docker image inspect --format '{{{{.Id}}}}' {q(backend['reference'])})" = {q(backend['digest'])}
test "$(docker image inspect --format '{{{{.Id}}}}' {q(frontend['reference'])})" = {q(frontend['digest'])}
cd {q(release + '/deploy')}
{command} config --quiet
{command} run --rm -T --interactive=false --pull never migrate
{command} run --rm -T --interactive=false --pull never --no-deps backend python -m eic.core.schema check
{command} up -d --no-build --pull never
{command} exec -T backend python -c '
import json,time,urllib.request,urllib.error
for attempt in range(40):
 try:
  with urllib.request.urlopen("http://127.0.0.1:8000/api/Health/Ready",timeout=12) as response:
   if response.status==200 and json.load(response)["status"]=="ready": break
 except (OSError,ValueError): pass
 time.sleep(1)
else: raise SystemExit("readiness 未通过，发布中止")
'
echo RELEASE_READINESS_PASS
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host", "remote-dir", "project-name", "compose-file"):
        parser.add_argument("--" + name, required=True)
    for name in ("source-tar", "images-tar", "release-manifest"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--user", default="root")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--harness-gate", type=Path)
    parser.add_argument("--expected-harness-gate-sha256")
    parser.add_argument("--identity-file", type=Path)
    parser.add_argument("--known-hosts", type=Path)
    parser.add_argument("--layout-json", type=Path)
    parser.add_argument("--restore-receipt", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9.-]+", args.host):
        parser.error("主机名无效")
    manifest = json.loads(args.release_manifest.read_text(encoding="utf-8"))
    helpers = validate_contract(args.source_tar, args.images_tar, manifest,
                                args.remote_dir, args.project_name, args.compose_file)
    layout = None
    if args.compose_file == "existing-host-bind":
        from existing_host_release import validate_layout
        if args.layout_json is None or args.restore_receipt is None:
            parser.error("既有目录挂载发布需要布局清单及恢复凭据")
        layout = json.loads(args.layout_json.read_text(encoding="utf-8"))
        validate_layout(layout, args.remote_dir)
        if (manifest.get("existing_layout_sha256") != file_digest(args.layout_json)
                or manifest.get("restore_receipt_sha256") != file_digest(args.restore_receipt)):
            raise ValueError("布局或恢复凭据与发布清单未绑定")
    print(json.dumps({"mode": "apply" if args.apply else "preview", "project": args.project_name,
                      "remote_dir": args.remote_dir, "source_sha256": manifest["source_archive_sha256"],
                      "full_release_gate": manifest["release_gates"].get("full_release_gate") is True},
                     ensure_ascii=False))
    if not args.apply:
        return
    require_harness_release(args.harness_gate, args.expected_harness_gate_sha256, manifest["source_members"],
                            {key: value["digest"] for key, value in manifest["images"].items()})
    gates = manifest["release_gates"]
    if not all(gates.get(key) is True for key in ("full_release_gate", "correctness", "restore", "policy_approved")):
        raise SystemExit("发布门禁未通过；保留现有部署")
    password = os.environ.get("EIC_SSH_PASS") or os.environ.get("EIC_SSH_PASSWORD")
    identity = args.identity_file or os.environ.get("EIC_SSH_IDENTITY_FILE")
    if not password and not identity:
        raise SystemExit("未配置 SSH 凭据")
    client = helpers.connect(args.host, args.port, args.user, password,
                             identity_file=identity, known_hosts=args.known_hosts)
    try:
        if layout is not None:
            from existing_host_release import remote_apply
            remote_apply(client, helpers, args, manifest)
            return
        sftp = client.open_sftp()
        try:
            with sftp.file(args.remote_dir + "/deploy/.env", "r") as file:
                existing = file.read()
            if isinstance(existing, bytes):
                existing = existing.decode("utf-8")
            env, _ = helpers.prepare_environment(existing, args.host)
            # 先移除旧镜像配置，避免重复键覆盖和旧 tag 漂移。
            env = "\n".join(line for line in env.splitlines()
                            if line.split("=", 1)[0].strip() not in {"EIC_BACKEND_IMAGE", "EIC_FRONTEND_IMAGE"}) + "\n"
            env += ("EIC_BACKEND_IMAGE=" + manifest["images"]["backend"]["digest"] + "\n"
                    "EIC_FRONTEND_IMAGE=" + manifest["images"]["frontend"]["digest"] + "\n")
            import secrets
            stamp = manifest["source_archive_sha256"][:16] + "-" + secrets.token_hex(6)
            prefix = "/tmp/eic-release-" + stamp
            for local, remote in [(args.source_tar, prefix + ".tar.gz"), (args.images_tar, prefix + ".images.tar")]:
                helpers.sftp_put(client, local, remote)
            with sftp.file(prefix + ".env", "wx") as file:
                sftp.chmod(prefix + ".env", 0o600)
                file.write(env)
        finally:
            sftp.close()
        helpers.run(client, deployment_script(
            args.remote_dir, args.project_name, args.compose_file, stamp, prefix + ".tar.gz",
            prefix + ".images.tar", prefix + ".env", manifest["source_archive_sha256"],
            manifest["images_archive_sha256"], manifest), timeout=900)
        print("发布步骤执行通过；观察期和最终验收仍需独立完成")
    finally:
        client.close()


if __name__ == "__main__":
    main()
