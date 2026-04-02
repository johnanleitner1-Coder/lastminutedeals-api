# setup_scheduler.ps1 — Register pipeline tasks in Windows Task Scheduler
# Right-click this file and choose "Run with PowerShell" (no admin needed for current user tasks)

$WorkDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir  = Join-Path $WorkDir ".tmp\logs"
$Python  = "python"

# Create log directory
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ── Pipeline task — every 4 hours ────────────────────────────────────────────
$PipelineAction  = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c cd /d `"$WorkDir`" && call run_pipeline.bat >> `"$LogDir\pipeline.log`" 2>&1" `
    -WorkingDirectory $WorkDir

$PipelineTrigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Hours 4) -Once -At "00:00"

$PipelineSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

Register-ScheduledTask `
    -TaskName "LastMinuteDeals_Pipeline" `
    -Action   $PipelineAction `
    -Trigger  $PipelineTrigger `
    -Settings $PipelineSettings `
    -Force

Write-Host "Registered: LastMinuteDeals_Pipeline (every 4 hours)" -ForegroundColor Green

# ── Seed refresh task — weekly Sunday 3am ────────────────────────────────────
$SeedAction  = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c cd /d `"$WorkDir`" && call refresh_seeds.bat >> `"$LogDir\seed_refresh.log`" 2>&1" `
    -WorkingDirectory $WorkDir

$SeedTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "03:00"

Register-ScheduledTask `
    -TaskName "LastMinuteDeals_SeedRefresh" `
    -Action   $SeedAction `
    -Trigger  $SeedTrigger `
    -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 3)) `
    -Force

Write-Host "Registered: LastMinuteDeals_SeedRefresh (every Sunday 3am)" -ForegroundColor Green

# ── Show what was created ─────────────────────────────────────────────────────
Write-Host ""
Write-Host "Scheduled tasks:" -ForegroundColor Cyan
Get-ScheduledTask -TaskName "LastMinuteDeals_*" | Select-Object TaskName, State | Format-Table -AutoSize

Write-Host "Next pipeline run: " -NoNewline
(Get-ScheduledTaskInfo -TaskName "LastMinuteDeals_Pipeline").NextRunTime

Write-Host ""
Write-Host "To run immediately:  Start-ScheduledTask 'LastMinuteDeals_Pipeline'" -ForegroundColor Yellow
Write-Host "To view logs:        Get-Content '$LogDir\pipeline.log' -Tail 50" -ForegroundColor Yellow
Write-Host "To remove all:       Get-ScheduledTask 'LastMinuteDeals_*' | Unregister-ScheduledTask -Confirm:`$false" -ForegroundColor Yellow
Write-Host ""
Write-Host "Done! Pipeline runs automatically every 4 hours." -ForegroundColor Green
