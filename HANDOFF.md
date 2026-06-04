# 交接文档 / Project Handoff（单一权威文档）

> 本文件已合并原 `PLAN.md`（路线图）、`CHANGELOG.md`（变更史）、`HANDOFF.md`（项目状态）。
> **下一个接手的 agent：先完整读这份。** 个股推荐 / 高频 / 自动下单都不做。
>
> **硬边界**：教育/辅助决策工具——**输出建议、不构成投资建议、不承诺收益、不自动下单**；人在环，最终拍板与下单永远在用户手里；**ETF-only**；不编造数据（缺失就如实标"不可用/缺失"）。

---

## 0. 协作规则 / 单一事实源

- 核心代码只在 `engine/`。两个 agent 入口 `.claude/skills/weekly-briefing/SKILL.md`、`.agents/skills/weekly-briefing/SKILL.md` **只是薄包装**，不要把 `signals.py` / `backtest.py` / app 逻辑拷进 agent 目录。
- 改行为：**先改 `engine/` 实现**，再按需更新 `README.md` / 两个 SKILL（仅当接口变化）。
- 每改一处：跑 `python engine/tests/test_engine.py`（当前 **69 用例**，纯函数、无网络）必须全绿；前端改完 `node --check engine/web/app.js`。

## 1. 用户真实定位（所有标定的依据）

| 项目 | 取值 |
|---|---|
| 总资金 | **170 万** |
| ETF 风险桶上限 | **至多 100 万**（慢慢分批，是上限不是目标） |
| 场外稳健桶 | **70 万**（活期/固收/定存；只让算法"知道有"，不跟踪明细） |
| 目标年化 | **8%**（针对 ETF 桶"做工的钱"，非全组合承诺、非保证） |
| 最大可接受回撤 | **20%**（全组合 170 万口径） |
| 经验 / 节奏 | intermediate；约 **12–24 个月**边学边投，按估值与学习里程碑调速 |

**核心策略洞察（整个重标定的灵魂）**：70 万稳健桶是"安全垫"——正因为有它，100 万 ETF 桶才能更激进去够 8%，同时把全组合回撤压在 20% 内。
**必须诚实保留**：8% 即便对 ETF 桶也偏进取（回测 ETF 段约 4–6%、长代理段约 8% 但伴 −40%+ 回撤），需权益重仓 + 容忍股票级波动，工具要把权衡量化讲清，绝不暗示稳赚。

**当前账户状态**（用户已应用缓冲感知建议权重）：
- `portfolio.yaml`：9 只，目标权重 = 国债0.07 / 沪深300 0.15 / 红利低波0.09 / 中证500 0.15 / 黄金0.10 / 标普500 0.21 / 纳指0.13 / 创业板0.06 / 科创50 0.06。ETF 桶权益约 85%、全组合压力回撤约 17%（预算 20% 内）。
- `investor_profile.yaml`：target 0.08 / max_dd 0.20 / experience intermediate / stable_assets_outside 700000 / stable_assets_yield 0.025 / planned_etf_capital 1000000 / emergency_cash 0。
- `strategy.yaml`：`risk_profile: 进取`；`risk_controls`：min_trade 500、max_weekly 50000、first_tranche_pct 0.15、allow_trade_with_cache false。universe 9 只、watchlist 3 只。

## 2. 组件与架构

| 文件 | 职责 |
|---|---|
| `engine/signals.py` | 周度信号引擎：趋势(MA200)/动量(60d)/估值分位/再平衡(5-25)；多源取数+缓存+数据分级；风险预算（全组合口径）；首次建仓预览；动作门槛；`trend_alerts`（危机保险） |
| `engine/backtest.py` | 回测：① ETF 可交易段 ② 指数代理长段（价格指数，看回撤轮廓）；**DCA 分批建仓对比**（`run_dca`） |
| `engine/reports.py` | 周报归档 + 月度复盘（看是否守规则，不算盈亏）+ 成交后持仓草稿 |
| `engine/validate_flags.py` + `flags_schema.json` | AI 舆情风险旗标的结构校验 |
| `engine/learning.py` + `learning_cards.yaml` | 观察池学习系统（观察≥4周+学完→可讨论纳入；永不可直接买） |
| `engine/app.py` + `engine/web/` | 本地 Web 驾驶舱（Flask + 单页 vanilla JS + 本地 ECharts）；不实现独立投资逻辑，只调 `engine/` |
| `strategy.yaml` / `portfolio.yaml` / `investor_profile.yaml` | 策略参数 / 持仓 / 个人档案 |
| `engine/data/` | 回测种子数据（committed，离线可复现）；`meta.json` 记来源/复权/区间 |

