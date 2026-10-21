# Runs the collector from this PC: Plan B only, if Google ever blocks GitHub's runners.
#
# CAREFUL: on 2026-10-21 about 170 Google Flights reads from this PC in one hour made Google show
# its "unusual traffic" CAPTCHA to every browser on the home network (GitHub's runners kept
# working). So this script refuses to run unless $env:VOLS_ALLOW_LOCAL is "1", and you should keep
# --budget small (10 or less) and the calendars to their normal schedule.
#
# Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the app's gitignored .env.local (or the
# file named by $env:VOLS_ENV_FILE), never prints them. Every argument goes to the collector:
#   $env:VOLS_ALLOW_LOCAL = "1"; .\run-local.ps1 --budget 10
$ErrorActionPreference = "Stop"
if ($env:VOLS_ALLOW_LOCAL -ne "1") {
  Write-Error "Refused: running from this PC can get the home network CAPTCHA'd by Google. Set `$env:VOLS_ALLOW_LOCAL = '1' if GitHub's runners are blocked and you accept that."
  exit 2
}
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
