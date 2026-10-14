@echo off
rem Auto-start idx serve (scheduler EOD/IC + polling) - hidden background
cd /d D:\Project\market-labs\idx-scraper
rem Bebaskan profil Chrome dari chrome yatim sebelum serve start. Sisa chrome
rem dari serve yang mati menahan profil p3 dan membuat EOD masuk 0 baris.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\preflight-chrome.ps1"
set PYTHONUNBUFFERED=1
.venv\Scripts\python.exe -m idx_scraper.cli serve >> _serve.log 2>&1
