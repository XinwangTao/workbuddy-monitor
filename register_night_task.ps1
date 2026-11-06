# register_night_task.ps1 — 注册/注销「WorkBuddy 夜间自动迭代」计划任务
# 注册:  powershell -ExecutionPolicy Bypass -File register_night_task.ps1
# 注销:  powershell -ExecutionPolicy Bypass -File register_night_task.ps1 -Remove
# 测试:  powershell -ExecutionPolicy Bypass -File night_run.ps1
param([switch]$Remove)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = 'WorkBuddy Night Iteration'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "已注销计划任务: $TaskName"
    exit 0
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $Root 'night_run.ps1') + '"') `
    -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Daily -At '23:05'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 12) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "已注册计划任务: $TaskName（每晚 23:05 启动（免费窗口 23:00-08:00 内），最长 12 小时；提前手动触发会等待开窗）"

