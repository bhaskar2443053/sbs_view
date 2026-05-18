@echo off
setlocal

set "PROJECT_DIR=C:\Users\User\Downloads\camera_streaming\single_sender\ZED_SBS_View"
set "JX11_DIR=C:\Users\User\Documents\project_c\button_jx11"
set "PYTHON_EXE=C:\Users\User\AppData\Local\Programs\Python\Python312\python.exe"
set "PYTHONW_EXE=C:\Users\User\AppData\Local\Programs\Python\Python312\pythonw.exe"
set "FORCE_BRIDGE_SCRIPT=%PROJECT_DIR%\force_bridge_server.py"
set "BUTTON_BRIDGE_SCRIPT=%PROJECT_DIR%\operator_handle_button_bridge.py"
set "VIEWER_SCRIPT=%PROJECT_DIR%\sbs_cast_view.py"
set "JX11_MAPPER_SCRIPT=%JX11_DIR%\jx11_mapper.py"
set "FORCE_BRIDGE_URL=http://127.0.0.1:8765/force"
set "FORCE_HOST=192.168.6.1"
set "FORCE_PORT=9090"
set "FORCE_TOPIC=/protect/follower_state_controller/F_ext"

if not exist "%PYTHON_EXE%" (
    echo Python launcher not found: %PYTHON_EXE%
    pause
    exit /b 1
)

if not exist "%PYTHONW_EXE%" (
    echo Python launcher not found: %PYTHONW_EXE%
    pause
    exit /b 1
)

call :stop_managed

start "" /b "%PYTHONW_EXE%" "%FORCE_BRIDGE_SCRIPT%" --ros-host %FORCE_HOST% --ros-port %FORCE_PORT% --ros-topic %FORCE_TOPIC%
start "" /b "%PYTHONW_EXE%" "%BUTTON_BRIDGE_SCRIPT%" --ros-host %FORCE_HOST% --ros-port %FORCE_PORT%
start "" /b "%PYTHONW_EXE%" "%JX11_MAPPER_SCRIPT%" run

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$url = '%FORCE_BRIDGE_URL%'; $deadline = [DateTime]::UtcNow.AddSeconds(3); while ([DateTime]::UtcNow -lt $deadline) { try { Invoke-RestMethod -Uri $url -TimeoutSec 1 | Out-Null; break } catch { Start-Sleep -Milliseconds 250 } }"

pushd "%PROJECT_DIR%"
"%PYTHON_EXE%" "%VIEWER_SCRIPT%" --start-display-mode 2d --force-source bridge --no-force-bridge-autostart --force-bridge-url "%FORCE_BRIDGE_URL%" --force-host %FORCE_HOST% --force-port %FORCE_PORT% --force-topic %FORCE_TOPIC%
set "VIEWER_EXIT=%ERRORLEVEL%"
popd

call :stop_managed
exit /b %VIEWER_EXIT%

:stop_managed
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$managedPattern = 'force_bridge_server\.py|operator_handle_button_bridge\.py|sbs_cast_view\.py|jx11_mapper\.py'; Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^pythonw?\.exe$') -and $_.CommandLine -and ($_.CommandLine -match $managedPattern) } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }; Start-Sleep -Milliseconds 300"
goto :eof
