# start_workbuddy.ps1 - WorkBuddy 启动/CDP 保障脚本
# 用法:
#   start_workbuddy.ps1                 # 正常启动(GUI + WorkBuddy CDP)
#   start_workbuddy.ps1 -CheckOnly      # 只自检,不启动任何东西
#   start_workbuddy.ps1 -RestartForCdp  # WorkBuddy 无 CDP 时优雅重启
param([switch]$CheckOnly, [switch]$RestartForCdp)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogPath = Join-Path $Root 'launch.log'
$CdpPorts = @(9224, 9223, 9222)

function Write-LaunchLog([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}
function Find-WorkBuddyCdp {
    foreach ($port in $CdpPorts) {
        try {
            $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$port/json" -TimeoutSec 1
            if (@($targets | Where-Object { $_.type -eq 'page' -and (($_.title + $_.url) -match 'WorkBuddy') }).Count) { return $port }
        } catch { }
    }
    return $null
}
function Find-Pythonw {
    $candidates = @()
    $localVenv = Join-Path $Root '.venv\Scripts\pythonw.exe'
    if (Test-Path -LiteralPath $localVenv) { $candidates += $localVenv }
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            $python = (& $pyLauncher.Source -3 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -First 1)
            if ($python) { $candidates += (Join-Path (Split-Path -Parent $python.Trim()) 'pythonw.exe') }
        } catch { }
    }
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\pythonw.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\pythonw.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\pythonw.exe'),
        'F:\ANACONDA\1\pythonw.exe'
    )
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
        $python = Join-Path (Split-Path -Parent $candidate) 'python.exe'
        if (Test-Path -LiteralPath $python) {
            & $python -c 'import tkinter, websocket' 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
    }
    return $null
}
function Find-WorkBuddyExe {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'WorkBuddy\WorkBuddy.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\WorkBuddy\WorkBuddy.exe'),
        (Join-Path $env:LOCALAPPDATA 'WorkBuddy\WorkBuddy.exe')
    )
    $uninstallRoots = @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
                        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
                        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')
    foreach ($root in $uninstallRoots) {
        if (Test-Path $root) {
            foreach ($app in Get-ChildItem $root -ErrorAction SilentlyContinue) {
                $entry = Get-ItemProperty $app.PSPath -ErrorAction SilentlyContinue
                if ($entry.DisplayName -match 'WorkBuddy' -and $entry.InstallLocation) {
                    $candidates += (Join-Path $entry.InstallLocation 'WorkBuddy.exe')
                }
            }
        }
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

try {
    $pythonw = Find-Pythonw
    $gui = Join-Path $Root 'workbuddy_gui.py'
    if (-not $pythonw) { throw '找不到 pythonw.exe。请安装带 tkinter 的 Python，或将 pythonw.exe 加入 PATH。' }
    if (-not (Test-Path -LiteralPath $gui)) { throw "找不到监控 GUI：$gui" }

    $cdpPort = Find-WorkBuddyCdp
    if ($cdpPort) {
        Write-LaunchLog "检测到 WorkBuddy CDP，端口 $cdpPort；不重复启动 WorkBuddy。"
    } else {
        $running = Get-Process -Name WorkBuddy -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($running -and -not $RestartForCdp) {
            Write-LaunchLog 'WorkBuddy 已运行但未发现 CDP；不重复启动，余额同步/模型发现可能不可用。'
        } else {
            if ($running) {
                Write-LaunchLog 'WorkBuddy 已运行但无 CDP，按 -RestartForCdp 优雅重启以启用远程调试。'
                Get-Process -Name WorkBuddy -ErrorAction SilentlyContinue | ForEach-Object {
                    try { $_.CloseMainWindow() | Out-Null } catch { }
                }
                for ($i = 0; $i -lt 10; $i++) {
                    Start-Sleep -Seconds 1
                    if (-not (Get-Process -Name WorkBuddy -ErrorAction SilentlyContinue)) { break }
                }
                # 关窗无效(例如托盘常驻)时才强杀
                Get-Process -Name WorkBuddy -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
                # 等所有进程(含子进程)完全退出,避免新实例误判旧实例仍在而放弃 CDP 端口
                for ($i = 0; $i -lt 15; $i++) {
                    Start-Sleep -Seconds 1
                    if (-not (Get-Process -Name WorkBuddy -ErrorAction SilentlyContinue)) { break }
                }
                Start-Sleep -Seconds 3  # 缓冲,让句柄/端口彻底释放
            }
            $exe = Find-WorkBuddyExe
            if ($exe) {
                if ($CheckOnly) { Write-Output "Would start WorkBuddy: $exe --remote-debugging-port=9224" }
                else {
                    Start-Process -FilePath $exe -ArgumentList '--remote-debugging-port=9224'
                    Write-LaunchLog "已启动 WorkBuddy（CDP 9224）：$exe"
                    for ($i = 0; $i -lt 30 -and -not $cdpPort; $i++) { Start-Sleep -Seconds 1; $cdpPort = Find-WorkBuddyCdp }
                    if ($cdpPort) { Write-LaunchLog "WorkBuddy CDP 就绪，端口 $cdpPort。" }
                    else { Write-LaunchLog 'WorkBuddy 已启动，但 CDP 尚未就绪；监控 GUI 仍将启动。' }
                }
            } else {
                Write-LaunchLog '未找到 WorkBuddy 安装程序；将仅启动监控 GUI。'
            }
        }
    }

    if ($CheckOnly) {
        Write-Output "Pythonw: $pythonw"
        Write-Output "GUI: $gui"
        Write-Output "Existing CDP port: $cdpPort"
        Write-Output 'Check completed; no applications were started.'
        exit 0
    }

    # GUI 单实例保护：已有监控 GUI 在跑时不重复启动，避免双实例重复发送
    $existingGui = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*workbuddy_gui.py*" } | Select-Object -First 1
    if ($existingGui) {
        Write-LaunchLog "监控 GUI 已在运行（PID=$($existingGui.ProcessId)），不重复启动。"
    } else {
        $guiProc = Start-Process -FilePath $pythonw -ArgumentList @('"' + $gui + '"', '--start-monitor') -WorkingDirectory $Root -PassThru
        Write-LaunchLog "已启动监控 GUI，并自动开启监控。PID=$($guiProc.Id)"
        # 就绪确认：GUI 进程可能在 import 阶段就崩溃（依赖缺失等），等最多 10 秒确认存活
        $alive = $false
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Seconds 1
            if (Get-Process -Id $guiProc.Id -ErrorAction SilentlyContinue) { $alive = $true; break }
        }
        if (-not $alive) {
            Write-LaunchLog '监控 GUI 启动后立即退出（可能缺少依赖或端口占用），请查看 monitor.log'
            exit 1
        }
        Write-LaunchLog '监控 GUI 已就绪。'
    }

    # 启动后自检摘要（只读，不发送任何对话）
    try {
        $py = Join-Path (Split-Path -Parent $pythonw) 'python.exe'
        if (Test-Path -LiteralPath $py) {
            & $py (Join-Path $Root 'workbuddy_monitor.py') --doctor *>&1 |
                ForEach-Object { Write-LaunchLog ("[doctor] " + $_) }
        }
    } catch {
        Write-LaunchLog "自检执行失败(可忽略): $($_.Exception.Message)"
    }
} catch {
    Write-LaunchLog "启动失败：$($_.Exception.Message)"
    exit 1
}

