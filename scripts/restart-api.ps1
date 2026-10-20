# Restart/pulihkan API :8000 -- mengaktifkan kode baru, atau membuang proses yatim.
#
# Kenapa ada: sama seperti `serve`, API dijalankan task `IDX-API` dalam konteks
# tersimpan (session 0), sehingga shell biasa tidak boleh menghentikannya
# ("access denied"). Task itu boleh menghentikan proses yang ia lahirkan
# sendiri, jadi restart dijalankan lewat task yang sama.
#
# Dua pemicu:
#  1. `_restart-api.flag` ada -> restart paksa (mis. setelah kode baru):
#         ni _restart-api.flag
#         Start-ScheduledTask -TaskName IDX-API
#  2. :8000 TIDAK ada yang mendengarkan -> proses API yang masih hidup pasti
#     yatim (pendengar mati), dan biasanya masih memegang `_api.log` sehingga
#     `python ... >> _api.log` di start-api.bat gagal membuka berkas dan uvicorn
#     tidak pernah menyala (senyap di bawah run-hidden.vbs). Ini jalur pemulihan
#     otomatis -- tidak perlu flag.
#
# Kalau :8000 melayani dan tidak ada flag, tidak ada yang dihentikan.
# Jejak tiap eksekusi ditulis ke _restart-api.log.
#
# Kode keluar: 0 = lanjut menyalakan API; 2 = `_api.log` masih terkunci proses
# lain, jadi start-api.bat TIDAK boleh menyalakan python (redirect-nya akan
# gagal dan API tidak pernah jalan).

$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
$flag = Join-Path $root '_restart-api.flag'
$jejak = Join-Path $root '_restart-api.log'
$log = Join-Path $root '_api.log'

function Catat([string]$pesan) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $pesan" |
        Out-File -FilePath $jejak -Append -Encoding utf8
}

$flagAda = Test-Path $flag
$melayani = $false
try {
    $melayani = [bool](Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Stop)
}
catch {
    $melayani = [bool](netstat -ano | Select-String -Pattern ':(8000)\s+\S+\s+LISTENING')
}

if (-not $flagAda -and $melayani) {
    Catat 'tidak ada _restart-api.flag dan :8000 melayani - tidak ada yang dihentikan'
    exit 0
}

if (-not $flagAda) {
    Catat ':8000 tidak melayani - buang proses API yatim yang masih memegang _api.log'
}

$daftar = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'")
foreach ($p in $daftar) {
    Catat "python PID $($p.ProcessId) parent=$($p.ParentProcessId) cmd=[$($p.CommandLine)]"
}

# Cocokkan HANYA proses API ini -- jangan sentuh `idx_scraper.cli serve`
# (namanya `cli serve`, bukan `api.serve`) atau proses python lain (job refresh,
# skrip manual) yang kebetulan hidup.
$target = @($daftar | Where-Object {
        $_.CommandLine -like '*uvicorn*idx_scraper.api.app*' -or
        $_.CommandLine -like '*idx_scraper.api.serve*'
    })
if ($target.Count -eq 0) {
    Catat 'tidak ada proses API yang hidup - lanjut menyalakan yang baru'
}
else {
    foreach ($p in $target) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Catat "dihentikan PID $($p.ProcessId)"
        }
        catch {
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
    }
    catch {
        Start-Sleep -Milliseconds 500
    }
}
Catat "berkas _api.log bebas: $bebas"

# Tunggu port benar-benar bebas. Socket proses yang baru dibunuh bisa masih
# terlihat LISTEN sesaat; tanpa jeda ini guard-port.ps1 akan menyimpulkan
# ":8000 sudah jalan" dan melewati peluncuran, sehingga API tinggal mati.
$bebasPort = $false
for ($i = 0; $i -lt 20; $i++) {
    $sisa = $false
    try {
        $sisa = [bool](Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Stop)
    }
    catch {
        $sisa = $false
    }
    if (-not $sisa) {
        $bebasPort = $true
        break
    }
    Start-Sleep -Milliseconds 250
}
Catat "port 8000 bebas: $bebasPort"

if ($flagAda) {
    Remove-Item $flag -Force -ErrorAction SilentlyContinue
    Catat 'flag _restart-api.flag dihapus'
}

if (-not $bebas) {
    Catat '_api.log masih terkunci - start-api.bat diminta TIDAK menyalakan API'
    exit 2
}

exit 0
