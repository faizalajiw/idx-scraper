@echo off
rem Auto-start idx serve (scheduler EOD/IC + polling) - hidden background
cd /d D:\Project\market-labs\idx-scraper
set PYTHONUNBUFFERED=1
.venv\Scripts\python.exe -m idx_scraper.cli serve >> _serve.log 2>&1
