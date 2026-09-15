@echo off
setlocal
cd /d "%~dp0"
uv run python -m snappix
if errorlevel 1 (
    echo.
    echo [snappix-viewer] 起動に失敗しました（終了コード %errorlevel%）。
    echo [snappix-viewer] uv がインストールされているか、`uv sync` が実行済みか確認してください。
    pause
)
endlocal
