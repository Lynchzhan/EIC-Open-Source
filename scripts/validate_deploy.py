"""Static deployment-package validation used when Docker is unavailable."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
required = [
    ROOT / "backend" / "Dockerfile",
    ROOT / "frontend" / "Dockerfile",
    ROOT / "deploy" / "docker-compose.yml",
    ROOT / "deploy" / "docker-compose.prod.yml",
    ROOT / "deploy" / "nginx.conf",
    ROOT / "deploy" / "nginx.prod.conf",
    ROOT / "deploy" / "env.example",
    ROOT / "scripts" / "start-linux.sh",
]
missing = [str(path) for path in required if not path.is_file()]
if missing:
    raise SystemExit("missing deployment files: " + ", ".join(missing))

for compose_path in (ROOT / "deploy" / "docker-compose.yml", ROOT / "deploy" / "docker-compose.prod.yml"):
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = document.get("services", {})
    expected = {"mysql", "postgres", "redis", "backend", "worker", "frontend"}
    if not expected <= set(services):
        raise SystemExit(f"{compose_path}: missing services {expected - set(services)}")
    if compose_path.name.endswith(".prod.yml") and "nginx" not in services:
        raise SystemExit(f"{compose_path}: production nginx is missing")

print(f"deployment package valid: {len(required)} required files; compose files parsed")
