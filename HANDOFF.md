# 交接文档 / Project Handoff（当前真相 + 开放待办 · 单一权威源）

> **下一个接手的 agent：先读这份**——当前权威状态 §0A + 关键不变量 §3 + 开放待办 §5。个股推荐 / 高频 / 自动下单都不做。
> **历史不在这里**：变更史、已闭环审计（原 §0B）、已完成的提升计划（原 §0C / §4）已移到 [`HISTORY.md`](HISTORY.md)（只增、极少读）；设计规范见 [`STRATEGIC_ALLOCATION_DESIGN.md`](STRATEGIC_ALLOCATION_DESIGN.md) / [`TACTICAL_ALLOCATION_DESIGN.md`](TACTICAL_ALLOCATION_DESIGN.md)（参考，非当前状态）。
>
> **定位**：**私人投顾（单一所有者自用）**——**输出带理由的建议、不承诺收益、不自动下单**；人在环，最终拍板与下单永远在所有者手里；**ETF-only**；不编造数据（缺失就如实标“不可用/缺失”）。（自用工具、不对外提供投顾服务，不涉及“类投顾”合规边界；个股推荐/高频/自动下单仍不做。）

---

## 0. 协作规则 / 单一事实源

- 核心代码只在 `engine/`。两个 agent 入口 `.claude/skills/weekly-briefing/SKILL.md`、`.agents/skills/weekly-briefing/SKILL.md` **只是薄包装**，不要把 `signals.py` / `backtest.py` / app 逻辑拷进 agent 目录。
- 改行为：**先改 `engine/` 实现**，再按需更新 `README.md` / 两个 SKILL（仅当接口变化）。
- 每改一处：跑 `$env:UV_CACHE_DIR='F:\MakeMoney\.uv-cache'; uv run --offline --with-requirements engine\requirements.txt python -m unittest engine.tests.test_engine`（当前 **329 用例**）必须全绿；前端改完 `node --check engine/web/app.js`。

## 0A. 2026-06-07 当前权威状态

以下内容覆盖本文后续章节中仍保留的旧流程描述。

- 长期战略已经收敛为一条权威路径：保存长期战略设置后，系统自动计算场外稳健桶与本工具计划最大使用金额，构建模型组合，并在通过约束时直接应用。旧的“建议目标权重”、手动覆盖建议、季度墙和影子组合审查均已移除。
- 当前长期参数：总资金 170 万元、目标年化 7%、规划年限 30 年、最大回撤约束 30%、失业月开销 6000 元、失业缓冲 5 年、压力后储备 12 个月。由此自动计算场外稳健桶 43.2 万元，本工具计划最大使用金额 126.8 万元。
- 当前已应用的目标权重（**以 `portfolio.yaml` 为准**）：`511010` 25%、`510300` 15%、`510500` 15%、`512890` 5%、`518880` 5%、`513500` 20%、`513100` 5%、`159915` 5%、`588000` 5%。（早先一版 construct 输出把红利低波/黄金砍到 0%，与"黄金/红利绝不为腾权重而砍"的所有者决策冲突、已废弃。）
- 币种集中约束已改为 `single_risk_currency_exposure_max`，只约束风险资产的单一币种暴露，债券/现金类资产不计入。
- “当前 ETF 是否合适”已补全候选引入闭环：同资产类别的 universe/watchlist ETF 可作为替代候选；候选必须通过最近一次产品准入审查，之后可经 `/api/strategic/roles/introduce` 引入对应战略角色。目前没有符合条件的替代候选。
- “复杂策略是否值得保留”已改为“模型组合是否优于简单组合”，结果顶部直接给出保留复杂度、建议简化或证据不足的结论。当前回测结论倾向“建议简化”。
- “决策与组合”启动时自动加载配置和行情；首页按“我的组合 → 本周决策 → 调仓记录”纵向排列，顶部数据改为小字状态说明，组合表展示目标 ETF 与已购买 ETF，仅保留一个“调仓”入口。
- `start_windows.bat` 与 `start_mac.command` 会在启动前清理占用 5057 端口的旧 dashboard 进程；若端口由无关程序占用则停止启动，不会误杀。
- `journal/strategic_reviews/` 与新的时间戳 `reports/` 目录属于历史/诊断生成物，不再是产品主流程的一部分；除非明确需要同步诊断快照，否则不要纳入提交。

