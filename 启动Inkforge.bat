@echo off
rem Inkforge one-click launcher: resolve the engine interpreter, then start the desktop shell.
rem The Python engine sidecar is spawned automatically by the shell.
rem
rem Interpreter resolution order (first one that exists wins):
rem   1) INKFORGE_PYTHON already set in the environment
rem   2) the local default path below (edit it if your conda env lives elsewhere)
rem   3) "python" on PATH
rem Using "python" on PATH is only a fallback: the engine needs the packages from
rem engine/requirements.txt installed in that interpreter.

setlocal
set "DEFAULT_PYTHON=C:\Users\user\.conda\envs\langchain1.2\python.exe"

if not defined INKFORGE_PYTHON (
    if exist "%DEFAULT_PYTHON%" (
        set "INKFORGE_PYTHON=%DEFAULT_PYTHON%"
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo [Inkforge] No Python interpreter found.
            echo [Inkforge] Install Python 3.12+, or set INKFORGE_PYTHON to your interpreter, then retry.
            pause
            exit /b 1
        )
        echo [Inkforge] Default interpreter not found; falling back to "python" on PATH.
        set "INKFORGE_PYTHON=python"
    )
)

echo [Inkforge] Engine interpreter: %INKFORGE_PYTHON%

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
