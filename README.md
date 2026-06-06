# 投资周报助手（MVP）

一个"决策副驾"：**量化骨架算信号 + AI 增强读舆情**，每周给你一份带理由的行动清单。
你看简报 → 确认/否决 → 自己去券商手动下单。**最终决策永远在你手里。**

> ℹ️ 这是**自用私人投顾**工具：输出带理由的建议供你本人决策；不承诺收益，回测好 ≠ 未来赚钱。

## 项目结构（给维护者 / 评审者——人或 AI——的说明）

**本项目代码只有一份（单一事实源），在 `engine/`。** 两个 AI agent 各有一个"薄入口"，
都只放一份 SKILL.md、共用同一份 `engine/` 代码与根目录配置，**不存在重复或漂移的副本**：

```
MakeMoney/
├── engine/                       ← 唯一代码实现（single source of truth）
│   ├── signals.py                  周度信号引擎（配置校验 + 多源取数 + 数据新鲜度分级）
│   ├── backtest.py                 策略回测引擎（参数敏感性/换手/恢复期）← 本项目“有回测”
│   ├── validate_flags.py           AI 风险旗标校验器
│   ├── flags_schema.json           风险旗标规范（固定分类与字段）
│   ├── app.py                      网页驾驶舱后端（本地 Web UI）
│   ├── web/index.html              驾驶舱页面
│   ├── requirements.txt
│   ├── data/                       回测 seed 数据 + metadata（用于离线复现，可刷新）
│   └── cache/                      live 行情缓存（自动生成，不入库）
├── portfolio.yaml                ← 你的持仓/目标权重/现金（每周改这个）
├── strategy.yaml                 ← ETF 池、观察池、因子参数、再平衡阈值（调策略才动）
├── .claude/skills/weekly-briefing/SKILL.md   ← Claude 入口（薄包装，调 engine/）
├── .agents/skills/weekly-briefing/SKILL.md   ← Codex  入口（薄包装，调 engine/）
└── README.md
```

> 📌 **评审避坑提示**：历史快照里若看到 `.claude` 与 `.agents` 各有一份 `signals.py`、
> 或 SKILL.md 写着 `.Codex/...` 路径、或"没有回测"——那些都是**已修复的旧状态**。
> 当前：代码仅 `engine/` 一份；回测在 `engine/backtest.py`；路径统一为 `engine/...`。
>
> 🤝 Claude / Codex 协作交接请先读 [`HANDOFF.md`](HANDOFF.md)。

## 🖥️ 网页驾驶舱（最简单的用法，不用改文件）

macOS 双击：

```
start_mac.command
```

Windows 双击：

```
start_windows.bat
```

命令行启动：

```
python3 engine/app.py
```

浏览器打开 **http://127.0.0.1:5057** ，即可：编辑持仓 / 现金 / 风险偏好并保存、一键「生成本周信号」、跑回测——全程不碰 yaml。
（端口被占用时：`PORT=5058 python3 engine/app.py`。完整周报含 AI 舆情旗标，仍在 Claude / Codex 里说"给我本周决策简报"。）

## 观察池

`strategy.yaml` 里有 `watchlist`。观察池只用于学习与监控，不触发交易动作，也不参与再平衡。

当前观察方向包括：

- 现金管理：`511880`、`511990`
- 短融/短债：`511360`

> 海外宽基/成长（`513500`、`513100`）与 A 股成长（`159915`、`588000`）已升入可交易池 `universe`（见 [`HANDOFF.md`](HANDOFF.md)），不再属于观察池。

周报应分清两层：

- **持仓池**：买 / 卖 / 不动。
- **观察池**：只看趋势、动量、风险点和学习备注。

## 运行节奏

建议节奏是：**每天数据健康检查，每周投资决策**。

- 每天可运行 `python3 engine/signals.py` 刷新行情、检查缓存和观察池状态，但不因此交易。
- 每周运行正式周报，才考虑持仓再平衡、是否试仓、是否把观察池候选纳入实盘。
- 每月或每季度复盘 ETF 池和策略参数，避免频繁改规则。

