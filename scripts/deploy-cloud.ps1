param(
  [Parameter(Mandatory = $true)][string]$HostName,
  [Parameter(Mandatory = $true)][string]$RemoteDir,
  [Parameter(Mandatory = $true)][string]$ProjectName,
  [Parameter(Mandatory = $true)][string]$ComposeFile,
  [Parameter(Mandatory = $true)][string]$Package,
  [Parameter(Mandatory = $true)][string]$Images,
  [Parameter(Mandatory = $true)][string]$ReleaseManifest,
  [string]$HarnessGate,
  [string]$ExpectedHarnessGateSha256,
  [string]$Python = 'python',
  [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$arguments = @((Join-Path $PSScriptRoot 'release_deploy.py'), '--host', $HostName,
  '--remote-dir', $RemoteDir, '--project-name', $ProjectName, '--compose-file', $ComposeFile,
  '--source-tar', $Package, '--images-tar', $Images, '--release-manifest', $ReleaseManifest)
if ($Apply -and ([string]::IsNullOrWhiteSpace($HarnessGate) -or
    [string]::IsNullOrWhiteSpace($ExpectedHarnessGateSha256))) {
  throw 'Apply 需要 HarnessGate 和 ExpectedHarnessGateSha256。'
}
if ($HarnessGate) { $arguments += @('--harness-gate', $HarnessGate) }
if ($ExpectedHarnessGateSha256) {
  if ($ExpectedHarnessGateSha256 -cnotmatch '^[0-9a-f]{64}$') {
    throw 'ExpectedHarnessGateSha256 必须是精确的 64 位小写 SHA256。'
  }
  $arguments += @('--expected-harness-gate-sha256', $ExpectedHarnessGateSha256)
}
if ($Apply) { $arguments += '--apply' }
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw "发布步骤失败，退出码：$LASTEXITCODE；现有服务按报告核验。" }