## 1. 用户真实定位（所有标定的依据）

> **口径以 live 配置为准（2026-06-08 所有者拍板）**：下表与本节数字已对齐 `investor_profile.yaml` / strategic 自动算出的 live 值；§0A 为权威来源。旧的 100万/70万/8%/20% 已废弃。

| 项目 | 取值（live） |
|---|---|
| 总资金 | **170 万** |
| ETF 计划最大使用金额 | **126.8 万**（strategic 由总资金−稳健桶自动算出；慢慢分批，是上限不是目标） |
| 场外稳健桶 | **43.2 万**（strategic 自动算出：失业月开销6000×缓冲5年+压力后储备12月；活期/固收/定存，只让算法"知道有"） |
| 目标年化 | **7%**（针对 ETF 桶"做工的钱"，非全组合承诺、非保证） |
| 最大可接受回撤 | **30%**（全组合 170 万口径） |
| 经验 / 节奏 | intermediate；约 **12–24 个月**边学边投，按估值与学习里程碑调速 |

**核心策略洞察（整个重标定的灵魂）**：43.2 万稳健桶是"安全垫"——正因为有它，126.8 万 ETF 桶才能更激进去够 7%，同时把全组合回撤压在 30% 内。
**必须诚实保留**：7% 即便对 ETF 桶也偏进取（回测 ETF 段约 4–6%、长代理段约 8% 但伴 −40%+ 回撤）；且 HISTORY §0C #1 标定显示 **2008 级尾部下 ETF 桶 −38%、按计划满仓折算全组合 −28.5%，逼近 30% 预算**——需权益重仓 + 容忍股票级波动，工具要把权衡量化讲清，绝不暗示稳赚。

**当前账户状态**：
- `portfolio.yaml`（按资产类聚合）：bond(国债) 25% / equity(沪深300) 30% / global_equity(标普) 20% / china_growth(创业板+科创50) 10% / equity_defensive(红利低波) 5% / gold 5% / global_growth(纳指) 5%。ETF 桶权益约 70%；单情景全组合压力 17.5%、**2008 级最坏 28.5%**（live 预算 30% 内）。
- `investor_profile.yaml`（live）：target 0.07 / max_dd 0.30 / experience intermediate / stable_assets_outside 432000 / stable_assets_yield 0.02 / planned_etf_capital 1268000 / emergency_cash 0 / horizon 30y。
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
- **两道风险闸（把"风险提示"接进"动作/权重"，都只作用于买入侧）**：
  1. **执行质量闸** `_apply_execution_quality_gate`（app.py，`run_signals` 内、归档前）：对**买入类**动作（加仓 `suggest=add` / 首建 orders）按**实时折溢价 + 申购状态**裁决——纯函数 `_exec_quality_decision`（复用 `_classify_premium`/`_purchase_status_note`，敏感品种溢价≥1.5% 或不可/暂停申购=issue）。issue→`actionable=False`+补 `blocked_reasons`（移入拦截区）；warn/缺失→挂 `exec_quality_note`（**缺失≠中性、不硬拦，只提示自查**）。**只改买入、卖出不动**；回写 `signals.json` 使 `current_suggestions`/调仓建议同口径；`archive_report(signals=...)` 用加工后的 signals 归档（复盘与实时一致）。
  2. **政策闸** `_apply_policy_gate`（app.py，`/api/portfolio/target-suggestion` 内）：仅「**类别=政策风险 且 方向=利空 且 置信度=高**」(`_policy_restricted_codes`) 命中→**冻结其建议权重≤当前**（不建议加仓、允许引擎自身减配），释放权重按比例分给未受限项、合计≈1，打 `policy_restricted/policy_note/policy_gated`；`?ignore_policy=1` 一键忽略。**平时无此 flag → 休眠不打扰**。
