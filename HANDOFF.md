# 交接文档 / Project Handoff（单一权威文档）

> 本文件已合并原 `PLAN.md`（路线图）、`CHANGELOG.md`（变更史）、`HANDOFF.md`（项目状态）。
> **下一个接手的 agent：先完整读这份。** 个股推荐 / 高频 / 自动下单都不做。
>
> **定位**：**私人投顾（单一所有者自用）**——**输出带理由的建议、不承诺收益、不自动下单**；人在环，最终拍板与下单永远在所有者手里；**ETF-only**；不编造数据（缺失就如实标"不可用/缺失"）。（自用工具、不对外提供投顾服务，不涉及"类投顾"合规边界；个股推荐/高频/自动下单仍不做。）

---

## 0. 协作规则 / 单一事实源

- 核心代码只在 `engine/`。两个 agent 入口 `.claude/skills/weekly-briefing/SKILL.md`、`.agents/skills/weekly-briefing/SKILL.md` **只是薄包装**，不要把 `signals.py` / `backtest.py` / app 逻辑拷进 agent 目录。
- 改行为：**先改 `engine/` 实现**，再按需更新 `README.md` / 两个 SKILL（仅当接口变化）。
- 每改一处：跑 `python engine/tests/test_engine.py`（当前 **186 用例**，纯函数、无网络）必须全绿；前端改完 `node --check engine/web/app.js`。

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
- **两道风险闸（把"风险提示"接进"动作/权重"，都只作用于买入侧）**：
  1. **执行质量闸** `_apply_execution_quality_gate`（app.py，`run_signals` 内、归档前）：对**买入类**动作（加仓 `suggest=add` / 首建 orders）按**实时折溢价 + 申购状态**裁决——纯函数 `_exec_quality_decision`（复用 `_classify_premium`/`_purchase_status_note`，敏感品种溢价≥1.5% 或不可/暂停申购=issue）。issue→`actionable=False`+补 `blocked_reasons`（移入拦截区）；warn/缺失→挂 `exec_quality_note`（**缺失≠中性、不硬拦，只提示自查**）。**只改买入、卖出不动**；回写 `signals.json` 使 `current_suggestions`/调仓建议同口径；`archive_report(signals=...)` 用加工后的 signals 归档（复盘与实时一致）。
  2. **政策闸** `_apply_policy_gate`（app.py，`/api/portfolio/target-suggestion` 内）：仅「**类别=政策风险 且 方向=利空 且 置信度=高**」(`_policy_restricted_codes`) 命中→**冻结其建议权重≤当前**（不建议加仓、允许引擎自身减配），释放权重按比例分给未受限项、合计≈1，打 `policy_restricted/policy_note/policy_gated`；`?ignore_policy=1` 一键忽略。**平时无此 flag → 休眠不打扰**。
- **前端**：`applyTargetSuggestion()` 从建议项构建持仓（含新升入品种、保留已有 shares）；`marketTrackCodes()` 让"行情与质量"追踪整个 universe；ECharts 本地优先 `/web/vendor/echarts.min.js` + CDN 兜底。
  - **周报渲染（统一）**：`renderWeeklyReport(s,{mode:'live'|'history',container,flags})` 是**唯一**渲染器——常驻区「本周决策」`#weeklyReportLive`（live，含可勾选待办）与复盘标签 `#reportDetailPanel`（history，只读）共用。分**必看/可看/背景**三档（`.wk-must/.wk-why/.wk-bg/.wk-sec`）：一句话结论+本周该做什么+危机提醒 / 持仓信号表+动量图+目标可行性+旗标+纪律+拦截+首建 / 观察池+数据口径。动量图按 mode 隔离 id（`reportMomentumChart-live|-history`）、重渲染前 dispose 防 `ECHARTS[]` 泄漏。改这块**别再恢复**旧的 `renderSignals`/`renderReportDetail` 双份渲染或 `#sigbox`/`#decisionCard`（已删）。
  - **浮动盈亏（app.js `costBasisByCode`/`portfolioValueRows`）**：成本基 = **均价 × 当前持有份额**（不是累加执行记录净额）——自我纠正重复/手填持仓导致的假浮亏；无买入记录→「成本未知」、执行份额≠持仓→⚠ 估算。调仓 `confirmRebalance` 登记前对**近 7 天相同成交**软提示（`recentDuplicateItems`，不硬拦）。
