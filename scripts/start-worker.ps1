$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $root 'backend')
& "$root\backend\.venv\Scripts\arq.exe" eic.workers.WorkerSettings
if ($LASTEXITCODE -ne 0) { throw 'Worker 启动失败，请检查 Redis 与后端配置。' }
