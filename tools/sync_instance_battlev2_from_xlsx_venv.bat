@echo off
setlocal

rem 切到仓库根目录
cd /d "%~dp0.."

rem 优先使用虚拟环境 Python，可通过 VENV_PYTHON 显式指定
set "PY_EXE="
if defined VENV_PYTHON if exist "%VENV_PYTHON%" set "PY_EXE=%VENV_PYTHON%"
if not defined PY_EXE if exist "%CD%\.venv\Scripts\python.exe" set "PY_EXE=%CD%\.venv\Scripts\python.exe"
if not defined PY_EXE if exist "%CD%\venv\Scripts\python.exe" set "PY_EXE=%CD%\venv\Scripts\python.exe"
if not defined PY_EXE set "PY_EXE=python"

"%PY_EXE%" "%~dp0sync_instance_battlev2_from_xlsx.py" %*
exit /b %ERRORLEVEL%
