# 交接文档 / Project Handoff（单一权威文档）

> 本文件已合并原 `PLAN.md`（路线图）、`CHANGELOG.md`（变更史）、`HANDOFF.md`（项目状态）。
> **下一个接手的 agent：先完整读这份。** 个股推荐 / 高频 / 自动下单都不做。
>
> **硬边界**：教育/辅助决策工具——**输出建议、不构成投资建议、不承诺收益、不自动下单**；人在环，最终拍板与下单永远在用户手里；**ETF-only**；不编造数据（缺失就如实标"不可用/缺失"）。

---

## 0. 协作规则 / 单一事实源

- 核心代码只在 `engine/`。两个 agent 入口 `.claude/skills/weekly-briefing/SKILL.md`、`.agents/skills/weekly-briefing/SKILL.md` **只是薄包装**，不要把 `signals.py` / `backtest.py` / app 逻辑拷进 agent 目录。
- 改行为：**先改 `engine/` 实现**，再按需更新 `README.md` / 两个 SKILL（仅当接口变化）。
- 每改一处：跑 `python engine/tests/test_engine.py`（当前 **82 用例**，纯函数、无网络）必须全绿；前端改完 `node --check engine/web/app.js`。

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
- **westock（腾讯自选股）数据源——ETF 数据的【第一顺位·批量】**，三处用途都经 `npx -y westock-data-skillhub@1.0.3`（需 Node≥18 + `Bash(npx:*)` 放行；`.claude/settings.local.json` 本机已加、换机器要重加）。westock CLI 大多数命令支持**逗号分隔批量**（BatchResult，局部降级）——**整个看板只 2 次 npx**（kline + etf）覆盖全部 ETF，**不再逐只 shell**：
  1. **行情【首选源】（signals.py `fetch_hist`）**：顺序 **westock(腾讯,qfq) → 东财 → 新浪 → 缓存**。`prefetch_westock(codes)` 一次批量 `kline`（`_parse_westock_kline_batch`，现**保留 `amount` 列**→ DataFrame[date,close,amount]）填 `_WESTOCK_HIST`；signals `main()` 与 app.py 多码端点（market/quality/spot）都先预取再循环。westock `source="westock"`、按"完整"对待。OHLC 仅 2 位小数，对趋势/动量无碍。
  2. **ETF 质量层【首选·批量】（app.py，已从"兜底"反转为"优先"）**：`_prefetch_westock_etf(codes)` 一次批量 `etf` 取折溢价/规模/成交额/**QDII 申购状态**/**成立日**；`_quality_metrics` **westock 优先、akshare 快照(`fund_etf_spot_em`)兜底**（`extra.fallback=True` 表示退到 akshare）；**20 日成交额**从已批量的 kline `amount` 出（缺则 `_akshare_avg_turnover_20d`）；**上市年限**用 etf `establishDate`（`_years_since`，kline 320 日窗口不能当上市年限）；`_westock_covers_all()` 全覆盖时**跳过慢的 akshare 快照**。进程缓存 300s、失败返 None、不编造。⚠️ westock `etf` 接口本身偏不稳（盘后/限频常挂）——故 akshare 快照必须保留为兜底。
  3. **盘中实时价 `/api/etf/spot`（首页浮动盈亏用）**：同样 westock 优先（etf 详情 + kline 最新价），akshare 快照兜底。
  - backtest.py 未改（仍以 `engine/data/` 种子为主、`--refresh` 走东财/新浪），保持离线可复现。
- **前端**：`applyTargetSuggestion()` 从建议项构建持仓（含新升入品种、保留已有 shares）；`marketTrackCodes()` 让"行情与质量"追踪整个 universe；ECharts 本地优先 `/web/vendor/echarts.min.js` + CDN 兜底。
  - **周报渲染（统一）**：`renderWeeklyReport(s,{mode:'live'|'history',container,flags})` 是**唯一**渲染器——常驻区「本周决策」`#weeklyReportLive`（live，含可勾选待办）与复盘标签 `#reportDetailPanel`（history，只读）共用。分**必看/可看/背景**三档（`.wk-must/.wk-why/.wk-bg/.wk-sec`）：一句话结论+本周该做什么+危机提醒 / 持仓信号表+动量图+目标可行性+旗标+纪律+拦截+首建 / 观察池+数据口径。动量图按 mode 隔离 id（`reportMomentumChart-live|-history`）、重渲染前 dispose 防 `ECHARTS[]` 泄漏。改这块**别再恢复**旧的 `renderSignals`/`renderReportDetail` 双份渲染或 `#sigbox`/`#decisionCard`（已删）。
  - **浮动盈亏（app.js `costBasisByCode`/`portfolioValueRows`）**：成本基 = **均价 × 当前持有份额**（不是累加执行记录净额）——自我纠正重复/手填持仓导致的假浮亏；无买入记录→「成本未知」、执行份额≠持仓→⚠ 估算。调仓 `confirmRebalance` 登记前对**近 7 天相同成交**软提示（`recentDuplicateItems`，不硬拦）。
- **校验约束**：`validate_strategy` 要求 universe **有且仅有一个 `asset:bond`**；watchlist 与 universe 不得重复。
- ECharts 实例经 `initChart()` 注册到 `ECHARTS[]`，`activateTab` 调 `resizeCharts()`；`static_folder=None`，只服务 `/` 与 `/web/<path>`。

## 4. 已完成（P0 / P1 / P2 全部落地并验证；82 测试全绿）

