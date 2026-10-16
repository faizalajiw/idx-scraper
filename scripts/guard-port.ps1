# guard-port.ps1 <port>
#
# Exit 0 = port BEBAS (boleh start), exit 1 = port SUDAH dipakai (jangan start).
#
# Kenapa ada: task launcher (IDX-API, IDX-Web) menyala setiap hari, sementara
# proses dari hari sebelumnya sering masih hidup. Tanpa penjaga ini, peluncuran
# berikutnya mencoba instance kedua: uvicorn gagal bind :8000, dan `next dev`
# bisa pindah ke port lain sehingga lahir dev server kedua yang membakar CPU.
# Serve sendiri tidak butuh ini karena punya kunci satu-instance di dalam proses.

param(
    [Parameter(Mandatory = $true)]
    [int]$Port
)

$busy = $false

try {
    $busy = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
}
catch {
    # Modul NetTCPIP tidak tersedia / error -> jatuh ke netstat.
    $busy = [bool](netstat -ano | Select-String -Pattern (":$Port\s+\S+\s+LISTENING"))
}

if ($busy) {
    Write-Host "port $Port sudah dipakai - start dilewati"
    exit 1
}

exit 0
