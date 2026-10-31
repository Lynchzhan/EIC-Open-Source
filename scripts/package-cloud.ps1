param(
    [string]$Output = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) ('.packages\Enterprise-Intelligence-Center-cloud-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.tar.gz')),
    [string]$Python = 'python',
    [string]$PackageRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
# 两个入口使用同一份全层级排除与归档成员校验，不创建临时目录或删除旧产物。
$packager = Join-Path $PSScriptRoot 'package-cloud.py'
& $Python $packager --root $PackageRoot --output $Output
if ($LASTEXITCODE -ne 0) { throw "Source packaging failed with exit code $LASTEXITCODE" }