## 3. 关键不变量 & 耦合（改动**勿破坏**）

- **数据诚实**：缺数据标"不可用/缺失"，绝不编造；`grade_data` 分级 完整/缓存可用/过旧/部分缺失；只有"完整/缓存可用"才给再平衡；`allow_trade_with_cache=false` → 含缓存行情时拦截可执行交易。
- **全组合口径**：`signals.whole_portfolio_stress(etf_dd, etf_value, stable_outside)` 把 ETF 桶压力回撤按"稳健桶=0 冲击"折算到全组合；`risk_budget` 同时带 ETF 桶（`target_portfolio_stress_*`）与全组合（`whole_portfolio_*`）数值；**风险闸门与拦截文案都用全组合口径**（`max_acceptable_loss`/`stress_losses` 也用全组合基数；`target_annual_profit` 用 ETF 桶，已标注）。
- **两处 `DEFAULT_INVESTOR_PROFILE` 必须同步**（`signals.py` 和 `app.py` 各一份）；新字段 `stable_assets_outside`/`stable_assets_yield`/`planned_etf_capital` 要在 `save_config` 持久化（UI 无输入时按现值回退、不丢）、`_write_investor_profile` 写出、`validate_investor_profile` 校验。
- **建议权重 `app._suggest_target_weights`**：基于**整个 universe**（含未持有品种）；缓冲感知——`etf_share=planned_etf/(planned_etf+stable)`，`etf_dd_budget=min(max_dd/etf_share, 0.40)`；按 sleeve 参数化搜索权益比例（`e_cap` 随经验 0.65/0.85/0.95）；**残差并入当前最大权重项**（早先并入债券会在债券=0 时被 `max(0,..)` 吞掉→合计 1.01）；sleeve 的收益/冲击假设**复用 `signals.ASSET_EXPECTED_RETURN`/`ASSET_SHOCKS`**（勿再各写一份）。
- **估值三态**：`signals.VALUATION_APPLICABLE_ASSETS`（仅 A 股权益）。QDII/黄金/债券/现金 → `valuation_na`（不适用，不当缺失也不当中性）；A 股权益但无可用源（红利低波/创业板/科创50）→ `valuation_missing`（**非中性**，如实标）；有 index 且取到 → 分位。preflight/CLI/主信号视图/周报详情四处都区分三态。
- **DCA / 长回测**：`run_dca`/`_dca_sim`/`_median`（一次性 vs 6/12/24 月滚动窗口）；proxy 段**单个代理缺失只剔除该 sleeve、不整段放弃**；`159915`/`588000` 的 `proxy_index=null`（创业板指 2010/科创50 2019 太短，并入会把"20年段"截断）。
- **westock（腾讯自选股）数据源**——两处用途，都经 `npx -y westock-data-skillhub@1.0.3`（需 Node≥18 + `Bash(npx:*)` 放行；`.claude/settings.local.json` 本机已加、换机器要重加）：
  1. **行情【首选源】（signals.py 取价主链路）**：`fetch_hist` 顺序 **westock(腾讯,qfq) → 东财 → 新浪 → 缓存**（实测腾讯更稳更全，akshare 日线接口常抽风）。性能：`main()` 先 `prefetch_westock(所有code)` 一次**批量** npx（输出含 `symbol` 列单表，`_parse_westock_kline_batch` 解析）填 `_WESTOCK_HIST`，`fetch_hist` 命中、避免逐只 npx（整轮 ~20s）。westock 数据 `source="westock"`、按"完整"对待（不计 used_cache、不触发缓存禁令）。npx/Node 不可用时自然回退东财/新浪/缓存——akshare 仍是安全网。注意 westock K线 OHLC 仅 2 位小数（4.93 vs akshare 4.926），对趋势/动量/周度配置无碍。
  2. **ETF 质量层兜底**：akshare 快照缺折溢价/规模时，`_etf_quality_for`→`_quality_metrics`→`_westock_etf_metrics` 调 `... etf <code>` 补折溢价/规模/成交额 + **QDII 申购状态**（`不可申购`→敏感品种 issue）；进程缓存 300s、失败返 None、不编造。
  - backtest.py 未改（仍以 `engine/data/` 种子为主、`--refresh` 走东财/新浪），保持离线可复现。
