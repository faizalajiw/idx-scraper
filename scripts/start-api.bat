@echo off
rem Auto-start API server (uvicorn :8000) - hidden background
cd /d D:\Project\market-labs\idx-scraper
rem Restart terjaga: menghentikan API lama HANYA kalau _restart-api.flag ada.
rem Perlu karena proses API hidup di konteks task tersimpan; lihat
rem scripts\restart-api.ps1. Tanpa flag ini tidak ada yang dihentikan, jadi
rem port yang masih terpakai tetap membuat peluncuran dilewati di bawah.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\restart-api.ps1"
rem Jangan start instance kedua kalau :8000 sudah dipakai. Task launcher menyala
rem tiap hari, sedangkan proses dari hari sebelumnya biasanya masih hidup.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\guard-port.ps1" 8000
if errorlevel 1 (
  echo [%date% %time%] API :8000 sudah jalan - peluncuran dilewati >> _launcher.log
  exit /b 0
)
set PYTHONUNBUFFERED=1
.venv\Scripts\python.exe -m uvicorn idx_scraper.api.app:app --port 8000 >> _api.log 2>&1
