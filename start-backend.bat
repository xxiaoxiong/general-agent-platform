@echo off
title AI Platform - Backend

echo ============================================
echo   AI-GeneralPlat Backend Startup
echo ============================================
echo.

cd /d "%~dp0backend"

rem Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found! Please install Python 3.11+ and add it to PATH.
    echo.
    pause
    exit /b 1
)

rem Activate virtual environment if present
if exist ".venv\Scripts\activate.bat" (
    echo [INFO] Activating venv: .venv ...
    call .venv\Scripts\activate.bat
) else if exist "..\venv\Scripts\activate.bat" (
    echo [INFO] Activating venv: ..\venv ...
    call ..\venv\Scripts\activate.bat
) else (
    echo [WARN] No virtual environment found, using system Python
)

rem Check .env (auto-create from template to avoid startup DB errors)
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo [WARN] .env not found, auto-created from .env.example
        echo [WARN] Please verify DATABASE_URL port in .env:
        echo [WARN]   local MySQL      - localhost:3306
        echo [WARN]   Docker Compose   - 127.0.0.1:3308
    ) else (
        echo [ERROR] .env missing and no .env.example template found!
        pause
        exit /b 1
    )
)

echo.
echo [INFO] Working dir: %CD%
echo [INFO] Python:
python --version
echo.
echo [INFO] Starting FastAPI service (http://0.0.0.0:8002) ...
echo [INFO] API docs: http://127.0.0.1:8002/docs
echo.

rem Start service (Ctrl+C is a normal stop, exit code 0/3073 will not be reported as error)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
set EXITCODE=%errorlevel%
if %EXITCODE% neq 0 if %EXITCODE% neq 3073 (
    echo.
    echo [ERROR] Service failed to start! exit code: %EXITCODE%
    echo [HINT] Please check:
    echo   1. Dependencies installed: pip install -r requirements.txt
    echo   2. .env exists and DATABASE_URL points to a running MySQL
    echo      - local MySQL usually listens on localhost:3306
    echo      - Docker Compose maps MySQL to host port 127.0.0.1:3308
    echo   3. Database "ai_plat" exists in MySQL
    echo   4. Port 8002 is not occupied by another process
    echo.
    pause
    exit /b 1
)

echo.
echo [INFO] Service stopped
pause