- **前端**：`applyTargetSuggestion()` 从建议项构建持仓（含新升入品种、保留已有 shares）；`marketTrackCodes()` 让"行情与质量"追踪整个 universe；ECharts 本地优先 `/web/vendor/echarts.min.js` + CDN 兜底（`window.echarts||document.write(...)`）；目标可行性在**活的 `renderSignals` 决策区**显示（读 `risk_budget.expected_etf_return`/`whole_portfolio_stress_drawdown`），常驻 `strategyStrip` 显示目标年化/回撤/投资期等。
- **校验约束**：`validate_strategy` 要求 universe **有且仅有一个 `asset:bond`**；watchlist 与 universe 不得重复。
- ECharts 实例经 `initChart()` 注册到 `ECHARTS[]`，`activateTab` 调 `resizeCharts()`；`static_folder=None`，只服务 `/` 与 `/web/<path>`。

## 4. 已完成（P0 / P1 / P2 全部落地并验证；69 测试全绿）

- **P0 重标定 + 全组合 + 拓宽菜单**：universe 5→9（加 513500 global_equity、513100 global_growth、159915+588000 china_growth），watchlist 收到现金/短债 3 只；全组合风险预算 + 缓冲感知建议权重；门槛重标定（max_weekly 1万→5万、first_tranche 0.25→0.15）。
- **P1 分批与可行性**：DCA 分批建仓回测（前端"建仓路径对比"图+表）；目标可行性体检（`expected_etf_return` vs 目标 + 缺口）；危机保险提醒（`trend_alerts` 权益跌破 MA200）。
- **P2 覆盖与工程**：估值"不适用 vs 缺失(非中性)"区分；ECharts 本地化；编辑设置可配置稳健桶；新 ETF 纳入行情与质量追踪；westock 质量兜底。
- **整体 review 修复**（多 agent 对抗审查后逐条核实）：拦截文案改全组合口径；建议权重 1.01 修复；长回测截断修复；P1-2 可行性从死代码挪到 `renderSignals`；删除死函数 `renderGoalCoach`/`renderDecisionGuide`；补正注释与示例档案。

## 5. 待办 / 开放问题（下一个 agent 从这里继续）

1. **P2-2 真实业绩跟踪**（暂缓，有依据）：诚实业绩须用**资金/时间加权收益（TWR/MWR）剔除分批注入现金流**，否则把"持续注入本金"显示成"收益"会误导。前置：① 每日（或每周报时）落一份组合 NAV 快照；② 记录外部现金流；③ 算 TWR/MWR 再对比基准。当前"浮动盈亏 + 月度守规则复盘"已覆盖诚实子集。
2. **A 股成长估值接入**：红利低波/创业板/科创50 现为 `valuation_missing`。`创业板指`/`科创50`/`中证红利` **不是** `ak.stock_index_pe_lg` 合法符号（实测 KeyError），需找到可用 PE 分位源再接，别硬塞（会"永远取数失败"误标缺失）。
3. **ETF 替代候选比较**：需可靠的费率/跟踪误差/同类清单数据源（westock 的 `etf` 给管理费/托管费，可作起点）。
4. **⚠️ QDII 溢价实盘提醒（直接影响用户）**：用户组合含 513500 标普500(21%) + 513100 纳指(13%) 两只 QDII。实测 **513500 当前溢价 +4.9% 且"不可申购"**（典型溢价陷阱）。建议这两只 QDII **先缓、等溢价≤1.5% 或恢复申购再买**；每次建仓前看"折溢价/申购状态"。质量层已能在 akshare/westock 下给出该警告。
5. **前端浏览器验证**：`.claude/launch.json` 是 macOS 配置（`/bin/zsh`+`python3`），本机 Windows 跑不了 preview——UI 改动目前靠"数据层 + `node --check`"验证。若要可视化验收，加一个 Windows launch 配置或手动 `python engine/app.py` 看。
6. **取数稳定性现状**：**行情首选 westock(腾讯)，再东财→新浪→缓存**（akshare 日线接口实测常抽风，腾讯更稳更全）；**估值**仍只走 akshare/legulegu（`stock_index_pe_lg`），legulegu **较脆**（连不上时回退缓存/标缺失）——westock 不提供 PE 分位，估值备用源仍是开放项。可考虑：给估值加备用源、或养"每日刷新缓存"的健康检查、或把 westock 行情也接进 backtest 的 `--refresh`。

## 6. 数据与文件（gitignore 现状）

