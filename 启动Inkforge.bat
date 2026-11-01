@echo off
rem Inkforge one-click launcher: resolve the engine interpreter, then start the desktop shell.
rem The Python engine sidecar is spawned automatically by the shell (it inherits INKFORGE_PYTHON).
rem
rem Interpreter resolution order (first candidate that can import the engine deps wins):
rem   1) INKFORGE_PYTHON already set in the environment
rem   2) DEFAULT_PYTHON below, if you fill it in (empty by default: no machine-specific path is shipped)
rem   3) a repo-local virtualenv:  <repo>\engine\.venv\Scripts\python.exe
rem   4) any conda env under %USERPROFILE% that already has the deps (.conda / miniconda3 / anaconda3)
rem   5) "python" on PATH
rem A candidate is only accepted if `import fastapi, frontmatter, uvicorn` succeeds — otherwise the
rem engine would crash right after the window shows (ModuleNotFoundError), so we check up front.

setlocal EnableExtensions

rem ── Optional: hardcode your own interpreter here (e.g. "%USERPROFILE%\.conda\envs\myenv\python.exe")
set "DEFAULT_PYTHON="

if defined INKFORGE_PYTHON goto :resolved
if defined DEFAULT_PYTHON call :probe "%DEFAULT_PYTHON%"
if defined INKFORGE_PYTHON goto :resolved
call :probe "%~dp0engine\.venv\Scripts\python.exe"
if defined INKFORGE_PYTHON goto :resolved
for /d %%E in ("%USERPROFILE%\.conda\envs\*") do call :probe "%%~fE\python.exe"
if defined INKFORGE_PYTHON goto :resolved
for /d %%E in ("%USERPROFILE%\miniconda3\envs\*") do call :probe "%%~fE\python.exe"
if defined INKFORGE_PYTHON goto :resolved
for /d %%E in ("%USERPROFILE%\anaconda3\envs\*") do call :probe "%%~fE\python.exe"
if defined INKFORGE_PYTHON goto :resolved
call :probe_path
if defined INKFORGE_PYTHON goto :resolved

echo [Inkforge] No Python interpreter with the engine dependencies was found.
echo [Inkforge]   Install them:            python -m pip install -r engine\requirements.lock
echo [Inkforge]   Or point at your own:    set INKFORGE_PYTHON=C:\path\to\python.exe
echo [Inkforge] Then start this launcher again.
pause
exit /b 1

:resolved
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
exit /b 0

rem ─────────── helpers ───────────
:probe
rem %~1 = candidate python.exe; accepted only when the engine deps import.
if defined INKFORGE_PYTHON exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import fastapi, frontmatter, uvicorn" >nul 2>nul
if errorlevel 1 exit /b 0
set "INKFORGE_PYTHON=%~1"
exit /b 0

:probe_path
if defined INKFORGE_PYTHON exit /b 0
where python >nul 2>nul
if errorlevel 1 exit /b 0
python -c "import fastapi, frontmatter, uvicorn" >nul 2>nul
if errorlevel 1 exit /b 0
set "INKFORGE_PYTHON=python"
exit /b 0
