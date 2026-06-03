@echo off
setlocal

cd /d "%~dp0"

if "%PORT%"=="" set "PORT=5057"
set "URL=http://127.0.0.1:%PORT%"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>nul
  if %ERRORLEVEL%==0 (
    set "PY=python"
  ) else (
    echo 未找到 Python 3。请先安装 Python 3。
    pause
    exit /b 1
  )
)

%PY% -c "import flask, yaml" >nul 2>nul
if not %ERRORLEVEL%==0 (
  echo 缺少依赖。请先运行：
  echo   pip install -r engine\requirements.txt
  pause
  exit /b 1
)

echo 启动投资周报驾驶舱：%URL%
start "" cmd /c "timeout /t 2 /nobreak >nul && start "" "%URL%""
%PY% engine\app.py

endlocal