- **校验约束**：`validate_strategy` 要求 universe **有且仅有一个 `asset:bond`**；watchlist 与 universe 不得重复。
- ECharts 实例经 `initChart()` 注册到 `ECHARTS[]`，`activateTab` 调 `resizeCharts()`；`static_folder=None`，只服务 `/` 与 `/web/<path>`。

## 4. 已完成（P0 / P1 / P2 全部落地并验证；95 测试全绿）

- **P0 重标定 + 全组合 + 拓宽菜单**：universe 5→9（加 513500 global_equity、513100 global_growth、159915+588000 china_growth），watchlist 收到现金/短债 3 只；全组合风险预算 + 缓冲感知建议权重；门槛重标定（max_weekly 1万→5万、first_tranche 0.25→0.15）。
- **P1 分批与可行性**：DCA 分批建仓回测（前端"建仓路径对比"图+表）；目标可行性体检（`expected_etf_return` vs 目标 + 缺口）；危机保险提醒（`trend_alerts` 权益跌破 MA200）。
- **P2 覆盖与工程**：估值"不适用 vs 缺失(非中性)"区分；ECharts 本地化；编辑设置可配置稳健桶；新 ETF 纳入行情与质量追踪；westock 质量兜底。
- **整体 review 修复**（多 agent 对抗审查后逐条核实）：拦截文案改全组合口径；建议权重 1.01 修复；长回测截断修复；P1-2 可行性从死代码挪到 `renderSignals`；删除死函数 `renderGoalCoach`/`renderDecisionGuide`；补正注释与示例档案。

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

1. **P2-2 真实业绩跟踪**（暂缓，有依据）：诚实业绩须用**资金/时间加权收益（TWR/MWR）剔除分批注入现金流**，否则把"持续注入本金"显示成"收益"会误导。前置：① 每日（或每周报时）落一份组合 NAV 快照；② 记录外部现金流；③ 算 TWR/MWR 再对比基准。当前"浮动盈亏 + 月度守规则复盘"已覆盖诚实子集。
2. **A 股成长估值接入**：红利低波/创业板/科创50 现为 `valuation_missing`。`创业板指`/`科创50`/`中证红利` **不是** `ak.stock_index_pe_lg` 合法符号（实测 KeyError），需找到可用 PE 分位源再接，别硬塞（会"永远取数失败"误标缺失）。
3. **ETF 替代候选比较**：需可靠的费率/跟踪误差/同类清单数据源（westock 的 `etf` 给管理费/托管费，可作起点）。
4. ~~**⚠️ QDII 溢价实盘提醒**~~ **✅ 已落地为「执行质量闸」**（见 §3）：本周决策里 QDII 加仓任务在**溢价≥1.5% 或不可/暂停申购**时自动降级为「暂缓」并给原因，`current_suggestions`/调仓建议同口径。**遗留开放项**：政策闸已就位但需**真 flag** 才生效——若要让它对"12月 QDII 限购"等传闻反应，须先**查证并写一条** `政策风险/利空/高`（affected_assets 含 513100/513500）的 flag（建议在 `/周报` 研究环节做）。
5. **前端浏览器可视化验证**：本机 Preview MCP 这阶段起不来（环境把 python 指到 Xcode 的、权限被拒，与代码无关）。UI 改动目前靠 **"抽真实渲染代码 + 真实数据跑 + `node --check` + Flask test_client 验证 HTTP 交付"**；可视化验收需用户硬刷新浏览器自查，或修好 launch/preview 环境。
6. **取数稳定性现状**：**行情首选 westock(腾讯)，再东财→新浪→缓存**；**ETF 质量/实时价也已 westock 批量优先、akshare 快照兜底**——但 westock `etf` 接口偏不稳（盘后/限频常返"执行失败"），故 akshare 兜底必须保留、`_westock_covers_all` 控制是否跳过慢快照。**估值**仍只走 akshare/legulegu（`stock_index_pe_lg`），legulegu **较脆**——westock 不提供 PE 分位，估值备用源仍是开放项。可考虑：给估值加备用源、或养"每日刷新缓存"的健康检查、或把 westock 行情也接进 backtest `--refresh`。

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

