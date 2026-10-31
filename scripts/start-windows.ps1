param([int]$Port = 8080)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
  throw '缺少后端虚拟环境，请先按 docs/GETTING_STARTED.md 安装依赖。'
}
if (-not (Test-Path -LiteralPath (Join-Path $root 'backend\.env'))) {
  throw '缺少 backend/.env，请先运行 python scripts/init-env.py backend。'
}
Set-Location (Join-Path $root 'backend')
& $python -m eic.core.schema upgrade
if ($LASTEXITCODE -ne 0) { throw '数据库迁移失败，启动已中止。' }
Write-Host ('开发 API 地址：http://127.0.0.1:' + $Port)
Write-Host '前端请另开终端运行 scripts/start-frontend.ps1。'
& $python -m uvicorn eic.main:app --host 127.0.0.1 --port $Port --reload
