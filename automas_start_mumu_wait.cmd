@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0automas_start_mumu_wait.ps1"
exit /b %ERRORLEVEL%
