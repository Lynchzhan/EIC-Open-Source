function Get-SimulationSecrets {
  param([Parameter(Mandatory=$true)][string]$DataDir)
  $configPath = Join-Path $DataDir 'simulation.env'
  $dbPath = Join-Path $DataDir 'eic-sim.db'
  if (-not (Test-Path -LiteralPath $configPath)) {
    if (Test-Path -LiteralPath $dbPath) {
      throw '已有模拟数据库但缺少 simulation.env；请先恢复原 JWT 和加密密钥，禁止自动替换'
    }
    $jwt = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    $key = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    $bytes = [Text.Encoding]::UTF8.GetBytes("EIC_JWT_SECRET=$jwt`nEIC_SECRET_ENCRYPTION_KEY=$key`n")
    $stream = [IO.File]::Open($configPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
  }
  $values = @{}
  foreach ($line in [IO.File]::ReadAllLines($configPath)) {
    if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith('#')) { continue }
    $parts = $line.Split(@('='), 2)
    if ($parts.Length -ne 2 -or $values.ContainsKey($parts[0])) { throw '模拟密钥配置格式错误或存在重复键' }
    $values[$parts[0]] = $parts[1]
  }
  foreach ($name in @('EIC_JWT_SECRET','EIC_SECRET_ENCRYPTION_KEY')) {
    if (-not $values.ContainsKey($name) -or $values[$name].Length -lt 32 -or $values[$name].StartsWith('replace-')) {
      throw '模拟密钥缺失或无效；请恢复原配置，不自动生成替代值'
    }
  }
  return $values
}
