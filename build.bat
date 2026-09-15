@echo off
setlocal
cd /d "%~dp0"

echo [build.bat] Syncing dependencies (tagger + dev extras)...
rem --inexact keeps packages that plugin development layered into the venv.
uv sync --extra tagger --extra dev --inexact
if errorlevel 1 (
    set "BUILD_ERRORLEVEL=%errorlevel%"
    goto :fail
)

echo [build.bat] Running build_portable.py...
uv run python build_portable.py
if errorlevel 1 (
    set "BUILD_ERRORLEVEL=%errorlevel%"
    goto :fail
)

echo.
echo [build.bat] Done. See the [build] summary above for the exact dist layout.
endlocal
exit /b 0

:fail
echo.
echo [build.bat] FAILED (exit code %BUILD_ERRORLEVEL%)
pause
endlocal & exit /b %BUILD_ERRORLEVEL%