- **P0 重标定 + 全组合 + 拓宽菜单**：universe 5→9（加 513500 global_equity、513100 global_growth、159915+588000 china_growth），watchlist 收到现金/短债 3 只；全组合风险预算 + 缓冲感知建议权重；门槛重标定（max_weekly 1万→5万、first_tranche 0.25→0.15）。
- **P1 分批与可行性**：DCA 分批建仓回测（前端"建仓路径对比"图+表）；目标可行性体检（`expected_etf_return` vs 目标 + 缺口）；危机保险提醒（`trend_alerts` 权益跌破 MA200）。
- **P2 覆盖与工程**：估值"不适用 vs 缺失(非中性)"区分；ECharts 本地化；编辑设置可配置稳健桶；新 ETF 纳入行情与质量追踪；westock 质量兜底。
- **整体 review 修复**（多 agent 对抗审查后逐条核实）：拦截文案改全组合口径；建议权重 1.01 修复；长回测截断修复；P1-2 可行性从死代码挪到 `renderSignals`；删除死函数 `renderGoalCoach`/`renderDecisionGuide`；补正注释与示例档案。

## 5. 待办 / 开放问题（下一个 agent 从这里继续）

1. **P2-2 真实业绩跟踪**（暂缓，有依据）：诚实业绩须用**资金/时间加权收益（TWR/MWR）剔除分批注入现金流**，否则把"持续注入本金"显示成"收益"会误导。前置：① 每日（或每周报时）落一份组合 NAV 快照；② 记录外部现金流；③ 算 TWR/MWR 再对比基准。当前"浮动盈亏 + 月度守规则复盘"已覆盖诚实子集。
2. **A 股成长估值接入**：红利低波/创业板/科创50 现为 `valuation_missing`。`创业板指`/`科创50`/`中证红利` **不是** `ak.stock_index_pe_lg` 合法符号（实测 KeyError），需找到可用 PE 分位源再接，别硬塞（会"永远取数失败"误标缺失）。
3. **ETF 替代候选比较**：需可靠的费率/跟踪误差/同类清单数据源（westock 的 `etf` 给管理费/托管费，可作起点）。
4. **⚠️ QDII 溢价实盘提醒（直接影响用户）**：用户组合含 513500 标普500(21%) + 513100 纳指(13%) 两只 QDII。质量层已 **westock 批量**给出该警告（近测 **513500 +4.88% / 513100 +6.7%、均"不可申购"** → 敏感品种 issue/【不足】）。建议这两只 QDII **先缓、等溢价≤1.5% 或恢复申购再买**；每次建仓前看"折溢价/申购状态"。
5. **前端浏览器可视化验证**：本机 Preview MCP 这阶段起不来（环境把 python 指到 Xcode 的、权限被拒，与代码无关）。UI 改动目前靠 **"抽真实渲染代码 + 真实数据跑 + `node --check` + Flask test_client 验证 HTTP 交付"**；可视化验收需用户硬刷新浏览器自查，或修好 launch/preview 环境。
6. **取数稳定性现状**：**行情首选 westock(腾讯)，再东财→新浪→缓存**；**ETF 质量/实时价也已 westock 批量优先、akshare 快照兜底**——但 westock `etf` 接口偏不稳（盘后/限频常返"执行失败"），故 akshare 兜底必须保留、`_westock_covers_all` 控制是否跳过慢快照。**估值**仍只走 akshare/legulegu（`stock_index_pe_lg`），legulegu **较脆**——westock 不提供 PE 分位，估值备用源仍是开放项。可考虑：给估值加备用源、或养"每日刷新缓存"的健康检查、或把 westock 行情也接进 backtest `--refresh`。

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

- **本轮（周报重排 + 浮亏修复 + westock 批量优先）**：① 周报统一三档渲染器 `renderWeeklyReport`（live/history 共用、必看/可看/背景、去"渲染两遍"冗余、零丢失），常驻区两卡合一为「本周决策」；② 浮动盈亏改 **均价×持仓** 成本基（修假浮亏）+ 调仓近 7 天软查重 + 删一条重复执行记录；③ westock 反转为 **ETF 数据第一顺位且批量**（kline 带 amount、批量 etf 预取、质量/实时价/20日成交额 westock 优先、`_westock_covers_all` 跳过慢快照、上市年限用成立日）。另：`/api/portfolio/preview` 成交后持仓预览、调仓向导（模态 3 步）、`start_mac.command` 端口接管。测试 69→82。
- **上一轮（重标定 + review + westock + 清理）**：universe 5→9、全组合风险口径、缓冲感知建议权重、DCA 回测、目标可行性、危机保险提醒、估值三态、ECharts 本地化、稳健桶可在设置里配、行情追踪全 universe、westock 质量兜底；review 修复（拦截文案口径/建议权重 1.01/长回测截断/P1-2 可见性）；删死函数 `renderGoalCoach`+`renderDecisionGuide`；个人配置与记录改为入库。测试 50→69。
- **更早**：月度复盘（守规则）、偏离复盘、压力贡献拆解、观察池学习系统、AI 旗标富渲染、ETF 折溢价/清盘提示、成交后持仓草稿、执行金额保护、依赖自检、前端从 ~18 板块重构为"决策区 + 5 标签页"并轻拆 html/css/js、`/api/portfolio/target-suggestion` 建议权重等。
- **初版**：周度信号引擎、回测引擎、结构化 AI 旗标、本地 Web 驾驶舱、可视化周报归档、执行记录。
