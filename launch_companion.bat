@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" web.py --no-browser
) else (
  py -3.11 web.py --no-browser
)
endlocal
