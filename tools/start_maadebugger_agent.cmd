@echo off
setlocal

rem Manual MaaDebugger agent launcher. Set the same ID in MaaDebugger.
set "AGENT_ID=mga-debug-role"

for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "PYTHON=%PROJECT_ROOT%\python\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo [MGA] Project: %PROJECT_ROOT%
echo [MGA] Agent Identifier: %AGENT_ID%
echo [MGA] Set MaaDebugger Agent Identifier to the same value.
echo [MGA] Press Ctrl+C or disconnect MaaDebugger to stop the agent.
echo.

"%PYTHON%" "%PROJECT_ROOT%\agent\start_agent.py" "%AGENT_ID%"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo [MGA] Agent exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
