# ============================================================================
# FIX: repoint postgresql-x64-18 service to the moved data dir.
# The earlier sc.exe config silently failed (quoting), so the service still
# points at the old D:\pgdata (now gone). This sets ImagePath via the registry
# directly — no sc.exe quoting hell — then starts the service.
# RUN AS ADMINISTRATOR.
# ============================================================================
$ErrorActionPreference = 'Stop'

$svc     = 'postgresql-x64-18'
$newData = 'D:\Project\idx-scraper\pgdata'
$pgctl   = 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe'
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$svc"

Write-Host "== 1/5 Pre-checks ==" -ForegroundColor Cyan
if (-not (Test-Path $newData))                    { throw "Data dir $newData not found." }
if (-not (Test-Path "$newData\PG_VERSION"))       { throw "$newData does not look like a PG data dir (no PG_VERSION)." }
if (-not (Test-Path $pgctl))                      { throw "pg_ctl not found at $pgctl." }

Write-Host "== 2/5 Ensure service is stopped ==" -ForegroundColor Cyan
if ((Get-Service $svc).Status -ne 'Stopped') { Stop-Service $svc -Force }
$sw = [Diagnostics.Stopwatch]::StartNew()
while ((Get-Service $svc).Status -ne 'Stopped') {
    if ($sw.Elapsed.TotalSeconds -gt 60) { throw "Service did not stop within 60s." }
    Start-Sleep -Milliseconds 500
}
Write-Host "   stopped."

Write-Host "== 3/5 Write ImagePath in registry ==" -ForegroundColor Cyan
$before = (Get-ItemProperty -Path $regPath -Name ImagePath).ImagePath
Write-Host "   before: $before"
# Build the exact binary path string PostgreSQL expects.
$imagePath = '"{0}" runservice -N "{1}" -D "{2}" -w' -f $pgctl, $svc, $newData
Set-ItemProperty -Path $regPath -Name ImagePath -Value $imagePath -Type ExpandString
$after = (Get-ItemProperty -Path $regPath -Name ImagePath).ImagePath
Write-Host "   after : $after"
if ($after -notlike "*$newData*") { throw "ImagePath did not update correctly." }

Write-Host "== 4/5 Start service ==" -ForegroundColor Cyan
Start-Service $svc
$sw.Restart()
while ((Get-Service $svc).Status -ne 'Running') {
    if ($sw.Elapsed.TotalSeconds -gt 60) { throw "Service did not start within 60s. Check pgdata\log\*.log" }
    Start-Sleep -Milliseconds 500
}
Write-Host "   running."

Write-Host "== 5/5 Verify data_directory ==" -ForegroundColor Cyan
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' -U postgres -d idx_scraper -t -c 'show data_directory;'

Write-Host ""
Write-Host "DONE. data_directory should now read: $newData" -ForegroundColor Green
