# create_shortcut.ps1 —— 在桌面创建「WorkBuddy + 用量监控」快捷方式
# 用法：右键「使用 PowerShell 运行」，或命令行执行:
#   powershell -NoProfile -ExecutionPolicy Bypass -File create_shortcut.ps1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Vbs  = Join-Path $Root 'launch_workbuddy.vbs'
if (-not (Test-Path -LiteralPath $Vbs)) { throw "找不到启动入口：$Vbs" }

$WshShell = New-Object -ComObject WScript.Shell
$Desktop   = $WshShell.SpecialFolders('Desktop')
$Shortcut  = Join-Path $Desktop 'WorkBuddy + 用量监控.lnk'
$Lnk = $WshShell.CreateShortcut($Shortcut)
$Lnk.TargetPath       = "$env:SystemRoot\System32\wscript.exe"
$Lnk.Arguments        = '"' + $Vbs + '"'
$Lnk.WorkingDirectory = $Root
$Lnk.WindowStyle      = 1
$Lnk.Description      = '同时启动 WorkBuddy 和用量监控界面'

# 图标优先用 WorkBuddy 自带图标，找不到就退回脚本图标
$wbExe = Join-Path $env:ProgramFiles 'WorkBuddy\WorkBuddy.exe'
if (Test-Path -LiteralPath $wbExe) { $Lnk.IconLocation = "$wbExe,0" }
else { $Lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,21" }
$Lnk.Save()

# 同时放一份到开始菜单，方便搜索「WorkBuddy」直接起
try {
    $Programs = $WshShell.SpecialFolders('Programs')
    if ($Programs -and (Test-Path -LiteralPath $Programs)) {
        Copy-Item -LiteralPath $Shortcut -Destination (Join-Path $Programs 'WorkBuddy + 用量监控.lnk') -Force
        Write-Output "已创建开始菜单快捷方式"
    }
} catch {
    Write-Output "开始菜单快捷方式创建失败(可忽略): $($_.Exception.Message)"
}
Write-Output "已创建桌面快捷方式：$Shortcut"
Write-Output "指向：$Vbs"
