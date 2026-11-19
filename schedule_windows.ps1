# Registers a Windows scheduled task that crawls every hour while this PC is on.
# Run once:  powershell -ExecutionPolicy Bypass -File schedule_windows.ps1
# Remove:    Unregister-ScheduledTask -TaskName PuneJobCrawler -Confirm:$false
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python).Source
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$dir\crawler.py`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName PuneJobCrawler -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Scheduled: crawls every hour. Open the dashboard with: python crawler.py serve"
