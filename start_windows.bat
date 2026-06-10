@echo off
setlocal

cd /d "%~dp0"

if "%PORT%"=="" set "PORT=5057"
set "URL=http://127.0.0.1:%PORT%"

echo Checking for old dashboard service on port %PORT%...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$port=[int]$env:PORT; $lines=netstat -ano | Select-String ('^\s*TCP\s+\S+:'+$port+'\s+\S+\s+LISTENING\s+\d+$'); $ids=@($lines | ForEach-Object { [int](($_.Line -split '\s+')[-1]) } | Sort-Object -Unique); if($ids.Count -eq 0){exit 0}; $foreign=@(); foreach($id in $ids){$p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$id) -ErrorAction SilentlyContinue; if(-not $p -or $p.CommandLine -notmatch 'engine[\\/]+app\.py'){$foreign += $id}}; if($foreign.Count -gt 0){Write-Host ('Port '+$port+' is used by another program. PID: '+($foreign -join ',')); exit 2}; Write-Host ('Stopping old dashboard process: '+($ids -join ',')); foreach($id in $ids){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue}; Start-Sleep -Milliseconds 800"
if not %ERRORLEVEL%==0 (
  echo Port %PORT% is occupied by a non-dashboard process. Use another port, for example:
  echo   set PORT=5058
  pause
  exit /b 1
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>nul
  if %ERRORLEVEL%==0 (
    set "PY=python"
  ) else (
    echo Python 3 was not found. Please install Python 3 first.
    pause
    exit /b 1
  )
)

%PY% -c "import flask, yaml, pandas, akshare" >nul 2>nul
if not %ERRORLEVEL%==0 (
  echo Missing dependencies. Please run:
  echo   pip install -r engine\requirements.txt
  pause
  exit /b 1
)

echo Starting investment dashboard: %URL%
start "" cmd /c "timeout /t 2 /nobreak >nul && start "" "%URL%"""
%PY% engine\app.py
if not %ERRORLEVEL%==0 (
  echo.
  echo The dashboard exited with an error. Read the message above,
  echo or take a screenshot and ask Claude for help.
  pause
)

endlocal
