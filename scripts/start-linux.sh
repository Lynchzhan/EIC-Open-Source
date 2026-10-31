#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/backend"
if [[ ! -x .venv/bin/python || ! -f .env ]]; then
  echo "缺少虚拟环境或 backend/.env，请先阅读 docs/GETTING_STARTED.md。" >&2
  exit 1
fi
.venv/bin/python -m eic.core.schema upgrade
exec .venv/bin/python -m uvicorn eic.main:app --host 127.0.0.1 --port "${EIC_PORT:-8080}"
