"""独立 Langfuse 冷备份/恢复工具。仅标准库；本交付没有执行此文件。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
REFERENCE_RELEASE = "v3.100.0"
COMMAND_TIMEOUT_SECONDS = 60
STOP_TIMEOUT_SECONDS = 600
COLD_TAR_TIMEOUT_SECONDS = 3600
VOLUMES = ("pg", "ch", "chlogs", "redis", "objects")
SERVICES = ("langfuse-web", "langfuse-worker", "postgres", "clickhouse", "redis", "minio")
DIGESTS = ("WEB_SHA256", "WORKER_SHA256", "PG_SHA256", "CH_SHA256", "REDIS_SHA256", "MINIO_SHA256", "HELPER_SHA256")
SECRETS = ("PG_PASSWORD", "CH_PASSWORD", "REDIS_PASSWORD", "S3_ACCESS_KEY", "S3_SECRET_KEY", "SALT", "ENCRYPTION_KEY", "NEXTAUTH_SECRET")
KEYS = set(DIGESTS + SECRETS + ("RUN_ID", "OBJECT_BUCKET", "WEB_PORT", "OBJECT_PORT"))


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def read_env(path):
    # 限定简单 KEY=value，拒绝引号、插值和额外配置，避免与 Compose 解释不同。
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        require(sep and key in KEYS and key not in values, "env 含未知键/重复键/无效行")
        require(re.fullmatch(r"[a-zA-Z0-9-]*", value) is not None, "env 仅允许无引号字母数字连字符")
        values[key] = value
    require(set(values) == KEYS, "env 缺少必要键")
    require(re.fullmatch(r"[a-z][a-z0-9-]{6,38}[a-z0-9]", values["RUN_ID"]) is not None, "RUN_ID 格式错误")
    require(re.fullmatch(r"lf-[a-z][a-z0-9-]{6,38}[a-z0-9]", values["OBJECT_BUCKET"]) is not None, "OBJECT_BUCKET 格式错误")
    for key in DIGESTS + SECRETS:
        value = values[key]
        require(re.fullmatch(r"[0-9a-f]{64}", value) is not None and len(set(value)) >= 8, key + " 必须是非占位的 64 位小写 hex")
    require(len({values[k] for k in SECRETS}) == len(SECRETS), "secret 不得复用")
    for key in ("WEB_PORT", "OBJECT_PORT"):
        require(values[key].isdigit() and 1024 <= int(values[key]) <= 65535, "端口范围错误")
    require(values["WEB_PORT"] != values["OBJECT_PORT"], "端口必须不同")
    if os.name != "nt":
        require(path.stat().st_mode & 0o077 == 0, "env 必须 chmod 600 或更严格")
    return values


def run(args, env=None, *, timeout=COMMAND_TIMEOUT_SECONDS):
    # 不打印命令或 stderr，防止数据库口令被 Compose 报错带出。
    try:
        result = subprocess.run(args, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # 不透传异常：其中可能含命令参数、环境相关报错或部分输出。
        raise RuntimeError("命令等待超时；Docker CLI 超时不保证 daemon 操作或 helper 已停止。保留部分产物和容器，不自动清理、重启或重试；须人工核查状态。") from None
    require(result.returncode == 0, "命令失败（输出已隐藏）；状态须人工核查，保留部分产物，不自动清理。")
    return result.stdout.strip()


def compose(values, env_path, *args):
    clean = {k: v for k, v in os.environ.items() if k not in KEYS and not k.startswith("COMPOSE_")}
    clean.update(values)
    return run(["docker", "compose", "--project-name", "lf-" + values["RUN_ID"],
                "--env-file", str(env_path), "-f", str(ROOT / "compose.yaml"), *args], clean)


def project_containers(values):
    ids = run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=lf-" + values["RUN_ID"]]).split()
    return json.loads(run(["docker", "inspect", *ids])) if ids else []


def volume_name(values, key):
    return "lf-" + values["RUN_ID"] + "-" + key


def require_owned_volumes(values):
    for key in VOLUMES:
        info = json.loads(run(["docker", "volume", "inspect", volume_name(values, key)]))[0]
        labels = info.get("Labels") or {}
        require(labels.get("com.docker.compose.project") == "lf-" + values["RUN_ID"]
                and labels.get("com.docker.compose.volume") == key, "卷所有权不符: " + key)


def require_stopped(values):
    for container in project_containers(values):
        state = container["State"]
        require(not state["Running"] and not state.get("Restarting") and not state.get("Paused"), "仍有运行/重启/暂停容器")
        require(not state.get("OOMKilled") and state.get("ExitCode") == 0, "发现非干净退出，禁止生成一致性备份")
    for key in VOLUMES:
        require(not run(["docker", "ps", "-q", "--filter", "volume=" + volume_name(values, key)]), "卷被其他运行容器挂载")


def require_target(values, env_path):
    containers = project_containers(values)
    require(len(containers) == 6 and {c["Config"]["Labels"].get("com.docker.compose.service") for c in containers} == set(SERVICES), "必须已有且仅有六项服务")
    expected = json.loads(compose(values, env_path, "config", "--format", "json"))["services"]
    for container in containers:
        labels = container["Config"]["Labels"]
        require(labels.get("com.docker.compose.project") == "lf-" + values["RUN_ID"], "容器项目身份不符")
        service = labels["com.docker.compose.service"]
        require(container["Config"]["Image"] == expected[service]["image"], "实际容器镜像与配置不符")
        actual_env = dict(item.split("=", 1) for item in container["Config"].get("Env", []) if "=" in item)
        require(all(actual_env.get(k) == str(v) for k, v in expected[service].get("environment", {}).items()), "实际容器环境与配置不符")
        mounts = container.get("Mounts", [])
        require(all(m["Type"] == "volume" for m in mounts), "容器存在非模板卷类型挂载")
        actual_mounts = {m["Destination"]: m.get("Name") for m in mounts}
        expected_mounts = {m["target"]: volume_name(values, m["source"]) for m in expected[service].get("volumes", [])}
        require(len(mounts) == len(expected_mounts) and actual_mounts == expected_mounts, "实际容器卷与配置不符")
    require_owned_volumes(values)
    ids = {c["Id"] for c in containers}
    for key in VOLUMES:
        users = set(run(["docker", "ps", "-q", "--no-trunc", "--filter", "volume=" + volume_name(values, key)]).split())
        require(users <= ids, "目标卷被非目标运行容器使用")
    return containers


def stop(values, env_path):
    containers = require_target(values, env_path)
    ids = {c["Config"]["Labels"]["com.docker.compose.service"]: c["Id"] for c in containers}
    # 只停止已核对的精确 ID；不用项目名重新选择可能被替换的对象。
    for services in (("langfuse-web",), ("langfuse-worker",), ("postgres", "clickhouse", "redis", "minio")):
        current = require_target(values, env_path)
        require({c["Config"]["Labels"]["com.docker.compose.service"]: c["Id"] for c in current} == ids, "停止期间容器身份变化，拒绝继续")
        run(["docker", "container", "stop", "--time", "120", *[ids[s] for s in services]], timeout=STOP_TIMEOUT_SECONDS)
    require_stopped(values)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def helper(values, folder, key, restore=False):
    helper_name = "lf-" + values["RUN_ID"] + "-cold-" + key + "-" + uuid.uuid4().hex[:16]
    print("冷归档 helper（保留容器供人工核查）: " + helper_name, flush=True)
    command = ["docker", "run", "--name", helper_name, "--pull=never", "--network=none", "--read-only",
               "--mount", "type=volume,src=" + volume_name(values, key) + ",dst=/volume" + ("" if restore else ",readonly"),
               "--mount", "type=bind,src=" + str(folder) + ",dst=/backup" + (",readonly" if restore else ""),
               "docker.io/library/alpine@sha256:" + values["HELPER_SHA256"], "tar"]
    command += (["xpf", "/backup/" + key + ".tar", "-C", "/volume"] if restore
                else ["cpf", "/backup/" + key + ".tar", "-C", "/volume", "."])
    run(command, timeout=COLD_TAR_TIMEOUT_SECONDS)


def backup(values, env_path, folder):
    containers = require_target(values, env_path)
    folder.mkdir(mode=0o700, parents=False, exist_ok=False)
    # helper 必须预先拉取；备份操作本身不联网拉镜像。
    run(["docker", "image", "inspect", "docker.io/library/alpine@sha256:" + values["HELPER_SHA256"]])
    stop(values, env_path)
    shutil.copyfile(env_path, folder / "secrets.env")
    os.chmod(folder / "secrets.env", 0o600)
    shutil.copyfile(ROOT / "compose.yaml", folder / "compose.yaml")
    shutil.copyfile(ROOT / "ops.py", folder / "ops.py")
    for key in VOLUMES:
        require_stopped(values)
        helper(values, folder, key)
    require_stopped(values)
    names = [k + ".tar" for k in VOLUMES] + ["secrets.env", "compose.yaml", "ops.py"]
    manifest = {"format": 1, "reference_release": REFERENCE_RELEASE,
                "utc": datetime.now(timezone.utc).isoformat(), "run_id": values["RUN_ID"],
                "bucket": values["OBJECT_BUCKET"], "method": "all-services-cleanly-stopped-cold-volumes",
                "images": {k: values[k] for k in DIGESTS},
                "container_image_ids": {c["Config"]["Labels"]["com.docker.compose.service"]: c["Image"] for c in containers},
                "platform": run(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"]),
                "sha256": {name: sha(folder / name) for name in names}}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("冷备份归档已生成；全部服务保持停止。manifest.json 需另存可信校验值；尚未恢复演练。")


def restore(values, env_path, folder):
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    require(manifest.get("reference_release") == REFERENCE_RELEASE, "备份参考发行版不符；禁止将 v4 数据当作 v3 恢复")
    names = {k + ".tar" for k in VOLUMES} | {"secrets.env", "compose.yaml", "ops.py"}
    require(manifest.get("format") == 1 and set(manifest.get("sha256", {})) == names, "备份清单格式不符")
    for name in names:
        file = folder / name
        require(file.is_file() and not file.is_symlink() and sha(file) == manifest["sha256"][name], "备份校验失败: " + name)
    source = read_env(folder / "secrets.env")
    require(source["RUN_ID"] == manifest["run_id"] and source["OBJECT_BUCKET"] == manifest["bucket"], "备份身份不符")
    require(values["RUN_ID"] != source["RUN_ID"], "恢复必须使用新的 RUN_ID")
    require(values["OBJECT_BUCKET"] == source["OBJECT_BUCKET"], "恢复必须保留原桶名")
    require(all(values[k] == source[k] for k in SECRETS + DIGESTS), "恢复不得更换 secret 或镜像 digest")
    require(manifest["images"] == {k: values[k] for k in DIGESTS}, "镜像清单不符")
    require(sha(ROOT / "compose.yaml") == sha(folder / "compose.yaml")
            and sha(ROOT / "ops.py") == sha(folder / "ops.py"), "工具/Compose 与备份不一致，需审核迁移")
    require(manifest["platform"] == run(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"]), "物理恢复要求相同平台")
    require(not project_containers(values), "恢复项目已有容器，拒绝覆盖")
    existing = set(run(["docker", "volume", "ls", "-q"]).splitlines())
    require(all(volume_name(values, k) not in existing for k in VOLUMES), "恢复卷必须全部不存在")
    run(["docker", "image", "inspect", "docker.io/library/alpine@sha256:" + values["HELPER_SHA256"]])
    restore_token = uuid.uuid4().hex
    for key in VOLUMES:
        require(not project_containers(values), "恢复期间目标出现容器，拒绝继续")
        existing = set(run(["docker", "volume", "ls", "-q"]).splitlines())
        require(volume_name(values, key) not in existing, "恢复期间出现同名卷，拒绝复用")
        run(["docker", "volume", "create", "--driver", "local", "--label", "com.docker.compose.project=lf-" + values["RUN_ID"],
             "--label", "com.docker.compose.volume=" + key,
             "--label", "eic.langfuse.restore-token=" + restore_token, volume_name(values, key)])
        info = json.loads(run(["docker", "volume", "inspect", volume_name(values, key)]))[0]
        labels = info.get("Labels") or {}
        require(labels.get("eic.langfuse.restore-token") == restore_token
                and labels.get("com.docker.compose.project") == "lf-" + values["RUN_ID"]
                and labels.get("com.docker.compose.volume") == key
                and info.get("Driver") == "local" and not info.get("Options"), "恢复卷不是本次新建的默认本地卷")
        require(not run(["docker", "ps", "-q", "--filter", "volume=" + volume_name(values, key)]), "恢复新卷被运行容器占用")
        # 仅处理已由运维验证可信来源的原始 tar；SHA256 不替代来源认证。
        helper(values, folder, key, restore=True)
    print("五个卷已恢复到全新 RUN_ID；没有启动任何服务。需人工隔离验收后决定是否启用。")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "backup", "restore", "stop"))
    parser.add_argument("--env", required=True, type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--writers-paused", action="store_true", help="确认所有生产者/直连对象写入及定时任务已暂停")
    args = parser.parse_args()
    require(os.name == "posix", "运维操作仅支持 Linux 本机 Docker Engine；Windows 本轮只交付源码")
    require(not os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"), "禁止环境指定远端 Docker")
    require(run(["docker", "context", "show"]) == "default", "仅支持本机 default Docker context")
    endpoint = run(["docker", "context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"])
    require(endpoint == "unix:///var/run/docker.sock", "仅支持本机标准 Unix Docker socket")
    path = args.env.resolve(strict=True)
    values = read_env(path)
    require(ROOT / "compose.yaml" != path, "env 路径错误")
    if args.action == "preflight":
        compose(values, path, "config", "--quiet")
        print("仅配置预检完成；不证明镜像来源、健康、兼容性或可恢复性。")
        return
    if args.action in ("backup", "stop"):
        require(args.writers_paused, "必须先暂停全部写入者，再传 --writers-paused")
    if args.action == "stop":
        stop(values, path)
        print("独立 Langfuse 服务已停止；卷保留。EIC 导出开关须由主另行关闭。")
    else:
        require(args.directory is not None, "backup/restore 必须指定 --directory")
        folder = args.directory.resolve()
        require("," not in str(folder), "归档路径不得含逗号")
        if args.action == "backup":
            backup(values, path, folder)
        else:
            restore(values, path, folder)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("失败: " + str(exc) + "；不自动重启/删除/回滚任何资源。", file=sys.stderr)
        sys.exit(1)