- **两层把关分工（2026-06-08 厘清，勿再混）**：**长期战略层**(§8.2 `hard_admission`)只看**结构性**质量——规模/流动性/费率/上市年限/容量 + **限购**(限购真的让你加不进去→冻结不加)；**折溢价不在长期准入里**（status=info、不计入 admitted）。**折溢价/实时申购是执行时点问题**，只由上面的「执行质量闸」在下单/调仓时把关。别再把实时折溢价塞回 §8.2——会把 30 年战略耦合到一个瞬时报价、且非交易时段（陈旧折价）误判全盘"不合格"。另：`_is_trading_session()` + `_etf_quality_for(realtime_reliable=)` 在非盘中置空折溢价（仅展示诚实，已不影响准入）。
- **前端**：`applyTargetSuggestion()` 从建议项构建持仓（含新升入品种、保留已有 shares）；`marketTrackCodes()` 让"行情与质量"追踪整个 universe；ECharts 本地优先 `/web/vendor/echarts.min.js` + CDN 兜底。
  - **周报渲染（统一）**：`renderWeeklyReport(s,{mode:'live'|'history',container,flags})` 是**唯一**渲染器——常驻区「本周决策」`#weeklyReportLive`（live，含可勾选待办）与复盘标签 `#reportDetailPanel`（history，只读）共用。分**必看/可看/背景**三档（`.wk-must/.wk-why/.wk-bg/.wk-sec`）：一句话结论+本周该做什么+危机提醒 / 持仓信号表+动量图+目标可行性+旗标+纪律+拦截+首建 / 观察池+数据口径。动量图按 mode 隔离 id（`reportMomentumChart-live|-history`）、重渲染前 dispose 防 `ECHARTS[]` 泄漏。改这块**别再恢复**旧的 `renderSignals`/`renderReportDetail` 双份渲染或 `#sigbox`/`#decisionCard`（已删）。
  - **浮动盈亏（app.js `costBasisByCode`/`portfolioValueRows`）**：成本基 = **均价 × 当前持有份额**（不是累加执行记录净额）——自我纠正重复/手填持仓导致的假浮亏；无买入记录→「成本未知」、执行份额≠持仓→⚠ 估算。调仓 `confirmRebalance` 登记前对**近 7 天相同成交**软提示（`recentDuplicateItems`，不硬拦）。
- **校验约束**：`validate_strategy` 要求 universe **有且仅有一个 `asset:bond`**；watchlist 与 universe 不得重复。
- ECharts 实例经 `initChart()` 注册到 `ECHARTS[]`，`activateTab` 调 `resizeCharts()`；`static_folder=None`，只服务 `/` 与 `/web/<path>`。

## 5. 待办 / 开放问题（下一个 agent 从这里继续）

### P0：统一“决策周期”，解决多状态源不一致

产品机制审查结论：这个工具本质上不是普通行情看板，而是一个**低频、人在环的投资决策状态机**：

`策略/个人目标 + 当前持仓 + 行情数据 → 生成本周建议 → 人工判断/券商下单 → 登记真实成交 → 更新持仓 → 纪律复盘`

目前页面看似是一套系统，实际上同时维护至少五种“当前状态”：

1. **配置当前态**：`portfolio.yaml` / `investor_profile.yaml` / `strategy.yaml`
2. **最近周报快照**：`reports/<id>/report.json` 中生成时的价格、现金、建议和风险判断
3. **当前建议态**：`engine/signals.json`，由 `/api/executions` 的 `current_suggestions()` 读取
4. **当前行情态**：页面异步拉取的日 K、实时价、ETF 质量与浏览器行情缓存
5. **执行事实态**：`journal/executions/*.json`；另有浏览器 `localStorage` 手动待办状态

这些状态尚未统一成一个明确的“本周决策周期”，是当前“不顺手”的主要根因。已实际走查确认：

- **首页与调仓可能读取不同建议版本**：首页载入最新归档周报，但调仓建议读取 `signals.json`。走查时最新周报组合价值为 `¥31,244.51`，`signals.json` 为 `¥31,238.51`。
- **已完成动作仍会再次带入调仓**：一份周报共 9 项建议，其中 4 项已登记成交；点击“使用本周建议”仍带入全部 9 项，需要手动删除已完成项。
- **执行前不会重新验证当前交易质量**：走查时行情质量页显示标普500ETF溢价约 `5.33%`、纳指ETF约 `6.25%`，且均为不可申购；调仓仍会带入旧建议。执行质量闸只在生成信号时运行，未在准备执行时重验。
- **成交登记不是原子操作**：当前先保存 execution，再调用 preview 并保存 config；若后半段失败，会形成“已登记成交、持仓未更新”的中间状态。
- **月度复盘被重复生成的周报放大**：当前 2026-06-03 至 2026-06-05 共 10 份周报；同日重复建议均累计进“建议动作/计划投入”，复盘统计不能代表唯一正式计划。
- **战略配置与本周执行混在同一入口层**：“生成建议权重”属于低频战略配置；“本周决策/调仓”属于执行层，应分离使用场景。