## 动作门槛

`strategy.yaml` 里有 `risk_controls`，用于把信号和交易动作隔离开：

- `min_trade_amount`：低于该金额不交易。
- `max_weekly_trade_amount`：单周投入/调整上限。
- `first_tranche_pct`：0 持仓首次只投入可用现金的一部分。
- `allow_trade_with_cache`：行情来自缓存时是否允许执行真实交易。

`engine/signals.py` 会输出：

- `action_discipline`：本周纪律检查是否允许交易。
- `actionable_rebalance`：通过门槛后的再平衡动作。
- `first_funding_plan`：0 持仓账户的首次建仓预览。

这些输出只做辅助；最终仍由用户在券商手动确认和下单。

## 每周怎么用（3 步）

1. 在 Claude 或 Codex 里说 **"给我本周决策简报"**（或 `/周报`）。
   它会跑 `engine/signals.py`、扫一遍舆情，给你简报 + 行动清单。
2. 你决定**确认 / 调整 / 否决**，然后**自己在券商 App 手动下单**。
3. 成交后，打开 [`portfolio.yaml`](portfolio.yaml)，把买卖的 ETF 的 `shares` 和 `cash` 改成成交后的真实数字。

## 首次准备（一次性）

```
cp examples/portfolio.example.yaml portfolio.yaml   # 建你的私有持仓（已 .gitignore，不进版本库）
pip install -r engine/requirements.txt
```

数据来源：AkShare（免费日终行情/估值，覆盖国内场内 ETF；回测用新浪源，带本地缓存）。

## 跑回测（验证策略）

```
python3 engine/backtest.py
```

对比"本策略 / 静态再平衡 / 沪深300 买入持有"的年化、回撤、夏普、Calmar、换手率、恢复期等。

> ⚠️ **基础回测，非精确预测**：ETF 段约 6 年；指数代理段约 20 年（价格指数未含分红，低估收益）。
> **关键发现**：趋势过滤在平静的近 6 年里摊薄收益，却在含 2008/2015 的长样本里把最大回撤从约 −42% 压到 −24%——
> 它是"**危机保险**"而非"增收工具"，是否启用取决于你的风险偏好（见 `strategy.yaml` 的 `risk_profile`）。
> 指数代理段对你真实组合是**偏保守**的近似（剔除了黄金、把红利低波当沪深300），真实回撤应更浅。
> 联网机器上 `python3 engine/backtest.py --refresh` 可取前复权数据。

若本周没有可记录的新闻/政策风险旗标，可初始化空旗标文件：

```
python3 engine/validate_flags.py --init-empty
```

## 路线图

**已完成（先把它变成可靠工具 + 验证策略）**
- ✅ 单一事实源 + 双 agent 入口统一
- ✅ 配置校验（strategy.yaml + portfolio.yaml 启动即校验，失败即停）
- ✅ 多源取数（东财→新浪→缓存）+ 估值缓存/缺失状态 + 数据新鲜度分级 + 组合级行情日期
- ✅ 回测两段：① ETF 可交易（~6年）② 指数代理长期（~20年，含2008/2015）；含夏普/Calmar/换手/恢复期 + 参数敏感性 + 成本 + 数据 metadata
- ✅ 风险偏好开关 `risk_profile`（保守/平衡/进取）
- ✅ AI 舆情层结构化风险旗标 + 机械校验器
- ✅ 网页驾驶舱（本地 Web UI：改持仓/偏好、一键信号、跑回测，不用碰 yaml）
- ✅ 观察池 watchlist（只学习/监控，不触发交易）
- ✅ 动作门槛 + 0 持仓首次建仓预览

**项目状态、路线图与变更史已合并到** [`HANDOFF.md`](HANDOFF.md)（单一权威交接文档）。
当前定位：按"170 万 / 至多 100 万 ETF / 70 万稳健垫 / 目标年化 8%"重标定；P0/P1/P2 已落地（全组合口径风险预算、缓冲感知建议权重、分批建仓回测、目标可行性体检、危机保险提醒等）；待办与开放问题见 HANDOFF.md 第 5 节。
