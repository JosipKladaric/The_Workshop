param([switch]$Remove, [int]$Port = 8080)
$ErrorActionPreference = 'Stop'
$taskName = 'The Workshop Local Studio'
if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host 'The Workshop will no longer start at sign-in.'
    exit
}
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Choose a port between 1 and 65535.' }
$workspacePath = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $workspacePath 'data/cabin.db'))) {
    throw 'Start The Workshop once and save your panel password before installing automatic startup.'
}
$backgroundScript = Join-Path $PSScriptRoot 'run-background.ps1'
$arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Port {1}' -f $backgroundScript, $Port
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $workspacePath
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host 'The Workshop will start in the background when you sign in. Logs: data/server.log'
Write-Host 'Enable automatic resume in each project only after verifying its checks and budgets.'
