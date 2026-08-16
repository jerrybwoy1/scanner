@echo off
setlocal
cd /d "%~dp0"
start "Extractor Backend" cmd /k "cd backend && py -m pip install -r requirements.txt && py -m playwright install chromium && py -m uvicorn api:app --host 127.0.0.1 --port 8000"
timeout /t 2 /nobreak >nul
start "Extractor Frontend" cmd /k "npm install && npx wrangler dev"
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8787
endlocal
