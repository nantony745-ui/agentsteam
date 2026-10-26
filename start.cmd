@echo off
setlocal
cd /d "%~dp0"
title Agentsteam
if not exist "agents.toml" (
    echo Copy agents.example.toml to agents.toml and configure your installed agents first.
    pause
    exit /b 1
)
set "memory_option="
if /i "%~1"=="memory" (
    set "memory_option=--extra memory"
    if exist ".runtime\memory-postgres.json" (
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\memory-postgres.ps1 -Action Start
        if errorlevel 1 (
            echo Project PostgreSQL failed to start. See the error above.
            pause
            exit /b 1
        )
    )
)
echo Agentsteam: http://127.0.0.1:8765/
echo Press Ctrl+C in this window to stop the service.
echo.
call uv run --locked %memory_option% uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 1 --timeout-graceful-shutdown 5
set "service_exit_code=%errorlevel%"
if not "%service_exit_code%"=="0" (
    echo.
    echo Startup or service failed. See the error above.
    pause
)
exit /b %service_exit_code%
