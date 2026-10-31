from __future__ import annotations

import hashlib
import json
import secrets
import time
import tarfile
from pathlib import Path

try:
    import paramiko
except ImportError:
    paramiko = None
from release_policy import excluded_source_path



def connect(host, port, user, password=None, *, identity_file=None, known_hosts=None):
    if paramiko is None:
        raise RuntimeError("缺少既有 SSH 运行依赖 paramiko；预览和来源核验仍可执行")
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if known_hosts is not None:
        client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    if identity_file is not None and password:
        raise ValueError("SSH 密钥与密码模式不得混用")
    if identity_file is not None and not Path(identity_file).is_file():
        raise ValueError("SSH 私钥文件不存在")
    if identity_file is None and not password:
        raise ValueError("必须明确提供 SSH 密钥或密码")
    client.connect(
        hostname=host,
        port=port,
        username=user,
        password=password,
        key_filename=str(identity_file) if identity_file is not None else None,
        timeout=30,
        banner_timeout=30,
        auth_timeout=30,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def run(client, command, timeout=120):
    print("REMOTE:", command.splitlines()[0][:120], flush=True)
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if out.strip():
        print(out.rstrip(), flush=True)
    if err.strip():
        print("STDERR:", err[-4000:], flush=True)
    if code != 0:
        raise SystemExit("remote command failed exit=%s" % code)
    return out


def sftp_put(client, local, remote):
    print("UPLOAD %s -> %s (%s bytes)" % (local, remote, local.stat().st_size), flush=True)
    sftp = client.open_sftp()
    last = [0.0]

    def cb(transferred, total):
        now = time.time()
        if now - last[0] >= 3 or transferred == total:
            last[0] = now
            pct = (transferred / total * 100) if total else 0
            print("  %s/%s (%.1f%%)" % (transferred, total, pct), flush=True)

    sftp.put(str(local), remote, callback=cb)
    sftp.close()


def prepare_environment(existing: str | None, host: str, *, bootstrap_new: bool = False) -> tuple[str, str | None]:
    """Reuse effective secrets on upgrades; generation requires an explicit empty-environment bootstrap."""
    if existing is not None:
        values = {}
        for line in existing.splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            if key in values:
                raise ValueError("Duplicate environment keys require manual review")
            values[key] = value.strip().strip("\"'")
        for key in ("EIC_JWT_SECRET", "EIC_SECRET_ENCRYPTION_KEY"):
            value = values.get(key, "")
            if len(value) < 32 or value.startswith(("replace-", "${", "unused")):
                raise ValueError("Existing secrets are missing or unresolved; stop rather than generate replacements")
        preserved = [
            line for line in existing.splitlines()
            if line.strip().split("=", 1)[0].strip() != "EIC_SEED_DEMO_DATA"
        ]
        preserved.append("EIC_SEED_DEMO_DATA=false")
        return "\n".join(preserved) + "\n", None
    if not bootstrap_new:
        raise ValueError("No existing deployment environment; explicit empty-environment bootstrap required")
    admin_password = secrets.token_urlsafe(24) + "a1"
    body = "\n".join([
        "MYSQL_PASSWORD=unused-lite", "MYSQL_ROOT_PASSWORD=unused-lite", "POSTGRES_PASSWORD=unused-lite",
        "EIC_JWT_SECRET=" + secrets.token_hex(32),
        "EIC_SECRET_ENCRYPTION_KEY=" + secrets.token_hex(32),
        "EIC_BOOTSTRAP_ADMIN=admin", "EIC_BOOTSTRAP_ADMIN_PASSWORD=" + admin_password,
        "EIC_SEED_DEMO_DATA=false", "EIC_REQUIRE_RUNTIME_DEPENDENCIES=false",
        "EIC_CORS_ORIGINS=http://%s,http://127.0.0.1,http://localhost" % host,
        "EIC_UPLOAD_ROOT=/app/uploads", "",
    ])
    return body, admin_password


def validate_source_archive(path: Path) -> None:
    manifest = json.loads(path.with_name(path.name + ".manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("Source archive fingerprint mismatch")
    expected = {record["member"]: record["sha256"] for record in manifest["members"]}
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if len(expected) != len(manifest["members"]) or len(members) != len(expected):
            raise ValueError("Archive member inventory mismatch")
        seen = set()
        for member in members:
            from pathlib import PurePosixPath

            name = PurePosixPath(member.name)
            if not member.isfile() or name.is_absolute() or ".." in name.parts or "\\" in member.name or member.name in seen:
                raise ValueError("Unsafe source archive member")
            seen.add(member.name)
            if member.name not in expected or excluded_source_path(member.name):
                raise ValueError("Unexpected source archive member")
            source = archive.extractfile(member)
            content = source.read() if source is not None else None
            if content is None or hashlib.sha256(content).hexdigest() != expected[member.name]:
                raise ValueError("Source member fingerprint mismatch")
            from source_secrets import require_public_content
            require_public_content(member.name, content)



if __name__ == "__main__":
    from release_deploy import main
    main()
