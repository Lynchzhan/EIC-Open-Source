$ErrorActionPreference = 'Stop'
$health = Invoke-RestMethod 'http://127.0.0.1:8000/health'
$ready = Invoke-RestMethod 'http://127.0.0.1:8000/api/Health/Ready'
if ($health.status -ne 'ok' -or $ready.status -ne 'ready') { throw "Enterprise Intelligence Center is not ready" }
Write-Output ($ready | ConvertTo-Json -Compress)
