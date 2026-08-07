@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
  echo Usage: launch_worker.bat https://your-control-plane.example
  echo Before launching, set CYBEREDITOR_WORKER_TOKEN to the worker token.
  echo.
  echo 用法: launch_worker.bat https://你的控制平面地址
  echo 启动前请将 CYBEREDITOR_WORKER_TOKEN 设置为独立的 Worker 密钥。
  pause
  exit /b 2
)

if "%CYBEREDITOR_WORKER_TOKEN%"=="" (
  echo CYBEREDITOR_WORKER_TOKEN is not set. / 尚未设置 Worker 密钥。
  pause
  exit /b 2
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" worker.py --server "%~1"
) else (
  py -3 worker.py --server "%~1"
)

if errorlevel 1 pause
endlocal
