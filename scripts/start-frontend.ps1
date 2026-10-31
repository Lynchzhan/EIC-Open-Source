param([string]$ApiUrl = 'http://127.0.0.1:8080')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:EIC_DEV_API_URL = $ApiUrl
Set-Location (Join-Path $root 'frontend')
& npm.cmd run dev -- --host 127.0.0.1 --port 5174
if ($LASTEXITCODE -ne 0) { throw '前端启动失败，请检查依赖和端口占用。' }