建议建立统一的活动决策周期对象：

```text
decision_cycle
├── id / status / created_at / superseded_at
├── portfolio_version / strategy_version / investor_profile_version
├── data_as_of / data_quality
├── recommendation snapshot
├── execution-quality recheck
├── actions
│   ├── pending
│   ├── blocked_now
│   ├── executed
│   ├── skipped
│   └── expired
└── linked execution records
```

推荐实施顺序：

1. 首页、本周决策、调仓统一读取同一个活动决策周期，不再分别读取最新 report 与 `signals.json`。
2. 调仓只带入尚未完成、未过期且当前仍可执行的动作。
3. 打开调仓或进入确认步骤时，重新检查折溢价、申购状态、数据新鲜度和最新持仓偏离。
4. 将成交登记与持仓更新合并成后端单一事务接口；失败时不得留下半完成状态。
5. 新周期生成后，将旧活动周期标为 superseded/expired；历史周报继续只读保留。
6. 月度复盘按“正式决策周期”去重统计，而不是累计每次刷新生成的周报。
7. 将“建议目标权重”归入独立的月度/季度策略审视流程，不与每周执行入口混放。

**阶段 1 已实现（2026-06-06）**：

- 直接复用 `reports/<id>/report.json` 作为决策周期事实源；新周报标记 `cycle_status=active`，上一活动周期标为 `superseded`；旧数据兼容为“最新周报即活动周期”。
- `/api/executions` 与调仓建议不再读取 `signals.json`，统一从活动周期派生；只返回尚未完成的动作，并按 `report_id + code + side` 关联真实成交。
- 首页“可执行”在加载活动周期后显示剩余动作数；走查中从原周报总数 9 正确变为剩余 5。
- 打开调仓时使用快速实时源重验折溢价/申购状态；`blocked_now` 不带入调仓，warn 直接显示在对应成交行；快速源缺失时提示自查，不调用慢速 AkShare 全市场快照阻塞窗口。
- 调仓确认改为 `/api/decision-cycle/execute` 单一事务接口：验证活动周期与动作状态、登记成交并更新持仓；失败时回滚刚写入的执行记录，避免半完成状态。
- 月度复盘每个自然日只采用最后一份正式决策周期，避免同日重复刷新放大建议动作与计划金额。
- 新增相关回归测试；当前 `engine/tests/test_engine.py` 共 **105 项全绿**。

**阶段 2 已实现（2026-06-06）**：

- 新决策周期写入 `portfolio_version / strategy_version / investor_profile_version` 内容指纹；打开调仓与确认执行时检测配置是否已变化。手动编辑配置或应用新目标权重后，旧周期会提示失效并阻止按旧建议执行；正常成交更新持仓后会自动推进周期的持仓版本，不误判为手动改配置。
- 新增 `journal/decisions/<cycle_id>.json` 周期决策日志与 `/api/decision-cycle/action`：每条建议可明确“跳过本周期 / 否决建议”，记录原因并从待执行列表移除；也可恢复为待处理。月度复盘会汇总这些未执行原因。
- “建议目标权重”已从首页日常组合入口移到“复盘与历史 → 策略审视（月度 / 季度）”，并使用独立 `/api/strategy-review/target-suggestion` 入口；应用新目标权重后明确要求重新生成周度信号。

