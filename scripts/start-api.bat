@echo off
rem Auto-start API server (uvicorn :8000) - hidden background
cd /d D:\Project\market-labs\idx-scraper
set PYTHONUNBUFFERED=1
.venv\Scripts\python.exe -m uvicorn idx_scraper.api.app:app --port 8000 >> _api.log 2>&1
