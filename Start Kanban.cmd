@echo off
setlocal
cd /d "%~dp0"
if not exist "index.html" goto missing
if not exist "board.js" goto missing
if not exist "server.py" goto missing
set "PYTHON="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON=py -3"
)
if not defined PYTHON (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYTHON=python"
    )
)
if not defined PYTHON (
    where python3 >nul 2>nul
    if not errorlevel 1 (
        python3 -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYTHON=python3"
    )
)
rem A Python installed only through uv is on no PATH; ask uv for it.
if not defined PYTHON (
    where uv >nul 2>nul
    if not errorlevel 1 (
        for /f "usebackq delims=" %%i in (`uv python find --no-project ">=3.10" 2^>nul`) do set "PYTHON="%%i""
    )
)
if not defined PYTHON goto pythonmissing
echo.
echo Starting Human Kanban...
%PYTHON% server.py %*
if errorlevel 1 goto serverfailed
goto :eof
:missing
echo ERROR: Keep index.html, board.js, and server.py in the same directory.
goto fallback
:pythonmissing
echo ERROR: Python 3.10+ was not found. Install Python from https://www.python.org/downloads/windows/
echo        During installation, enable the Python launcher / Add Python to PATH.
echo        With uv installed, "uv python install 3.12" also works.
echo        No third-party Python packages are required.
goto fallback
:serverfailed
echo ERROR: Could not run the server. Read the error above.
echo        If port 8765 is already in use, try: "Start Kanban.cmd" --port 8766
goto fallback
:fallback
echo Read-only fallback: double-click index.html in this folder.
pause
exit /b 1
