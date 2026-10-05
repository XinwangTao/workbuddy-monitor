' WorkBuddy Monitor 一键启动入口
' 双击本文件即可启动 PowerShell 启动器；窗口隐藏运行。
Set sh = CreateObject("WScript.Shell")
scriptPath = CreateObject("Scripting.FileSystemObject").BuildPath(CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName), "start_workbuddy.ps1")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptPath & """"
sh.Run cmd, 0, False
