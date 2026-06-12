@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  一键同步（Windows）
REM  把 持仓 / 投资档案 / 成交记录 / 历史周报 提交并推送到你的私人 git 仓。
REM  双击运行即可。代码由开发流程单独维护，本脚本只同步你的个人数据。
REM  另一台电脑只需 git pull（或同样双击对应脚本）就能看到一致的数据。
REM ============================================================
cd /d "%~dp0"

echo 仓库：%cd%
echo.

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo [错误] 这里不是 git 仓库。
  echo.
  pause
  exit /b 1
)

REM 只暂存个人数据；engine\signals.json、engine\flags.json 已 gitignore，不会被带进来。
REM strategy.yaml 也要同步：网页会写它（再平衡频率/引入角色/policy_version），漏掉会双机漂移（U3-5）。
REM 逐个 add：路径可能暂时不存在（如 2026-06 换券商清零后 reports\ 为空、pull 后目录被 git 删掉），
REM 合在一条 add 里会因“一个路径匹配不到”整条失败、其他文件也暂存不上（且报错被静默）。
git add portfolio.yaml 2>nul
git add investor_profile.yaml 2>nul
git add strategy.yaml 2>nul
git add journal 2>nul
git add reports 2>nul

git diff --cached --quiet
if errorlevel 1 (
  echo [提交] 本次将提交以下文件：
  git diff --cached --name-only
  git commit -q -m "数据同步：%DATE% %TIME% 持仓/成交记录/周报"
  echo [完成] 已在本地记录这次变化。
) else (
  echo [跳过] 没有新的持仓 / 成交记录 / 周报变化，无需提交。
)
echo.

echo [拉取] 先获取远端最新...
git pull --rebase --autostash origin main
if errorlevel 1 (
  echo.
  echo [警告] 拉取/合并出现冲突（多半是两台电脑改了同一份文件）。
  echo        先别急，把这个窗口截图发给 Claude 帮你处理即可。
  echo.
  pause
  exit /b 1
)
echo.

echo [推送] 上传到远端...
git push origin main
if errorlevel 1 (
  echo.
  echo [错误] 推送失败：请检查网络或 git 登录状态后重试。
) else (
  echo.
  echo [完成] 同步成功！另一台电脑 git pull（或双击本脚本）即可看到最新数据。
)
echo.
pause
endlocal