- **现已入库**（私人仓，用户确认无隐私风险）：`portfolio.yaml`、`investor_profile.yaml`（配置）；`reports/`（周报归档）、`journal/`（执行/学习记录）。
- **仍忽略**（每次运行重写/高频 churn）：`engine/signals.json`、`engine/flags.json`、`engine/cache/`；以及 `.claude/settings.local.json`、`.DS_Store`、`__pycache__/`、`*.pyc`。
- **种子数据** `engine/data/*.csv` + `meta.json` 入库，供离线复现回测（含为 4 只新 ETF 补的种子）。
- ⚠️ `.claude/settings.local.json` 不入库 → 换机器后要重新加 `Bash(npx:*)` 才能用 westock 兜底。

## 7. 常用命令

```bash
python -m py_compile engine/signals.py engine/backtest.py engine/validate_flags.py engine/reports.py engine/app.py engine/learning.py
python engine/tests/test_engine.py          # 回归测试（无网络，秒级）
python engine/signals.py                     # 生成本周信号 → engine/signals.json
python engine/validate_flags.py --init-empty # 无重大事件时初始化空旗标
python engine/reports.py                     # 归档可视化周报 → reports/<id>/
python engine/backtest.py                    # 回测（--json 出结构化、--refresh 联网重取种子）
python engine/app.py                          # 本地驾驶舱 http://127.0.0.1:5057（PORT=5058 可改端口）
node --check engine/web/app.js               # 前端语法检查
```

## 8. 运行节奏 / 动作门槛 / 周报流程 / 观察池 / 投资边界

- **节奏**：每天可跑 `signals.py` 做数据健康/观察（不代表每天交易）；每周正式决策；每月/季复盘策略与池。低频、克制。
- **动作门槛**（`risk_controls`）：保留原始 `rebalance`（信号级偏离），用户可执行动作看 `actionable_rebalance`，0 持仓用 `first_funding_plan`；UI 只展示预览，绝不自动写交易/改份额；观察池永不参与首建/再平衡。
- **周报归档流程**：`signals.py` → 写/初始化 `flags.json` → `validate_flags.py` → `reports.py` → Web"历史周报/详情视图"渲染 `reports/<id>/report.json`；简报要带 `report_id`。
- **观察池规则**：`watchlist`（现 511880/511990/511360）只学习/监控，不影响权重、不触发再平衡；未经用户明确"纳入"不得用买/卖措辞。
- **投资边界**：仅 ETF 配置；不加个股推荐（若加只能先做观察/风险监控）；对用户的组合建议要：讲清假设、不承诺收益、优先小额分批、ETF-only、明确手动下单。

## 9. 回测口径与发现（数字随当前持仓而变）

- ETF 可交易段当前约 **2021-11 → 2026-06（~4.3 年，受科创50 2020 上市拖累交集）**；指数代理长段 **2006 → 2026（~19.6 年）**，长段剔除并分摊黄金/QDII/创业板/科创50（价格指数未含分红，主要看回撤轮廓、非精确收益）。
- **趋势过滤定位为"危机保险"非增收**：长样本里它把最大回撤从约 −42% 压到约 −24%，但平静期摊薄收益。`risk_profile=进取` 下趋势仅作展示信号 + `trend_alerts` 提醒，不自动调仓。
- **DCA 实测**（ETF 段 ~4.3 年、16 滚动窗口）：一次性 1.46x / 分6月 1.47x（56% 窗口跑赢一次性）/ 分12、24 月略逊，回撤均约 −12.8%——符合"上行市一次性通常更优、分批主要降择时后悔"。建仓别拖太久（6 个月一档已拿到大部分平滑效果）。

## 10. 变更历史（精简，最新在上）

- **本轮（重标定 + review + westock + 清理）**：universe 5→9、全组合风险口径、缓冲感知建议权重、DCA 回测、目标可行性、危机保险提醒、估值三态、ECharts 本地化、稳健桶可在设置里配、行情追踪全 universe、westock 质量兜底；review 修复（拦截文案口径/建议权重 1.01/长回测截断/P1-2 可见性）；删死函数 `renderGoalCoach`+`renderDecisionGuide`；个人配置与记录改为入库。测试 50→69。
- **更早**：月度复盘（守规则）、偏离复盘、压力贡献拆解、观察池学习系统、AI 旗标富渲染、ETF 折溢价/清盘提示、成交后持仓草稿、执行金额保护、依赖自检、前端从 ~18 板块重构为"决策区 + 5 标签页"并轻拆 html/css/js、`/api/portfolio/target-suggestion` 建议权重等。
- **初版**：周度信号引擎、回测引擎、结构化 AI 旗标、本地 Web 驾驶舱、可视化周报归档、执行记录。
