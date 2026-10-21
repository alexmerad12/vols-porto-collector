# Runs the collector from this PC (Plan B if Google ever blocks GitHub's runners, and the day-1
# fill). Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the app's gitignored .env.local
# (or the file named by $env:VOLS_ENV_FILE), never prints them. Every argument goes to the
# collector:
#   .\run-local.ps1 --budget 100 --force-calendar
$ErrorActionPreference = "Stop"
$envFile = if ($env:VOLS_ENV_FILE) { $env:VOLS_ENV_FILE } else { Join-Path $PSScriptRoot "..\vols-porto\.env.local" }
foreach ($line in Get-Content $envFile) {
  if ($line -match '^(SUPABASE_URL|SUPABASE_SERVICE_ROLE_KEY)=(.*)$') {
    Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2].Trim().Trim('"')
  }
}
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
Push-Location $PSScriptRoot
try {
  & $python -m collector.collect --host local --trigger manual @args
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