1. ✅ **P2-2 真实业绩跟踪（已落地为 WS3，HISTORY §0C #6 收尾）**：`reports.save_nav_snapshot`（每周报落 `journal/nav/`）+ `compute_twr`/`compute_mwr` + `performance_summary`（剔除注入本金、沪深300 基准、费用单列、诚实注脚）+ `GET /api/performance` + `#performancePanel`；已接进证据台账 `live_track_record` 行（≥8 周快照点亮 `live` 档）。时钟自 2026-06-07 在走。详见 HISTORY §0C #6。~~暂缓~~ 说法作废。
2. ✅ **已接入（2026-06-08，两条腿方案）** **A 股成长估值**：选源核查——`stock_index_pe_lg`(legulegu) 确认不收 创业板指/科创50/中证红利低波(KeyError)；百度 `stock_zh_valuation_baidu` 把指数码当**同名个股**返回(陷阱:中证红利"PE"35 实为佳电股份,官方仅 8.48)、已排除；csindex 官方有精确当前 PE 但静态文件只滚动 ~20 天、**算不出长分位**。落地：① **创业板 159915 → 创业板50 代理**(legulegu 2009~、强相关；config `valuation_proxy`；UI 标"代理·近似"，**可触发 cheap/rich**)；② **科创50 588000 / 红利低波 512890 → csindex 官方按日自建累积**(config `valuation_csindex`=000688/H30269；`fetch_valuation_csindex` 把 ~20 天窗口并进 `journal/valuation/<code>.json` 按日去重、**时钟从接入日(2026-06-08)起走**；历史 < `VALUATION_ACCUM_MIN_YEARS`(3 年) → `valuation_accumulating` 态：**只显示当前 PE + "分位积累中(N 月)"、percentile=None、绝不冒充信号**；满 3 年后自动升级为自身精确分位)。前端 `valCell` 渲染 代理/积累 两态 + hover tooltip 说明数据历史(已 Preview 真机验)。测试 311→**316**(+5)。**注**：创业板指 csindex 也 404(深证指数)故只能代理；csindex 市盈率1=PE-TTM(对照 legulegu 沪深300 13.7 验)。
3. ✅ **已接入（2026-06-09，自动匹配·人工确认）** **同类 ETF 发现**：费率(`_etf_fee`/`fund_fee_em`)、跟踪误差(`_etf_tracking_dispersion`)、规模(westock etf)本已逐只可取；缺的「同类清单」——选源核查：xq 详情接口坏(`KeyError 'data'`)、名称模糊匹配**过匹配**(搜"沪深300"含红利/价值/增强=不同指数)、`fund_etf_spot_em` 无费率/规模列。**落地**：`_etf_spot_list`(全市场清单 ~30s/14 页 → 当日文件缓存 `cache/etf_spot_list.json`) + `_name_matches_peer`(用『<关键词>ETF』**精确子串**、且匹配位前字符非数字以排除"300红利低波"这类不同指数) + `_etf_peers`(按成交额取前 N、拉费率、**费率升序**、incumbent 永远纳入) + `GET /api/etf/peers`；前端 incumbent 表每行「找同类」按钮 → 费率/流动性对比，明确标"**自动匹配·需人工确认 / 研究发现非动作**"，满意的加 watchlist 走既有准入闭环。测试 +3，Preview 真机验。**实测**：510300 → 25 只沪深300同类全 0.20%(已商品化、无更便宜的)。
4. ✅ **已落地（2026-06-09）** QDII 政策闸：① **执行质量闸**（溢价≥1.5%/不可·暂停申购，实时）早已就位（见 §3）。② **前瞻政策闸（本轮新建——此前其实是 no-op！）**：核查发现旗标此前**只 validate+展示、从不拦动作**（旧 HANDOFF 称"政策闸已就位"不实，flags 只进 `archive_report` 当展示）。已落地 `_policy_flag_blocks` 接进 `_apply_execution_quality_gate`：买入动作命中『**政策风险/流动性风险 · 利空 · actionable**』旗标（`affected_assets` 含该 code 或 `ALL`）→ 强制 `actionable=False` 暂缓、附旗标标题为由。**顺手修了 `load_json` 未从 reports 导入** 的 bug（致 #3 spot 缓存读 + 本闸读 flags 静默 NameError 进 except——#3 的"当日缓存"此前其实没生效）。测试 +2。③ **已写入真旗标 + 实测**：查证 513100 国泰纳指ETF **因 QDII 额度自 2024 持续暂停申购** + 基金公司 2026-05-26 官方溢价风险提示 → 写一条 `政策风险/利空/高`(513100) 旗标（`validate_flags` ✓），**实测加仓 513100 被前瞻闸暂缓**。⚠️ `flags.json` 不入库、每次 `/周报` 重写 → 后续 briefing 研究环节应保留同类已查证旗标。
5. ✅ **前端浏览器可视化验证（2026-06-08 已可用）**：Preview MCP 现可启动——`.claude/launch.json` 已有 `dashboard` 配置（`PORT=5090 python3 engine/app.py`），`preview_start name=dashboard` → `preview_eval`(`activateTab('review')`/scrollIntoView) → `preview_screenshot`/`preview_console_logs` 即可真机验收（已用此法验过线性流程步骤条）。`node --check` + Flask test_client 仍作快速回归。（旧记录"起不来"已失效。）
6. **取数稳定性现状**：**行情首选 westock(腾讯)，再东财→新浪→缓存**；**ETF 质量/实时价也已 westock 批量优先、akshare 快照兜底**——但 westock `etf` 接口偏不稳（盘后/限频常返"执行失败"），故 akshare 兜底必须保留、`_westock_covers_all` 控制是否跳过慢快照。**估值**仍只走 akshare/legulegu（`stock_index_pe_lg`），legulegu **较脆**——westock 不提供 PE 分位，估值备用源仍是开放项。可考虑：给估值加备用源、或养"每日刷新缓存"的健康检查、或把 westock 行情也接进 backtest `--refresh`。
   - **（2026-06-08 刷新提速）** ① **缓存跳过**：`_latest_completed_session()`(工作日 ≥15:30→今天 / 否则上一交易日；周末→周五；**节假日不识别，最坏多拉一次、绝不返回过期数据**)判定缓存是否已达最新已收盘交易日；达到则 `fetch_hist`/`fetch_valuation_pct` 直接用缓存(source=`cache_current`，**按"完整"对待——不计 used_cache、不挡交易，因它就是该交易日定稿价**)、`prefetch_westock` 只对"落后"的 code 跑 npx；csindex 当天已拉(`fetched_at==today`)跳过 Excel 重下。② **per-ETF 取数并行**(`ThreadPoolExecutor`，各写独立缓存文件、无竞争)。**实测刷新 4.9s→1.47s**(全缓存命中)。失败方向永远偏"拉"。③ **已删**前端每 10 分钟行情自动刷新(`MARKET_TIMER`)。测试 316→**321**(+5)。

