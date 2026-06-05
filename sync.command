#!/bin/bash
# 一键同步：把「持仓 / 投资档案 / 成交记录 / 历史周报」提交并推送到你的私人 git 仓。
# 双击运行即可。代码由开发流程单独维护，本脚本只同步你的个人数据。
# 另一台电脑只需 `git pull` 就能看到完全一致的持仓、买卖记录与周报。

cd "$(dirname "$0")" || { echo "❌ 无法进入脚本目录"; exit 1; }

echo "📂 仓库：$(pwd)"
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "❌ 这里不是 git 仓库。"
  read -n 1 -s -r -p "按任意键关闭…"; echo; exit 1
fi

# 只暂存个人数据；engine/signals.json、engine/flags.json 已 gitignore，不会被带进来。
git add portfolio.yaml investor_profile.yaml journal reports 2>/dev/null

if git diff --cached --quiet; then
  echo "✅ 没有新的持仓 / 成交记录 / 周报变化，无需提交。"
else
  echo "📝 本次将提交："
  git diff --cached --name-only | sed 's/^/   · /'
  git commit -q -m "数据同步：$(date '+%Y-%m-%d %H:%M') 持仓/成交记录/周报"
  echo "✔️  已在本地记录这次变化。"
fi

echo "⬇️  先拉取远端最新…"
if ! git pull --rebase --autostash origin main; then
  echo "⚠️  拉取/合并出现冲突（多半是两台电脑改了同一份文件）。"
  echo "   先别急，把这个窗口截图发给 Claude 帮你处理即可。"
  read -n 1 -s -r -p "按任意键关闭…"; echo; exit 1
fi

echo "⬆️  推送到远端…"
if git push origin main; then
  echo "🎉 同步完成！另一台电脑运行 git pull（或同样双击本脚本）即可看到最新数据。"
else
  echo "❌ 推送失败：请检查网络或 git 登录状态后重试。"
fi

read -n 1 -s -r -p "按任意键关闭…"; echo
