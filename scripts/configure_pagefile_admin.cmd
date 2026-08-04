@echo off
setlocal
set "SCRIPT=%~dp0configure_pagefile.ps1"
set "RESULT=%~dp0..\data\pagefile-config-result.json"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','%SCRIPT%','-DataDrive','D:','-InitialSizeMB','32768','-MaximumSizeMB','49152','-SystemDriveInitialMB','4096','-SystemDriveMaximumMB','8192','-ResultPath','%RESULT%')"
if exist "%RESULT%" (
  echo Page-file configuration saved. Restart Windows manually when convenient.
) else (
  echo Configuration did not complete. Accept the UAC prompt and try again.
)
pause
