# night_run.ps1 - 夜间自动迭代入口（计划任务调用）
# 流程：等待免费窗口开启 → 确保 WorkBuddy(CDP) 就绪 → 跑 night runner
# 免费窗口默认 23:00-08:00（config.json free_window）。
# 日志: night_run.log（本目录，UTF-8）
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
# 修复日志中文乱码：强制控制台/管道走 UTF-8
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }
$env:PYTHONIOENCODING = 'utf-8'
$Goal = '持续打磨 workbuddy-monitor 项目：修复缺陷、补测试、完善文档。每轮必须先读 SOP.md 并严格遵守（单主题小改动、测试全绿、可核查的[[NEXT]]），回复末尾用 [[NEXT]]给出下一步优化方向'

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -LiteralPath (Join-Path $Root 'night_run.log') -Value $line -Encoding UTF8
    Write-Output $line
}

Log "=== 夜间自动迭代启动 ==="

# 0) 等待免费窗口开启（最多等 2 小时；已开窗则直接进入）
$waited = 0
while ($true) {
    $now = Get-Date
    # 跨夜窗口判断：当天 23:00 之后，或当天 08:00 之前（属昨夜窗口）
    $t = $now.TimeOfDay
    $inWindow = ($t -ge [TimeSpan]::FromHours(23)) -or ($t -lt [TimeSpan]::FromHours(8))
    if ($inWindow) { break }
    if ($waited -ge 120) {
        Log "等待免费窗口超时（2 小时仍未到 23:00），本次退出"
        Log "=== 夜间自动迭代结束（未进窗） ==="
        exit 0
    }
    if ($waited -eq 0) { Log "尚未进入免费窗口（23:00 开窗），等待中..." }
    Start-Sleep -Seconds 60
    $waited++
}
if ($waited -gt 0) { Log "免费窗口已开启（等待了 $waited 分钟）" }

# 1) 环境准备：WorkBuddy 带 CDP 启动 / 已跑则跳过；没带 CDP 的旧进程会被优雅重启
try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'start_workbuddy.ps1') -RestartForCdp 2>&1 | ForEach-Object { Log "start_workbuddy: $_" }
} catch { Log ("start_workbuddy 异常(继续): " + $_.Exception.Message) }

# 2) 阻塞跑 night runner（窗口结束或跑满轮数自动退出）
$py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = 'python' }
& $py (Join-Path $Root 'workbuddy_monitor.py') --night $Goal --night-max 20 2>&1 |
    ForEach-Object { Log "runner: $_" }
Log "=== 夜间自动迭代结束 ==="


