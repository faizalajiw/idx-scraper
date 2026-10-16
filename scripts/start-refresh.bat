@echo off
rem Fallback EOD 16:10 (Sen-Jum) - jalan walau `serve` sedang mati.
rem Dipanggil Task Scheduler IDX-EOD (WakeToRun + StartWhenAvailable).
rem Preflight dulu supaya profil Chrome tidak terkunci chrome yatim.
cd /d D:\Project\market-labs\idx-scraper
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\preflight-chrome.ps1"
set PYTHONUNBUFFERED=1
.venv\Scripts\python.exe -m idx_scraper.cli refresh --skip-if-fresh >> _refresh.log 2>&1
