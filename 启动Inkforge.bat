@echo off
rem Inkforge one-click launcher: set engine python, then start the desktop shell.
rem The Python engine sidecar is spawned automatically by the shell.
rem
rem If your interpreter path differs, edit INKFORGE_PYTHON below or set it
rem as a system environment variable.

setlocal
set "INKFORGE_PYTHON=C:\Users\user\.conda\envs\langchain1.2\python.exe"

if not exist "%INKFORGE_PYTHON%" (
    echo [Inkforge] Engine interpreter not found: %INKFORGE_PYTHON%
    echo [Inkforge] Edit this script or set the INKFORGE_PYTHON env var, then retry.
    pause
    exit /b 1
)

cd /d "%~dp0apps\desktop"
if not exist node_modules (
    echo [Inkforge] First run: installing desktop dependencies...
    call npm install
)
if not exist out\main\index.js (
    echo [Inkforge] First run: building desktop shell...
    call npm run build
)

echo [Inkforge] Starting... the engine boots right after the window shows.
call npx electron .
endlocal
