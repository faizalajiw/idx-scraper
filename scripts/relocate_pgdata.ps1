# ============================================================================
# Relocate PostgreSQL 18 data directory: D:\pgdata -> D:\Project\idx-scraper\pgdata
# RUN AS ADMINISTRATOR (right-click PowerShell -> Run as administrator).
# ============================================================================
$ErrorActionPreference = 'Stop'

$svc     = 'postgresql-x64-18'
$oldData = 'D:\pgdata'
$newData = 'D:\Project\idx-scraper\pgdata'
$pgctl   = 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe'
$acct    = 'NT AUTHORITY\NetworkService'

Write-Host "== 1/7 Pre-checks ==" -ForegroundColor Cyan
if (-not (Test-Path $oldData)) { throw "Source $oldData not found." }
if (Test-Path $newData)        { throw "Target $newData already exists. Remove it first." }
if (-not (Test-Path $pgctl))   { throw "pg_ctl not found at $pgctl." }

Write-Host "== 2/7 Stop service $svc ==" -ForegroundColor Cyan
Stop-Service $svc -Force
# Wait until fully stopped
$sw = [Diagnostics.Stopwatch]::StartNew()
while ((Get-Service $svc).Status -ne 'Stopped') {
    if ($sw.Elapsed.TotalSeconds -gt 60) { throw "Service did not stop within 60s." }
    Start-Sleep -Milliseconds 500
}
Write-Host "   stopped."

Write-Host "== 3/7 Move data dir ==" -ForegroundColor Cyan
# Move-Item preserves ACLs and is atomic within the same volume (D: -> D:).
Move-Item -Path $oldData -Destination $newData
Write-Host "   moved $oldData -> $newData"

Write-Host "== 4/7 Grant $acct full control on new path ==" -ForegroundColor Cyan
$acl  = Get-Acl $newData
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $acct, 'FullControl',
    'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.SetAccessRule($rule)
Set-Acl -Path $newData -AclObject $acl
Write-Host "   ACL set."

Write-Host "== 5/7 Repoint service -D flag ==" -ForegroundColor Cyan
# Rebuild the service binPath with the new data directory.
$newBin = '"{0}" runservice -N "{1}" -D "{2}" -w' -f $pgctl, $svc, $newData
# sc.exe requires a space after binPath=
& sc.exe config $svc binPath= $newBin | Out-Null
Write-Host "   binPath -> $newBin"

Write-Host "== 6/7 Start service ==" -ForegroundColor Cyan
Start-Service $svc
$sw.Restart()
while ((Get-Service $svc).Status -ne 'Running') {
    if ($sw.Elapsed.TotalSeconds -gt 60) { throw "Service did not start within 60s." }
    Start-Sleep -Milliseconds 500
}
Write-Host "   running."

Write-Host "== 7/7 Verify data_directory via psql ==" -ForegroundColor Cyan
$env:PGPASSWORD = $null  # rely on trust/peer or prompt if configured
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' -U postgres -d idx_scraper -t -c 'show data_directory;'

Write-Host ""
Write-Host "DONE. New data_directory should read: $newData" -ForegroundColor Green
Write-Host "Old path $oldData has been MOVED (no copy left behind)." -ForegroundColor Green
