<#
Pre-flight profil Chrome IDX sebelum `idx serve` dijalankan.

Chrome menolak membuka --user-data-dir yang masih dipegang proses lain
("Opening in existing browser session"). Sisa chrome dari serve yang sudah
mati tidak melepas profil itu, sehingga fetch TradingSummary gagal dan EOD
harian masuk 0 baris.

Aturan keselamatan:
  - Hanya chrome dengan --user-data-dir = profil IDX yang disentuh.
  - Hanya chrome YATIM (proses induknya sudah tidak hidup) yang dimatikan.
  - Chrome sehat (induknya masih hidup) TIDAK disentuh.
  - Berkas Singleton* hanya dihapus bila pemegang lock-nya sudah mati.

Aman dijalankan berulang. Parameter -ReportOnly hanya melapor, tanpa mengubah apa pun.
#>
param(
    [switch]$ReportOnly
)

$ErrorActionPreference = 'Stop'

# Root idx-scraper = satu level di atas folder scripts.
$root    = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env'

# 1. Tentukan profil. Urutan sama dengan cf_transport: env var yang sudah di-set
#    (dipanggil dari .bat yang menimpanya) menang, lalu .env, lalu default.
$profileDir = $env:IDX_CHROME_PROFILE_DIR
if (-not $profileDir -and (Test-Path $envFile)) {
    $m = Select-String -Path $envFile -Pattern '^\s*IDX_CHROME_PROFILE_DIR\s*=\s*(.+?)\s*$' |
         Select-Object -First 1
    if ($m) { $profileDir = $m.Matches[0].Groups[1].Value.Trim('"') }
}
if (-not $profileDir) {
    $profileDir = Join-Path $env:LOCALAPPDATA 'idx-scraper-chrome-profile'
}
$profileDir = [Environment]::ExpandEnvironmentVariables($profileDir)

if (-not (Test-Path $profileDir)) {
    Write-Host "[preflight] profil belum ada: $profileDir (tidak ada yang dibersihkan)"
    exit 0
}

# 2. Snapshot proses hidup.
$all   = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
$alive = @{}
foreach ($p in $all) { $alive[[int]$p.ProcessId] = $true }

# 3. Chrome yang memakai profil IDX ini.
$needle = "--user-data-dir=$profileDir"
$mine = @($all | Where-Object {
    $_.Name -eq 'chrome.exe' -and $_.CommandLine -and
    $_.CommandLine.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0
})

$orphans = @()
$healthy = @()
foreach ($c in $mine) {
    $ppid = [int]$c.ParentProcessId
    if ($ppid -gt 0 -and $alive.ContainsKey($ppid)) { $healthy += $c } else { $orphans += $c }
}

# 4. Matikan yatim saja.
foreach ($c in $orphans) {
    if ($ReportOnly) {
        Write-Host "[preflight] (report) akan mematikan chrome yatim pid=$($c.ProcessId)"
    } else {
        Write-Host "[preflight] matikan chrome yatim pid=$($c.ProcessId)"
        Stop-Process -Id $c.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

# 5. Lock basi. Di Windows Chrome memakai "lockfile" (Unix memakai
#    SingletonLock/Cookie/Socket). Dihapus hanya bila tidak ada chrome sehat
#    yang memegang profil ini - kalau masih ada, jangan sentuh apa pun.
$removed = 0
if ($healthy.Count -gt 0) {
    Write-Host "[preflight] lock dibiarkan: ada chrome sehat ($($healthy.Count)) yang memegang profil ini"
} else {
    foreach ($f in 'lockfile', 'SingletonLock', 'SingletonCookie', 'SingletonSocket') {
        $path = Join-Path $profileDir $f
        if (Test-Path -LiteralPath $path) {
            if ($ReportOnly) {
                Write-Host "[preflight] (report) akan menghapus $f"
            } else {
                Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            }
            $removed++
        }
    }
}

Write-Host ("[preflight] profil={0} chrome-profil={1} sehat={2} yatim={3} singleton={4}{5}" -f `
    $profileDir, $mine.Count, $healthy.Count, $orphans.Count, $removed, $(if ($ReportOnly) { ' (report-only)' } else { '' }))
exit 0
