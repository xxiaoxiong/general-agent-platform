@echo off
title General Agent Platform - Frontend

echo ============================================
echo   General Agent Platform Frontend Startup
echo ============================================
echo.

cd /d "%~dp0frontend"

rem Check Node.js
node --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js not found! Please install Node.js 20+ and add it to PATH.
    echo.
    pause
    exit /b 1
)

rem Install dependencies on first run
if not exist "node_modules" (
    echo [INFO] First run, installing dependencies...
    call npm install
    if errorlevel 1 (
        echo [ERROR] Dependency install failed! Check network / npm registry and retry.
        echo.
        pause
        exit /b 1
    )
    echo.
)

echo [INFO] Working dir: %CD%
echo [INFO] Starting Vite dev server...
echo [INFO] URL: http://localhost:5173
echo [INFO] API proxy target: http://127.0.0.1:8002 (start start-backend.bat first)
echo.

rem Start dev server (Ctrl+C is a normal stop, exit code 0/3073 will not be reported as error)
call npm run dev
set EXITCODE=%errorlevel%
if %EXITCODE% neq 0 if %EXITCODE% neq 3073 (
    echo.
    echo [ERROR] Frontend failed to start! exit code: %EXITCODE%
    echo [HINT] Check Node.js version (20+), or delete node_modules and rerun npm install.
    echo.
    pause
    exit /b 1
)

echo.
echo [INFO] Service stopped
pause
