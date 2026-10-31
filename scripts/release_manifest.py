"""从真实容器生成发布清单，不以 tag 或报告标题替代运行源码证据。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def capture(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("发布来源核验命令失败，退出码: " + str(result.returncode))
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-tar", type=Path, required=True)
    parser.add_argument("--images-tar", type=Path, required=True)
    parser.add_argument("--backend-container", required=True)
    parser.add_argument("--frontend-container", required=True)
    parser.add_argument("--gates", type=Path, required=True)
    parser.add_argument("--frontend-build-record", type=Path, required=True)
    parser.add_argument("--schema-transition-plan", type=Path)
    parser.add_argument("--layout-json", type=Path)
    parser.add_argument("--restore-receipt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("发布清单输出已存在")
    if bool(args.layout_json) != bool(args.restore_receipt):
        raise ValueError("既有布局与恢复凭据必须同时提供")
    from release_deploy import load_helpers, file_digest, validate_image_archive, source_schema_head
    load_helpers().validate_source_archive(args.source_tar)
    source = json.loads(args.source_tar.with_name(args.source_tar.name + ".manifest.json").read_text())
    members = {item["member"]: item["sha256"] for item in source["members"]}
    images = {}
    for role, container in (("backend", args.backend_container), ("frontend", args.frontend_container)):
        metadata = json.loads(capture(["docker", "inspect", container]))[0]
        if not metadata["State"]["Running"]:
            raise ValueError("来源容器未运行")
        images[role] = {"reference": metadata["Config"]["Image"], "digest": metadata["Image"],
                        "container_id": metadata["Id"]}
    program = ("import hashlib,json,pathlib;"
               "r=pathlib.Path('/app/src/eic');"
               "print(json.dumps({p.relative_to(r).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() "
               "for p in r.rglob('*.py')}))")
    modules = json.loads(capture(["docker", "exec", args.backend_container, "python", "-B", "-c", program]))
    expected = {name[len("backend/src/eic/"):]: digest for name, digest in members.items()
                if name.startswith("backend/src/eic/") and name.endswith(".py")}
    if modules != expected:
        raise ValueError("实际运行模块与源代码归档不一致")
    rows = capture(["docker", "exec", args.frontend_container, "sh", "-c",
                    "cd /usr/share/nginx/html && find . -type f -exec sha256sum {} \\;"])
    assets = {}
    for line in rows.splitlines():
        digest, name = line.split(None, 1)
        assets[name[2:] if name.startswith("./") else name] = digest
    if "index.html" not in assets:
        raise ValueError("前端镜像未包含入口资产")
    build = json.loads(args.frontend_build_record.read_text(encoding="utf-8"))
    frontend_sources = {name: digest for name, digest in members.items() if name.startswith("frontend/")}
    if (build.get("exit_code") != 0 or build.get("frontend_source_members") != frontend_sources
            or build.get("output_sha256") != assets):
        raise ValueError("前端实际运行资产与本次源码构建不一致")
    images["backend"]["runtime_modules"] = modules
    images["frontend"]["assets"] = assets
    validate_image_archive(args.images_tar, images)
    # schema check 使用该运行容器的有效配置，stdout 不含数据库连接串。
    schema = capture(["docker", "exec", args.backend_container, "python", "-B", "-m", "eic.core.schema", "check"])
    record = {"source_archive_sha256": source["sha256"], "source_members": members,
              "images_archive_sha256": file_digest(args.images_tar),
              "lockfiles": {name: digest for name, digest in members.items()
                            if name.endswith(("package-lock.json", "uv.lock"))},
              "images": images, "schema_head": source_schema_head(args.source_tar), "schema_check_stdout": schema,
              "frontend_build": {"exit_code": build["exit_code"], "source_members": frontend_sources,
                                 "assets": assets, "record_sha256": file_digest(args.frontend_build_record)},
              "release_gates": json.loads(args.gates.read_text()), "registry_digest_available": False,
              "digest_kind": "docker_local_image_id_not_registry_digest"}
    if args.layout_json:
        from existing_host_release import validate_layout
        layout = json.loads(args.layout_json.read_text(encoding="utf-8"))
        validate_layout(layout, layout['root'])
        record['existing_layout_sha256'] = file_digest(args.layout_json)
        record['restore_receipt_sha256'] = file_digest(args.restore_receipt)
    if args.schema_transition_plan:
        if not args.layout_json:
            raise ValueError("schema迁移只支持经审查的既有目录布局")
        from harness.boundaries import read_json
        from schema_transition import validate_transition
        transition = read_json(args.schema_transition_plan.read_bytes())
        validate_transition(transition, record)
        record['schema_transition'] = transition
    args.output.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print("RELEASE_SOURCE_VERIFIED modules=" + str(len(modules)) + " assets=" + str(len(assets)))


if __name__ == "__main__":
    main()
