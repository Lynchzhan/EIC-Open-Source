#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${EIC_PORT:-8080}"
cd "$ROOT"
if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3.11+。"
  exit 1
fi
if [[ ! -x "$ROOT/backend/.venv/bin/python" ]]; then
  python3 -m venv "$ROOT/backend/.venv"
fi
if [[ "${EIC_INSTALL_DEPENDENCIES:-false}" == "true" ]]; then
  "$ROOT/backend/.venv/bin/python" -m pip install -e "$ROOT/backend"
fi
if [[ ! -f "$ROOT/frontend/dist/index.html" ]]; then
  echo "缺少 frontend/dist，请先运行：cd frontend && npm ci && npm run build"
  exit 1
fi
mkdir -p "$ROOT/data/uploads"
PASS_FILE="$ROOT/data/admin-password.txt"
if [[ ! -f "$PASS_FILE" ]]; then
  python3 -c "import secrets,pathlib; pathlib.Path('data/admin-password.txt').write_text(secrets.token_urlsafe(12)+'a1', encoding='utf-8')"
fi
PASSWORD="$(tr -d '\r\n' < "$PASS_FILE")"
"$ROOT/backend/.venv/bin/python" "$ROOT/scripts/simulation-secrets.py" --data-dir "$ROOT/data"
JWT="$("$ROOT/backend/.venv/bin/python" -c 'import sys; from pathlib import Path; print(dict(line.split("=",1) for line in Path(sys.argv[1]).read_text().splitlines())["EIC_JWT_SECRET"])' "$ROOT/data/simulation.env")"
KEY="$("$ROOT/backend/.venv/bin/python" -c 'import sys; from pathlib import Path; print(dict(line.split("=",1) for line in Path(sys.argv[1]).read_text().splitlines())["EIC_SECRET_ENCRYPTION_KEY"])' "$ROOT/data/simulation.env")"
export EIC_DATABASE_URL="sqlite+aiosqlite:///$ROOT/data/eic-sim.db"
export EIC_AUTO_CREATE_SCHEMA=false
export EIC_RUNTIME_SCHEMA_UPDATES=false
export EIC_SEED_DEMO_DATA=true
export EIC_REQUIRE_RUNTIME_DEPENDENCIES=false
export EIC_SERVE_FRONTEND=true
export EIC_FRONTEND_DIST="$ROOT/frontend/dist"
export EIC_CORS_ORIGINS="http://127.0.0.1:${PORT},http://localhost:${PORT}"
export EIC_BOOTSTRAP_ADMIN=admin
export EIC_BOOTSTRAP_ENABLED=true
export EIC_BOOTSTRAP_ADMIN_PASSWORD="$PASSWORD"
export EIC_JWT_SECRET="$JWT"
export EIC_SECRET_ENCRYPTION_KEY="$KEY"
export EIC_SKILL_ROOT="$ROOT/backend/skills"
export EIC_UPLOAD_ROOT="$ROOT/data/uploads"
export EIC_ENVIRONMENT=simulation
echo "模拟环境地址：http://127.0.0.1:${PORT}"
echo "用户名：admin / 租户：1000"
echo "密码文件：$PASS_FILE"
cd "$ROOT/backend"
"$ROOT/backend/.venv/bin/python" -m eic.core.schema upgrade
exec "$ROOT/backend/.venv/bin/python" -m uvicorn eic.main:app --host 127.0.0.1 --port "$PORT"