## 10. 变更历史（精简，最新在上）

- **本轮（定位改私人投顾 + 可解释化 + 假设透明化｜Track A WS0/WS4/WS1/WS5/WS2）**：① **WS0 定位变更**——全仓「教育/辅助工具·不构成投资建议」→「私人投顾（自用）」（HANDOFF/README/两 SKILL/signals docstring）；保留 人在环/手动下单/不编造/ETF-only。② **WS4 假设单一来源**——`strategy.yaml` 新增 `assumptions` 块（每 sleeve 收益/冲击 + 来源/备注，乐观假设暴露在明处）；`signals.load_assumptions()` 默认回退模块表、逐键覆盖；删 `app._suggest_target_weights` 重复 `SLEEVE`，`estimate_target_stress_drawdown`/`expected_etf_return` 加可选参数贯通；`validate_strategy` 校验越界；建议权重载荷带 `assumptions_meta`、UI「假设与来源」折叠。③ **WS1 本周每只持仓 ETF 理由**——`signals.explain_rebalance_action()` 纯函数（error/被拦截/触发add-trim/不动 四档优先级 + 趋势/动量/估值三态限定语，缺失绝不写中性）→ 每行 `action_reason`/`reason_factors`；执行质量闸追加后缀；reports.md/控制台/前端（持仓信号表加「本周建议」列+ⓘ理由、wkTasks、wkBlocked）/两 SKILL 接线。④ **WS5 估值减速**——`signals.decelerate_add()` 对 rich 估值加仓按 risk_profile 软化建议规模（`action_mode=缓建`/`soften_amount`，只缩不放、仅 add、na/missing/cheap/neutral 不动、非闸不拦截）。⑤ **WS2 长期每权重理由**——`_suggest_target_weights` 每 item 加 `reason`（角色/权益预算贡献/压力贡献/QDII·成长提示/假设来源），政策闸冻结时追加「政策受限」；前端每行 ⓘ 理由。测试 105→**134**（新增 TestAssumptions/TestRebalanceReason/TestValuationDeceleration/TestTargetSuggestionReason）。
- **Track B Phase A 基座（定位已升 P0）**：新增纯函数模块 `engine/tactical.py`（双向战术配置，权威规格 `TACTICAL_ALLOCATION_DESIGN.md`）——**冻结三个 §15.1 门槛函数**：`score_asset`(§4.11 打分流水线)、`construct_tactical_portfolio`(§7.6 单向收缩+守恒+回退)、`next_tactical_state`(§5 状态机)，外加 `raw_tactical_weight`/`bounded_tactical_weight`/`load_tactical_config`/`validate_tactical_config`。`strategy.yaml` 加 `tactical_allocation`（`enabled:false`+`mode:shadow`，**纯计算、不进 actionable_rebalance**）。冻结测试向量全绿：§4.11 纳指→effective −0.380/raw 0.1028、§7.6 构建→A48/B20/R32/cash0 + 守恒 + 无风险间再分配 + 回退、状态机多周期。测试 134→**149**。
- **Track B Phase A 集成完成（影子产出，全程只读、不进 actionable_rebalance）**：`tactical.compute_shadow()` 编排（每资产 子信号→`score_asset`→`next_tactical_state`→带宽目标→`construct_tactical_portfolio`；reserve 不独立评分 §7.2）。`signals.py` 接线：`build_signal` 多回传 closes、`closes_by_code` 汇总，main 末尾算影子写 `out["tactical"]`（含每资产 state/effective/战术目标/偏离 + `input_fingerprint` §12.2 + mode/enabled），gate 在 `tactical_allocation` 存在且 mode∈{shadow,advisory}、try/except 包裹绝不阻断周报。**§5.1 状态契约**：`reports.prior_tactical_states()` 从上一活动周期读 `state_after`（旧报告优雅返 {}），新周期 state_after 随 `report.json` 归档→下周期读取。前端 `wkTacticalShadow()` 在周报「背景」档加只读「影子战术建议」表（状态/战术分/战略 vs 战术目标/偏离 + reserve/现金/主动偏离/回退标记）。`backtest.tactical_weekly_sim()` 周频事件模拟器骨架（§15.1#7，与影子**共用 tactical.py 纯函数**；成本/门槛/估值时点重建留 Phase B）。`strategy.yaml` 加 `tactical_allocation`（enabled:false/mode:shadow/reserve_asset 511010）。端到端实测：`signals.json` 已含 tactical 影子块。测试 149→**153**（TestTacticalShadow/TestTacticalBacktestSkeleton）。
- **Track B Phase B（回测计成本/门槛 + 消融 + walk-forward）**：① **状态门控修正**——`compute_shadow(gate_by_state=True)`：只有状态机 active/recovering 才真正倾斜，watch/neutral 维持战略（迟滞落到权重；去状态机消融用 `gate_by_state=False`）。② `backtest.simulate_tactical()` 全事件周频模拟：point-in-time 决策（`_val_pct_at` 无前视）、动作门槛(`min_rebal_turnover`)、佣金/滑点(`cost_per_side`)、QDII 溢价(`premium_extra`)；mode ∈ {static, 5_25, tactical, negative_only, no_valuation, no_state}。③ `run_tactical_comparison`(§13.1 六策略)+`walk_forward_tactical`(§13.4 分段冻结)+`perturb_params`(±20% 扰动)。④ CLI `python engine/backtest.py --tactical`（真实 ETF 段跑对比+walk-forward，估值臂离线为仅价格、已计成本/溢价）。测试 153→**162**（TestTacticalBacktestPhaseB：六策略/门槛降换手/成本降净值/消融有别/仅负向不增险/无前视/可复现/walk-forward+扰动）。**实测诚实结论**：当前 4.3 年 ETF 段上，双向战术 **未跑赢静态**（年化 5.1% vs 5.2%、回撤 −34.3% vs −34.7%、换手 +47%）——按 §13.5 **不达验收**（Calmar/Sortino 未改善 ≥10%）；样本短、无 2008/2015、估值臂仅价格是主因。这正是"上线前用回测挡住无效复杂度"的作用。
- **WS3 真实业绩跟踪 TWR/MWR（剔除注入本金）**：业绩书=已投入 ETF（NAV=`etf_value`，不含未投现金）。`reports.py` 新增 `journal/nav/<as_of>.json` 快照（`save_nav_snapshot`，每份正式周报落一条、同 `generated_for` 当日覆盖；折进 `archive_report`）+ `load_nav_series` + `cash_flows_from_executions`(买=+amount/卖=−amount、费用单列) + `compute_twr`(期末流入约定、子区间链乘、起始 NAV≤0 跳过) + `compute_mwr`(XIRR 二分 rate∈[−0.9999,10]、无符号变化→不可用) + `performance_summary`(TWR/MWR + 单只沪深300 基准 TWR + 累计费用 + nav_curve + 诚实注脚)。`/api/performance`（基准点对齐 NAV 区间、快照<2 或无网络优雅降级）+ 复盘标签「真实业绩 TWR/MWR」卡（chips + 相对基准跑赢/跑输 + 费用单列 + 注脚）。测试 162→**170**（TestPerformanceTracking：剔除本金 TWR≈0 / 两段链乘 / 已知 IRR / 加投不算收益 / 现金流符号 / 快照不足不可用 / 起始 0 跳过 / 快照同日覆盖）。- **Track B Phase D（估值时点重建 + 长样本回测——让 §13.5 验收变可信）**：`backtest.valuation_percentile_series()`（**历史时点 PE 分位、无前视**、纯函数）；`fetch_pe_history`+种子 `engine/data/pe_hs300.csv`(沪深300,2005~)/`pe_zz500.csv`(中证500,2007~)（**已联网取并入库、离线可复现**）；`build_proxy_valuations`(sh000300→沪深300/sh000905→中证500)；`build_proxy_panel`(长代理面板)。`--tactical` 现跑**两段**：① ETF 段(~4.3y,仅价格) ② **长代理段(~20y,含 2008/2015,估值臂=PE 时点重建)**。测试 170→**174**（TestValuationReconstruction：单调升/降时点分位、分位值、长面板离线构建）。**关键发现（长样本才看得出"危机保险"价值）**：长段静态 +9.6%/回撤 **−64.9%** vs 双向 +9.5%/回撤 **−60.5%**（去状态机 −58.5%）——**危机里双向把回撤压低 ~4.4pp、收益几乎不变、换手仅 20%**；估值臂生效（双向 9.5% vs 去估值 8.6%，低PE加A股 +0.9pp/y）。但 **Calmar 仅 +6.7%（<§13.5 的 +10% 门槛）**，价格指数(无分红)夸大回撤、代理段塌成 2 权益+1 债(丢黄金/QDII 分散)是保守偏差。**结论**：双向在危机样本里**方向明确、幅度温和、逼近但未过严格验收线**——比 4.3y 样本(无危机→看不出价值)可信得多。
- **Phase D 续：全收益+全分散长段复评（去掉两个保守偏差）**：新增 `fetch_us_index`（标普 `.INX`/纳指 `.IXIC`，2004~，种子 `idx_spx.csv`/`idx_ixic.csv` **已入库**）、`build_full_panel`（A股+美股QDII+国债、`_to_total_return` 合成全收益(补股息/票息 DIV_YIELD)、忽略汇率、剔黄金/创业板/科创50无长序列）、`--tactical` 加 **③ 全收益+全分散段（2006~2026 ≈19y，含 2008/2015，估值臂=沪深300/中证500 PE 时点重建）**。**复评结果(关键)**：静态 +13.7%/回撤 −54.2%/Calmar 0.25 vs **双向 +14.1%/−51.8%/0.27** vs **仅负向 +13.8%/−51.3%/0.27/换手仅12%**。即**realistic 样本里双向同时改善收益(+0.4pp)与回撤(−2.4pp)、Calmar +8%**(逼近但仍差 §13.5 的 +10% 一口气)；**仅负向覆盖性价比最高**(−2.9pp 回撤、12% 换手)。walk-forward 三段一致。**与价格指数段对比**(双向 +5.1% vs 静态 +5.2%、看不出价值)，结论从"复杂度不值"翻为"**双向/仅负向在现实样本里有真实、一致的小幅改善，Calmar +8% 近线**——是否足够上线是**所有者判断题**了(且黄金 10% 仍被剔除、为 2008 分散器，纳入会更好看)。测试 182→**183**(TestValuationReconstruction 加 build_full_panel)。
- **Phase D 再续：纳入黄金(2008 分散器)的最终复评**：`fetch_gold_proxy`(SPDR GLD 持仓报告 `macro_cons_gold` 2004~ 反推美元金价=总价值/总库存，种子 `idx_gold.csv` 已入库；稀疏→`build_full_panel` 内 `ffill` 到日频，避免内连接砍稀疏)。③ 段现含 **A股+美股QDII+国债+黄金、全收益、2005~2026 ≈21年(含 2008/2015)**。**最终结果(最可信样本)**：静态 +12.6%/回撤 −50.5%/Calmar 0.25 vs **双向 +13.0%/−47.7%/0.27** vs **仅负向 +12.7%/−47.5%/0.27/换手15%**(去状态机 Calmar 0.28/+12%)。即**realistic 全分散样本里双向/仅负向稳定改善收益(+0.4pp)与回撤(−2.8~3.0pp)、Calmar +8%(逼近 §13.5 +10% 线)**，walk-forward 三段一致。黄金把静态回撤 −54%→−50.5%(分散贡献 3.7pp)但**战术边际仍 +8%**——结论稳健。⚠️黄金为 GLD 反推近似、忽略汇率。**总结论**：跨价格指数/全收益无黄金/全收益含黄金三框架，**双向战术的价值随静态组合是否已充分分散而递减**；用户真实组合已高度分散(黄金+QDII)，故战术是**温和的边际改善(Calmar +8%、回撤 −3pp)而非游戏规则改变者**，**仅负向覆盖性价比最高**(15% 换手拿到几乎全部降回撤)。是否上线为所有者判断题。**数据种子(全部已入库、离线可复现)**：`pe_hs300/pe_zz500`(PE 时点)、`idx_spx/idx_ixic`(美股)、`idx_gold`(黄金)。
- **WS6 新手友好（纯前端、零后端）**：术语表扩 缓建/TWR/MWR/估值分位/压力回撤/偏离/战术(`TERMS`/`GLOSS_ORDER`)；`wkTasks` 加一行任务汇总(买/卖/缓建计数+"点每项看理由")与"本周为何不动"(数据/拦截原因)；历史周报/调仓记录空状态改成带引导文案(点哪、做什么)。`node --check` 通过。
- **提交前对抗式多 agent 审查 + 修复（5 维度 16 agent，安全关键项全过）**：审出并修了 4 条坐实问题——**#1(高,已亲验崩溃)** `compute_twr` 当子区间"期内流入>期末市值"→twr<−1→`(负)**非整次幂` 返回复数→`round` 崩、连带 `/api/performance` 500：加 `base≤0→年化记 −100%` 守卫 + 回归测试。**#2(中)** 回测 `_tactical_targets` 估值可靠度硬编码 0.85 绕过 §4.6 历史长度分级→新增 `_val_reliability_at`(<3年→0/3-7年线性→0.85/≥7年 0.85)并贯通 `simulate_tactical`(修了"早期短历史被当近满置信、夸大估值臂"的偏差)。**#3(中)** `construct_tactical_portfolio` step5 压力集中度上限硬编码 0.35、sleeve 级未实现→改读 `max_asset/sleeve_stress_contribution`、补 sleeve 级单向收缩(修了一处自写的 scale 公式 bug：只缩 tilt、保留战略基线)、step8 加集中度断言。**#4(低)** `compute_shadow` reserve 上下界派生公式→改读冻结 config `reserve_lower_bound 0.03/upper 0.35`。**复评**：4 项修完**长样本结论稳健不变**(双向/仅负向 Calmar +8%、回撤 −3.7pp、CAGR 略升、仅负向换手 16%)，证明结论对口径修正稳健;同时换手更低、去估值≈双向(诚实地说明价值主要来自趋势臂、非估值臂)。**被正确反驳 1 条**(黄金 ffill 压低波动=建模假设非可证错)。安全关键项审过且正确：gated 零泄漏、MWR 符号、单向收缩守恒、估值无前视。测试 183→**186**。`git` 卫生提醒:本轮 diff 与既有决策周期改动混在一起,建议拆两个 commit。- **Track B Phase C（gated advisory 管线——开关就位、默认不执行）**：`tactical.tactical_actions()` 纯函数，从影子诊断生成净战术动作（current→tactical_weight，§8.3 方向）；**双触发**=结构 5/25(向 tactical_weight) **或** (状态 active 且过战术门槛)——中性时 tactical_weight≈strategic、5/25 承接普通再平衡不被状态机吞掉，active 时战术门槛更敏感（修了"advisory 下结构再平衡被丢"的 bug）；reserve 不出动作。`signals.py` 始终算并附 `out.tactical.actions`（shadow 仅展示）。**Phase C 闸在 `reports.cycle_suggestions`**：仅 `tactical.mode=="advisory"` 时用战术动作取代结构性建议（`_tactical_cycle_suggestions`），**默认 shadow 走原结构性路径、行为零变化**。前端 `wkTacticalShadow` advisory 显横幅+可执行动作、shadow 显只读。**翻 `tactical_allocation.mode: shadow→advisory` 即唯一上线开关**（仍须先过 §13.5 + 影子≥8 周）。测试 174→**182**（TestTacticalActions 6 + TestAdvisoryGate 2，**钉死 shadow 零泄漏**：实测 `cycle_suggestions` 在 shadow 下只出 `rebalance`、即便 `tactical.actions` 有 4 条 actionable）。**待续**：WS6 新手友好；翻 advisory 前的 §13.5 复评(用全收益指数/保留分散重测)+8 周影子；Phase D 余项（估值备用源补红利/创业板/科创、协方差风险贡献、真实 TWR 评价用 WS3）。
- **本轮（两道风险闸 + 我的组合重排 + 一键同步 + 周报瘦身）**：① **执行质量闸**——QDII 加仓任务按实时折溢价/申购状态自动拦成「暂缓」（落地 §5#4）；② **政策闸**——高置信度政策利空 flag 冻结建议权重、可一键忽略（休眠待真 flag）；③ 「我的组合」重排：环形图(外标签)+关键数字卡+策略条+**两表合并**为一张全宽持仓明细；④ 本周决策待办**由成交记录自动推导**、手动勾选可取消；⑤ 调仓第二步去[执行状态]+加[删除]行；⑥ **一键同步** `sync.command`(mac)/`sync.bat`(win) + `.gitattributes`，个人数据换机器即拉即用；⑦ 周报归档瘦身（紧凑 `report.json`、不再写 `report.md`）；涨红跌绿统一。测试 82→95。
- **上轮（周报重排 + 浮亏修复 + westock 批量优先）**：① 周报统一三档渲染器 `renderWeeklyReport`（live/history 共用、必看/可看/背景、去"渲染两遍"冗余、零丢失），常驻区两卡合一为「本周决策」；② 浮动盈亏改 **均价×持仓** 成本基（修假浮亏）+ 调仓近 7 天软查重 + 删一条重复执行记录；③ westock 反转为 **ETF 数据第一顺位且批量**（kline 带 amount、批量 etf 预取、质量/实时价/20日成交额 westock 优先、`_westock_covers_all` 跳过慢快照、上市年限用成立日）。另：`/api/portfolio/preview` 成交后持仓预览、调仓向导（模态 3 步）、`start_mac.command` 端口接管。测试 69→82。
- **上一轮（重标定 + review + westock + 清理）**：universe 5→9、全组合风险口径、缓冲感知建议权重、DCA 回测、目标可行性、危机保险提醒、估值三态、ECharts 本地化、稳健桶可在设置里配、行情追踪全 universe、westock 质量兜底；review 修复（拦截文案口径/建议权重 1.01/长回测截断/P1-2 可见性）；删死函数 `renderGoalCoach`+`renderDecisionGuide`；个人配置与记录改为入库。测试 50→69。
- **更早**：月度复盘（守规则）、偏离复盘、压力贡献拆解、观察池学习系统、AI 旗标富渲染、ETF 折溢价/清盘提示、成交后持仓草稿、执行金额保护、依赖自检、前端从 ~18 板块重构为"决策区 + 5 标签页"并轻拆 html/css/js、`/api/portfolio/target-suggestion` 建议权重等。
- **初版**：周度信号引擎、回测引擎、结构化 AI 旗标、本地 Web 驾驶舱、可视化周报归档、执行记录。
