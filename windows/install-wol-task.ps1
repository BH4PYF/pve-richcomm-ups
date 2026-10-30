$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "wol-ups.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $script)
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date.AddHours(8))
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "UPS-WOL-0800" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

$task = Get-ScheduledTask -TaskName "UPS-WOL-0800"
"TaskName : {0}" -f $task.TaskName
"State    : {0}" -f $task.State
"Start    : {0}" -f ($task.Triggers | Select-Object -First 1).StartBoundary
"WakeToRun: {0}" -f $task.Settings.WakeToRun
"Missed   : {0}" -f $task.Settings.StartWhenAvailable
"Action   : {0}" -f ($task.Actions | Select-Object -First 1).Arguments
Get-ScheduledTaskInfo -TaskName "UPS-WOL-0800" | Select-Object NextRunTime, LastRunTime, LastTaskResult | Format-List
