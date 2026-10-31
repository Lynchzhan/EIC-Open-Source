"""在干净副本和独立进程中运行所有后端测试，不读取当前运行数据。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
# 在 -I 启动时也先绑定当前源码的发布排除规则。
from release_policy import excluded_source_path, public_source_sha256  # noqa: E402


def copy_public_source(destination: Path) -> None:
    for directory, directories, names in os.walk(ROOT, followlinks=False):
        base = Path(directory)
        directories[:] = sorted(name for name in directories
            if not excluded_source_path((base / name).relative_to(ROOT).as_posix()))
        for name in [*directories, *names]:
            path = base / name
            relative = path.relative_to(ROOT)
            if excluded_source_path(relative.as_posix()):
                continue
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError(f"测试源码含链接：{relative}")
            if path.is_file():
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)


def test_environment() -> dict[str, str]:
    # 删除继承的应用配置，避免镜像或开发者的生产配置污染合成测试。
    environment = {
        name: value for name, value in os.environ.items()
        if not name.upper().startswith(("EIC_", "LANGFUSE_", "OTEL_"))
    }
    environment.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "EIC_ENVIRONMENT": "testing",
        "EIC_BOOTSTRAP_ENABLED": "true",
        "EIC_SEED_DEMO_DATA": "false",
        "EIC_AUTO_CREATE_SCHEMA": "false",
        "EIC_REQUIRE_RUNTIME_DEPENDENCIES": "false",
        "EIC_TASK_EXECUTION_ENABLED": "true",
        "EIC_OUTBOX_DISPATCH_ENABLED": "false",
        "EIC_GENERATION_RECOVERY_ENABLED": "false",
        "EIC_LANGFUSE_ENABLED": "false",
        "OTEL_SDK_DISABLED": "true",
        "EIC_SERVE_FRONTEND": "false",
        "EIC_UPLOAD_ROOT": "/tmp/eic-isolated-uploads",
        "EIC_JWT_SECRET": "isolated-test-only-jwt-secret-at-least-32-characters",
        "EIC_SECRET_ENCRYPTION_KEY": "isolated-test-only-encryption-key-at-least-32-characters",
        "EIC_BOOTSTRAP_ADMIN_PASSWORD": "TestOnly-Admin-Password-2026-Long",
    })
    return environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    modules = sorted(path.name for path in (ROOT / "backend/tests").glob("test_*.py"))
    modules += ["migration_checks/" + path.name for path in sorted((ROOT / "backend/migration_checks").glob("test_*.py"))]
    if args.module:
        unknown = set(args.module) - set(modules)
        if unknown:
            parser.error(f"未知测试模块：{sorted(unknown)}")
        modules = [name for name in modules if name in args.module]
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"modules": [], "all_modules": not args.module, "exitCode": 1,
              "sourceSha256": public_source_sha256(ROOT)}
    for name in modules:
        print(f"开始独立验证：{name}", flush=True)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="eic-isolated-") as directory:
            source = Path(directory) / "source"
            copy_public_source(source)
            environment = test_environment()
            # 配置缓存与 sqlite engine 只允许绑定本模块的合成库。
            environment["EIC_DATABASE_URL"] = (
                "sqlite+aiosqlite:///" + (source / "backend/tests/test_eic.db").as_posix()
            )
            environment["EIC_UPLOAD_ROOT"] = str(Path(directory) / "uploads")
            environment["EIC_EXPERIENCE_ROOT"] = str(Path(directory) / "experience")
            environment["EIC_EXPERIENCE_CONTENT_ROOT"] = str(Path(directory) / "experience-content")
            # Alembic/schema 子进程不继承 pytest 的 pythonpath 配置。
            guard_root = source / "scripts/harness/offline_runtime"
            environment["C3_GUARD_ROOT"] = str(guard_root)
            environment["EIC_CANDIDATE_NETWORK_LOG"] = str(args.output.resolve() / (name.replace("/", "_") + ".network.jsonl"))
            environment["PYTHONPATH"] = os.pathsep.join([str(guard_root), str(source / "backend/src"),
                str(source / "backend/tests"), str(source / "scripts")])
            environment["EIC_TEST_ARTIFACT_DIR"] = str(args.output.resolve())
            command = [
                sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                name if name.startswith("migration_checks/") else f"tests/{name}",
            ]
            log = args.output / (name.replace("/", "_").removesuffix(".py") + ".log")
            migration_log = args.output / (name.replace("/", "_").removesuffix(".py") + ".migration.log")
            try:
                with migration_log.open("xb") as stream:
                    migration = subprocess.run([sys.executable, "-B", "-m", "eic.core.schema", "upgrade"],
                        cwd=source / "backend", env=environment, stdout=stream,
                        stderr=subprocess.STDOUT, timeout=60)
                with log.open("wb") as stream:
                    if migration.returncode:
                        stream.write(b"ISOLATED_SCHEMA_MIGRATION_FAILED\n")
                        exit_code = migration.returncode
                    else:
                        result = subprocess.run(
                            command, cwd=source / "backend", env=environment,
                            stdout=stream, stderr=subprocess.STDOUT, timeout=args.timeout,
                        )
                        exit_code = result.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
                with log.open("ab") as stream:
                    stream.write(b"\nISOLATED_TEST_PROCESS_TIMEOUT\n")
            record = {
                "module": name, "command": command, "exitCode": exit_code,
                "seconds": round(time.monotonic() - started, 3), "log": log.name,
            }
            report["modules"].append(record)
            print(f"验证结束：{name}，exitCode={exit_code}", flush=True)
        (args.output / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    report["sourceUnchangedDuringRun"] = public_source_sha256(ROOT) == report["sourceSha256"]
    report["exitCode"] = int(not report["sourceUnchangedDuringRun"] or any(row["exitCode"] != 0 for row in report["modules"]))
    (args.output / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report["exitCode"]


if __name__ == "__main__":
    raise SystemExit(main())
