@echo off
setlocal
cd /d "%~dp0"
start "Extractor Backend" cmd /k "cd backend && py -m pip install -r requirements.txt && py -m uvicorn api:app --host 127.0.0.1 --port 8000"
start "Extractor Frontend" cmd /k "npm install && npx wrangler dev"
endlocal
