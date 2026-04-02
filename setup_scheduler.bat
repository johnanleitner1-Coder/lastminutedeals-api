@echo off
REM ============================================================
REM setup_scheduler.bat — Register pipeline tasks in Windows
REM Task Scheduler. Run ONCE as Administrator.
REM
REM Creates two scheduled tasks:
REM   LastMinuteDeals_Pipeline   — runs every 4 hours, all day
REM   LastMinuteDeals_SeedRefresh — runs every Sunday at 3am
REM ============================================================

set WORKDIR=%~dp0
REM Remove trailing backslash
if "%WORKDIR:~-1%"=="\" set WORKDIR=%WORKDIR:~0,-1%

set PYTHON=python
set PIPELINE=%WORKDIR%\run_pipeline.bat
set SEED=%WORKDIR%\refresh_seeds.bat
set LOGDIR=%WORKDIR%\.tmp\logs

REM Create log directory
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo Registering LastMinuteDeals_Pipeline (every 4 hours)...

schtasks /Create /F /TN "LastMinuteDeals_Pipeline" ^
  /TR "cmd /c cd /d \"%WORKDIR%\" && call run_pipeline.bat >> \"%LOGDIR%\pipeline.log\" 2>&1" ^
  /SC HOURLY /MO 4 ^
  /ST 00:00 ^
  /RL HIGHEST

if errorlevel 1 (
    echo ERROR creating pipeline task. Make sure you run this as Administrator.
    pause
    exit /b 1
)

echo Registering LastMinuteDeals_SeedRefresh (weekly Sunday 3am)...

schtasks /Create /F /TN "LastMinuteDeals_SeedRefresh" ^
  /TR "cmd /c cd /d \"%WORKDIR%\" && call refresh_seeds.bat >> \"%LOGDIR%\seed_refresh.log\" 2>&1" ^
  /SC WEEKLY /D SUN /ST 03:00 ^
  /RL HIGHEST

if errorlevel 1 (
    echo ERROR creating seed refresh task.
    pause
    exit /b 1
)

echo.
echo Scheduled tasks created:
schtasks /Query /TN "LastMinuteDeals_Pipeline" /FO LIST | findstr "Task Name\|Next Run\|Status"
echo.
schtasks /Query /TN "LastMinuteDeals_SeedRefresh" /FO LIST | findstr "Task Name\|Next Run\|Status"

echo.
echo Done! The pipeline will now run automatically every 4 hours.
echo Logs: %LOGDIR%
echo.
echo To verify: open Task Scheduler (taskschd.msc) and find LastMinuteDeals_*
echo To run now: schtasks /Run /TN "LastMinuteDeals_Pipeline"
echo To remove:  schtasks /Delete /TN "LastMinuteDeals_Pipeline" /F
pause