## 6. 数据与文件（gitignore 现状）

- **现已入库**（私人仓，用户确认无隐私风险）：`portfolio.yaml`、`investor_profile.yaml`（配置）；`reports/`（周报归档）、`journal/`（执行/学习记录）。
- **一键同步脚本**（换机器/多机用）：根目录 `sync.command`（mac 双击）/ `sync.bat`（Windows 双击）→ 只 `git add` 上述个人数据后 commit + `pull --rebase` + push；`signals.json`/`flags.json` 不同步（本地重算）。`.gitattributes` 固定 `*.bat`=CRLF、`*.command`=LF。
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
- **周报归档流程**：`signals.py` → 写/初始化 `flags.json` → `validate_flags.py` → `reports.py` → Web"历史周报/详情视图"渲染 `reports/<id>/report.json`；简报要带 `report_id`。归档只写**紧凑 `report.json`**（不缩进、约 15KB）；**不再落盘 `report.md`**（Web 由 json 重渲染，`render_report_md()` 仍保留供按需导出）。
- **观察池规则**：`watchlist`（现 511880/511990/511360）只学习/监控，不影响权重、不触发再平衡；未经用户明确"纳入"不得用买/卖措辞。
- **投资边界**：仅 ETF 配置；不加个股推荐（若加只能先做观察/风险监控）；对用户的组合建议要：讲清假设、不承诺收益、优先小额分批、ETF-only、明确手动下单。

## 9. 回测口径与发现（数字随当前持仓而变）

- ETF 可交易段当前约 **2021-11 → 2026-06（~4.3 年，受科创50 2020 上市拖累交集）**；指数代理长段 **2006 → 2026（~19.6 年）**，长段剔除并分摊黄金/QDII/创业板/科创50（价格指数未含分红，主要看回撤轮廓、非精确收益）。
- **趋势过滤定位为"危机保险"非增收**：长样本里它把最大回撤从约 −42% 压到约 −24%，但平静期摊薄收益。`risk_profile=进取` 下趋势仅作展示信号 + `trend_alerts` 提醒，不自动调仓。
- **DCA 实测**（ETF 段 ~4.3 年、16 滚动窗口）：一次性 1.46x / 分6月 1.47x（56% 窗口跑赢一次性）/ 分12、24 月略逊，回撤均约 −12.8%——符合"上行市一次性通常更优、分批主要降择时后悔"。建仓别拖太久（6 个月一档已拿到大部分平滑效果）。
