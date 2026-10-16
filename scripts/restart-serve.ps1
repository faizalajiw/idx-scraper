# Restart paksa `idx serve` — mengaktifkan kode baru ke proses yang sedang jalan.
#
# Kenapa ada: `serve` dijalankan task `IDX-Serve` dalam konteks tersimpan
# (session 0, kredensial), sehingga shell biasa tidak boleh menghentikannya
# ("access denied"). Task itu boleh menghentikan proses yang ia lahirkan
# sendiri, jadi restart dijalankan lewat task yang sama.
#
# Cara pakai (hanya kalau `serve` perlu memuat kode baru):
#     ni _restart-serve.flag
#     Start-ScheduledTask -TaskName IDX-Serve
#
# Tanpa flag, skrip ini tidak menghentikan apa pun: `start-serve.bat` tetap
# sekadar menyalakan serve kalau belum ada (dijaga kunci satu-instance).
# Jejak tiap eksekusi ditulis ke _restart-serve.log.
#
# CATATAN: karena cmd menolak append ke _serve.log selama handle proses lama
# masih hidup, skrip ini menunggu berkas itu benar-benar bebas sebelum keluar —
# tanpa itu, baris `python ... >> _serve.log` di start-serve.bat gagal dan serve
# baru tidak pernah start.

$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
$flag = Join-Path $root '_restart-serve.flag'
$jejak = Join-Path $root '_restart-serve.log'
$log = Join-Path $root '_serve.log'

function Catat([string]$pesan) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $pesan" |
        Out-File -FilePath $jejak -Append -Encoding utf8
}

if (-not (Test-Path $flag)) {
    Catat 'tidak ada _restart-serve.flag - tidak ada yang dihentikan'
    exit 0
}

$daftar = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'")
foreach ($p in $daftar) {
    Catat "python PID $($p.ProcessId) parent=$($p.ParentProcessId) cmd=[$($p.CommandLine)]"
}

$target = @($daftar | Where-Object { $_.CommandLine -like '*idx_scraper.cli serve*' })
if ($target.Count -eq 0) {
    Catat 'tidak ada proses idx_scraper.cli serve yang hidup - lanjut menyalakan yang baru'
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
Catat "berkas _serve.log bebas: $bebas"

Remove-Item $flag -Force -ErrorAction SilentlyContinue
Catat 'flag _restart-serve.flag dihapus'
