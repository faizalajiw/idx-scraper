# Restart paksa API `uvicorn :8000` -- mengaktifkan kode baru ke proses yang sedang jalan.
#
# Kenapa ada: sama seperti `serve`, API dijalankan task `IDX-API` dalam konteks
# tersimpan (session 0), sehingga shell biasa tidak boleh menghentikannya
# ("access denied"). Task itu boleh menghentikan proses yang ia lahirkan
# sendiri, jadi restart dijalankan lewat task yang sama.
#
# Cara pakai (hanya kalau API perlu memuat kode baru, mis. endpoint baru):
#     ni _restart-api.flag
#     Start-ScheduledTask -TaskName IDX-API
#
# Tanpa flag, skrip ini tidak menghentikan apa pun: `start-api.bat` tetap
# sekadar menyalakan API kalau :8000 belum dipakai (dijaga guard-port.ps1).
# Jejak tiap eksekusi ditulis ke _restart-api.log.
#
# CATATAN: karena cmd menolak append ke _api.log selama handle proses lama
# masih hidup, skrip ini menunggu berkas itu benar-benar bebas sebelum keluar --
# tanpa itu baris `python -m uvicorn ... >> _api.log` di start-api.bat gagal dan
# API baru tidak pernah start.

$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
$flag = Join-Path $root '_restart-api.flag'
$jejak = Join-Path $root '_restart-api.log'
$log = Join-Path $root '_api.log'

function Catat([string]$pesan) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $pesan" |
        Out-File -FilePath $jejak -Append -Encoding utf8
}

if (-not (Test-Path $flag)) {
    Catat 'tidak ada _restart-api.flag - tidak ada yang dihentikan'
    exit 0
}

$daftar = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'")
foreach ($p in $daftar) {
    Catat "python PID $($p.ProcessId) parent=$($p.ParentProcessId) cmd=[$($p.CommandLine)]"
}

# Cocokkan HANYA proses uvicorn API ini -- jangan sentuh `idx_scraper.cli serve`
# atau proses python lain (job refresh, skrip manual) yang kebetulan hidup.
$target = @($daftar | Where-Object { $_.CommandLine -like '*uvicorn*idx_scraper.api.app*' })
if ($target.Count -eq 0) {
    Catat 'tidak ada proses uvicorn idx_scraper.api.app yang hidup - lanjut menyalakan yang baru'
} else {
    foreach ($p in $target) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Catat "dihentikan PID $($p.ProcessId)"
        } catch {
            Catat "GAGAL menghentikan PID $($p.ProcessId): $($_.Exception.Message)"
        }
    }
}

$bebas = $false
for ($i = 0; $i -lt 40; $i++) {
    try {
        $fs = [System.IO.File]::Open(
            $log,
            [System.IO.FileMode]::Append,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $fs.Close()
        $bebas = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
Catat "berkas _api.log bebas: $bebas"

Remove-Item $flag -Force -ErrorAction SilentlyContinue
Catat 'flag _restart-api.flag dihapus'