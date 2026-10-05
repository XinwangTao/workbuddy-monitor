# night_run.ps1 — 夜间自动迭代入口（计划任务调用）
# 流程：确保 WorkBuddy(CDP) 就绪 → 跑 night runner（免费窗口内持续 autopilot，窗口结束自动退出）
# 日志: night_run.log（本目录）
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
$env:PYTHONIOENCODING = 'utf-8'
$Goal = '持续打磨 workbuddy-monitor 项目：修复缺陷、补测试、完善文档，并在回复末尾用 [[NEXT]]给出下一步优化方向'

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -LiteralPath (Join-Path $Root 'night_run.log') -Value $line -Encoding UTF8
    Write-Output $line
}

Log "=== 夜间自动迭代启动 ==="
# 1) 环境准备：WorkBuddy 带 CDP 启动 / 已跑则跳过
try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'start_workbuddy.ps1') 2>&1 | ForEach-Object { Log "start_workbuddy: $_" }
} catch { Log ("start_workbuddy 异常(继续): " + $_.Exception.Message) }

# 2) 阻塞跑 night runner（免费窗口外它会立即退出）
$py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = 'python' }
& $py (Join-Path $Root 'workbuddy_monitor.py') --night $Goal --night-max 20 2>&1 |
    ForEach-Object { Log "runner: $_" }
Log "=== 夜间自动迭代结束 ==="
