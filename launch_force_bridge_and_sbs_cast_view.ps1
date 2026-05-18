$ErrorActionPreference = 'Stop'

$projectDir = 'C:\Users\User\Downloads\camera_streaming\single_sender\ZED_SBS_View'
$jx11Dir = 'C:\Users\User\Documents\project_c\button_jx11'
$pythonwExe = 'C:\Users\User\AppData\Local\Programs\Python\Python312\pythonw.exe'
$forceBridgeScript = Join-Path $projectDir 'force_bridge_server.py'
$buttonBridgeScript = Join-Path $projectDir 'operator_handle_button_bridge.py'
$viewerScript = Join-Path $projectDir 'sbs_cast_view.py'
$jx11MapperScript = Join-Path $jx11Dir 'jx11_mapper.py'
$forceBridgeUrl = 'http://127.0.0.1:8765/force'
$forceHost = '192.168.6.1'
$forcePort = '9090'
$forceTopic = '/protect/follower_state_controller/F_ext'

function Stop-ManagedPythonProcesses {
    $managedScriptPattern = 'force_bridge_server\.py|operator_handle_button_bridge\.py|sbs_cast_view\.py|jx11_mapper\.py'
    $managedProcesses = Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -match '^pythonw?\.exe$') -and
        $_.CommandLine -and
        ($_.CommandLine -match $managedScriptPattern)
    }
    foreach ($proc in $managedProcesses) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
        } catch {
        }
    }
    Start-Sleep -Milliseconds 300
}

function Start-ManagedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )
    Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru
}

if (-not (Test-Path $pythonwExe)) {
    throw "Python launcher not found: $pythonwExe"
}

$managedPids = @()
$viewerProc = $null

try {
    Stop-ManagedPythonProcesses

    $forceProc = Start-ManagedProcess -FilePath $pythonwExe -ArgumentList @(
        $forceBridgeScript,
        '--ros-host', $forceHost,
        '--ros-port', $forcePort,
        '--ros-topic', $forceTopic
    ) -WorkingDirectory $projectDir
    $managedPids += $forceProc.Id

    $buttonProc = Start-ManagedProcess -FilePath $pythonwExe -ArgumentList @(
        $buttonBridgeScript,
        '--ros-host', $forceHost,
        '--ros-port', $forcePort
    ) -WorkingDirectory $projectDir
    $managedPids += $buttonProc.Id

    $jx11Proc = Start-ManagedProcess -FilePath $pythonwExe -ArgumentList @(
        $jx11MapperScript,
        'run'
    ) -WorkingDirectory $jx11Dir
    $managedPids += $jx11Proc.Id

    $deadline = [DateTime]::UtcNow.AddSeconds(3)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            Invoke-RestMethod -Uri $forceBridgeUrl -TimeoutSec 1 | Out-Null
            break
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }

    $viewerProc = Start-ManagedProcess -FilePath $pythonwExe -ArgumentList @(
        $viewerScript,
        '--force-source', 'bridge',
        '--no-force-bridge-autostart',
        '--force-bridge-url', $forceBridgeUrl,
        '--force-host', $forceHost,
        '--force-port', $forcePort,
        '--force-topic', $forceTopic
    ) -WorkingDirectory $projectDir

    Wait-Process -Id $viewerProc.Id
} finally {
    foreach ($pid in ($managedPids | Select-Object -Unique)) {
        try {
            Stop-Process -Id $pid -Force -ErrorAction Stop
        } catch {
        }
    }
}
