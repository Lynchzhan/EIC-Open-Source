param([int]$Port = 8080, [switch]$InstallDependencies, [switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
function Find-Python {
  $bundled = Join-Path $Root 'runtime\python\python.exe'
  if (Test-Path -LiteralPath $bundled) { return $bundled }
  foreach ($name in @('py','python','python3')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
      $ver = & $cmd.Source -c "import sys; print('%s.%s' % (sys.version_info.major, sys.version_info.minor))"
      if ([version]$ver -ge [version]'3.11') { return $cmd.Source }
    } catch { continue }
  }
  throw '需要 runtime\python 中的运行环境或 Python 3.11+'
}
$python = Find-Python
$bundledPython = Join-Path $Root 'runtime\python\python.exe'
$useBundled = (Test-Path -LiteralPath $bundledPython) -and ($python -eq $bundledPython)
if (-not $useBundled) {
  $venvPython = Join-Path $Root 'backend\.venv\Scripts\python.exe'
  if (-not (Test-Path $venvPython) -and -not $InstallDependencies) {
    throw '缺少已有 backend\.venv 运行环境；仅在明确需要安装时使用 -InstallDependencies'
  }
  if ($InstallDependencies -and -not (Test-Path $venvPython)) {
    Write-Host '正在创建虚拟环境...'
    & $python -m venv (Join-Path $Root 'backend\.venv')
    if ($LASTEXITCODE -ne 0) { throw '虚拟环境创建失败' }
  }
  $python = Join-Path $Root 'backend\.venv\Scripts\python.exe'
  if ($InstallDependencies) {
    Write-Host '正在安装后端依赖...'
    & $python -m pip install -e (Join-Path $Root 'backend')
    if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败，启动已中止' }
  }
} else {
  Write-Host '使用已有便携 Python 运行环境。'
}
$dist = Join-Path $Root 'frontend\dist\index.html'
if (-not (Test-Path $dist)) { throw '缺少 frontend/dist，请先构建前端。' }
$dataDir = Join-Path $Root 'data'
New-Item -ItemType Directory -Force -Path $dataDir,(Join-Path $dataDir 'uploads') | Out-Null
$dbPath = (Join-Path $dataDir 'eic-sim.db') -replace '\\','/'
$passFile = Join-Path $dataDir 'admin-password.txt'
if (-not (Test-Path $passFile)) {
  $guid = [guid]::NewGuid().ToString('N').Substring(0,12)
  Set-Content -LiteralPath $passFile -Value ($guid + 'Aa1') -Encoding ascii
}
$password = (Get-Content -LiteralPath $passFile -Raw).Trim()
. (Join-Path $PSScriptRoot 'simulation-secrets.ps1')
$simulationSecrets = Get-SimulationSecrets -DataDir $dataDir
$env:EIC_DATABASE_URL = 'sqlite+aiosqlite:///' + $dbPath
$env:EIC_AUTO_CREATE_SCHEMA = 'false'
$env:EIC_RUNTIME_SCHEMA_UPDATES = 'false'
$env:EIC_SEED_DEMO_DATA = 'true'
$env:EIC_REQUIRE_RUNTIME_DEPENDENCIES = 'false'
$env:EIC_SERVE_FRONTEND = 'true'
$env:EIC_FRONTEND_DIST = Join-Path $Root 'frontend\dist'
$env:EIC_CORS_ORIGINS = 'http://127.0.0.1:' + $Port + ',http://localhost:' + $Port
$env:EIC_BOOTSTRAP_ADMIN = 'admin'
$env:EIC_BOOTSTRAP_ENABLED = 'true'
$env:EIC_BOOTSTRAP_ADMIN_PASSWORD = $password
$env:EIC_JWT_SECRET = $simulationSecrets['EIC_JWT_SECRET']
$env:EIC_SECRET_ENCRYPTION_KEY = $simulationSecrets['EIC_SECRET_ENCRYPTION_KEY']
$env:EIC_SKILL_ROOT = Join-Path $Root 'backend\skills'
$env:EIC_UPLOAD_ROOT = Join-Path $dataDir 'uploads'
$env:EIC_ENVIRONMENT = 'simulation'
Write-Host ('模拟环境地址: http://127.0.0.1:' + $Port)
Write-Host '用户名: admin / 租户: 1000'
Write-Host ('密码文件: ' + $passFile)
Set-Location (Join-Path $Root 'backend')
& $python -m eic.core.schema upgrade
if ($LASTEXITCODE -ne 0) { throw 'Schema 迁移失败，应用启动已中止' }
if ($OpenBrowser) { Start-Process ('http://127.0.0.1:' + $Port + '/login') }
& $python -m uvicorn eic.main:app --host 127.0.0.1 --port $Port
