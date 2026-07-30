@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "gui.py"
    exit /b 0
)

echo [CyberEditor-Agent] Virtual environment not found.
echo Run: py -3.11 -m venv .venv
echo Then: .venv\Scripts\python.exe -m pip install -r requirements.txt
pause
exit /b 1
