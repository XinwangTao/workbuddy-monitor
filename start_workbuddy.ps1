param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogPath = Join-Path $Root 'launch.log'
$CdpPorts = @(9223, 9222)

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
        if ($running) {
            Write-LaunchLog 'WorkBuddy 已运行但未发现 CDP；不重复启动，余额同步/模型发现可能不可用。'
        } else {
            $exe = Find-WorkBuddyExe
            if ($exe) {
                if ($CheckOnly) { Write-Output "Would start WorkBuddy: $exe --remote-debugging-port=9223" }
                else {
                    Start-Process -FilePath $exe -ArgumentList '--remote-debugging-port=9223'
                    Write-LaunchLog "已启动 WorkBuddy（CDP 9223）：$exe"
                    for ($i = 0; $i -lt 15 -and -not $cdpPort; $i++) { Start-Sleep -Seconds 1; $cdpPort = Find-WorkBuddyCdp }
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
    $guiProc = Start-Process -FilePath $pythonw -ArgumentList @('"' + $gui + '"', '--start-monitor') -WorkingDirectory $Root -PassThru
    Write-LaunchLog "已启动监控 GUI，并自动开启监控。PID=$($guiProc.Id)"

    # 就绪确认：GUI 进程可能在 import 阶段就崩溃（依赖缺失等），
    # 这里等最多 10 秒确认进程存活，否则报错让用户知道"点了没反应"的原因。
    $alive = $false
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Seconds 1
        $p = Get-Process -Id $guiProc.Id -ErrorAction SilentlyContinue
        if ($p) { $alive = $true; break }
    }
    if (-not $alive) {
        Write-LaunchLog '监控 GUI 启动后立即退出（可能缺少依赖或端口占用），请查看 monitor.log'
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            "监控界面启动后立即退出了。`n`n常见原因：`n1) 缺少依赖（tornado/requests）`n2) 端口被占用`n`n请查看 launch.log 与 monitor.log。",
            'WorkBuddy Monitor 启动异常', 'OK', 'Warning') | Out-Null
        exit 1
    }
    Write-LaunchLog '监控 GUI 已就绪。'

    # 启动后自检摘要（只读，不发送任何对话）：把免费模型/时段/活跃迭代写入 launch.log
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
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'WorkBuddy Monitor 启动失败', 'OK', 'Error') | Out-Null
    exit 1
}
