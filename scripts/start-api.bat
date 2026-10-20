@echo off
rem Auto-start API server (:8000) - hidden background
cd /d D:\Project\market-labs\idx-scraper
rem Pulihkan dulu: restart-api.ps1 membuang proses API yatim yang masih memegang
rem :8000/_api.log (itu sebabnya peluncuran bisa gagal senyap), dan hanya
rem menghentikan proses yang benar-benar melayani kalau _restart-api.flag ada.
rem Exit 2 = _api.log masih terkunci proses lain -> jangan menyalakan python,
rem karena cmd akan gagal membuka redirect dan uvicorn tidak pernah jalan.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\restart-api.ps1"
if errorlevel 2 (
  echo [%date% %time%] _api.log terkunci proses lain - API TIDAK dinyalakan >> _launcher.log
  exit /b 1
)
rem Jangan start instance kedua kalau :8000 sudah dipakai. Task launcher menyala
rem tiap hari, sedangkan proses dari hari sebelumnya biasanya masih hidup.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\guard-port.ps1" 8000
if errorlevel 1 (
  echo [%date% %time%] API :8000 sudah jalan - peluncuran dilewati >> _launcher.log
  exit /b 0
)
set PYTHONUNBUFFERED=1
rem Loop Selector + pengawas port: lihat src\idx_scraper\api\serve.py
.venv\Scripts\python.exe -m idx_scraper.api.serve --port 8000 >> _api.log 2>&1
