# 交接文档 / Project Handoff（当前真相 · 单一权威源）

> **下一个接手的 agent：先读这份**，按四段读——**§1 项目目标 → §2 重要决策（权威状态）→ §3 关键不变量 → §4 任务进展/待办**。
> **历史**在 [`HISTORY.md`](HISTORY.md)（只增·极少读：变更史、已闭环审计与提升计划、一次性侦察/审查快照）；
> **设计规范**在 [`STRATEGIC_ALLOCATION_DESIGN.md`](STRATEGIC_ALLOCATION_DESIGN.md) / [`TACTICAL_ALLOCATION_DESIGN.md`](TACTICAL_ALLOCATION_DESIGN.md)（参考·非当前状态）；**再平衡专题**见 [`REBALANCING.md`](REBALANCING.md)。
> **红线**：个股推荐 / 高频 / 自动下单都不做。

---

## 0. 协作规则 / 单一事实源

- 核心代码只在 `engine/`。两个 agent 入口 `.claude/skills/weekly-briefing/SKILL.md`、`.agents/skills/weekly-briefing/SKILL.md` **只是薄包装**，不要把 `signals.py` / `backtest.py` / app 逻辑拷进 agent 目录。
- 改行为：**先改 `engine/` 实现**，再按需更新 `README.md` / 两个 SKILL（仅当接口变化）/ 本文（§2 决策或 §3 不变量变化时）。
- **审查报告（`REVIEW_*.md`）抬头必须注明审查模型/版本**（2026-06-15 立此约定）——历史上未记录，导致无法追溯"哪个模型审过、漏了什么"（如折溢价方向语义被 06-09/06-10 两次审查整体漏过，见 `HISTORY.md` 2026-06-15）。
- 每改一处必须全绿（当前 **467 用例**）；前端改完 `node --check engine/web/app.js`。
  - **macOS**：`python3 engine/tests/test_engine.py`
  - **Windows**：`$env:UV_CACHE_DIR='F:\MakeMoney\.uv-cache'; uv run --offline --with-requirements engine\requirements.txt python -m unittest engine.tests.test_engine`

---

## 1. 项目目标与边界

**定位**：**私人投顾（单一所有者自用）**——**输出带理由的建议、不承诺收益、不自动下单**；人在环，最终拍板与下单永远在所有者手里；**ETF-only**；不编造数据（缺失就如实标"不可用/缺失"）。自用工具、不对外提供投顾服务，不涉及"类投顾"合规边界；个股推荐/高频/自动下单仍不做。

**资金结构（只写口径·不写数值——具体数字一律以配置文件为准，防文档与配置漂移）**：总资金分两桶——**场外稳健桶**（活期/固收/定存，只让算法"知道有"，由 strategic 从失业月开销×缓冲年限 + 压力后储备**自动算出**）+ **ETF 计划桶**（"做工的钱"，= 总资金 − 稳健桶，慢慢分批、是上限不是目标）。**目标年化针对 ETF 桶**（非全组合承诺、非保证）；**最大可接受回撤是全组合口径**；经验 intermediate、约 12–24 个月边学边投。

> ⚠️ **本文不复制任何配置数值**（总资金 / 目标年化 / 最大回撤 / 失业参数 / 派生的稳健桶与计划 ETF 金额）——**一律以 `investor_profile.yaml` 为准**（由驾驶舱生成/编辑，strategic 自动派生稳健桶与计划 ETF 金额）；持仓/目标权重见 `portfolio.yaml`；策略参数（universe/watchlist/因子/再平衡/动作门槛/strategic_policy）见 `strategy.yaml`。文档只描述结构与口径。

**核心策略洞察（标定的灵魂）**：稳健桶是"安全垫"——正因有它，ETF 桶才能更激进去够目标年化，同时把全组合回撤压在预算内。
**必须诚实保留**：目标年化即便对 ETF 桶也偏进取（回测 ETF 段约 4–6%、长代理段约 8% 但伴 −40%+ 回撤）；2008 级尾部下 ETF 桶约 −38%、按计划满仓折算全组合接近回撤预算——需权益重仓 + 容忍股票级波动，工具要把权衡量化讲清，绝不暗示稳赚。

**当前账户结构**：实际持仓/权重以 `portfolio.yaml` 为准、个人档案以 `investor_profile.yaml` 为准、策略参数以 `strategy.yaml` 为准（本文不复制数值）。结构上 ETF 桶权益约 70%；单情景与 2008 级压力的全组合回撤由引擎按 live 配置实时计算、应落在预算内。

**投资边界**：仅 ETF 配置；不加个股推荐（若加只能先做观察/风险监控）；对组合建议要讲清假设、不承诺收益、优先小额分批、ETF-only、明确手动下单；观察池永不参与首建/再平衡。

---

## 2. 重要决策（权威 · 当前状态 2026-06-09）

以下覆盖任何旧流程描述；与设计文档冲突时以本节为准。

- **长期战略权威路径**：保存长期战略设置后，系统自动计算场外稳健桶与本工具计划最大使用金额，构建模型组合，并在通过约束时直接应用。旧的"建议目标权重"、手动覆盖建议、季度墙和影子组合审查均已移除。
- **长期参数（口径，数值不在本文写死）**：总资金 / 目标年化 / 规划年限 / 最大回撤约束 / 失业月开销·缓冲年限·压力后储备 **一律以 `investor_profile.yaml` 为准**；strategic 据此**自动算出**场外稳健桶与本工具计划最大使用金额。
- **当前已应用目标权重**：以 `portfolio.yaml` 为准（本文不复制权重数值）。决策红线：**黄金/红利绝不为腾权重而砍**——早先一版 construct 把红利低波/黄金砍到 0%，与此冲突、已废弃（黄金/防御 5% 下限见 strategy.yaml strategic_policy）。
- **🆕 积木式前瞻收益已驱动「构建模型组合」（2026-06-09）**：优化器 `strategic.construct_strategic_portfolio` 选权重时，用的是**逐只锚定的前瞻预期收益**——债券=当前国债YTM、A股权益=中性锚+估值回归、QDII=美债YTM+ERP、黄金=judgment（对标 JPMorgan/BlackRock/Grinold-Kroner 的 building-block CMA 做法），**替代了旧的冻结假设表 `ASSET_EXPECTED_RETURN`**。之前积木式只"贴在结果旁展示·不改优化器"，现已正式驱动权重选择（详见 §3「构建收益口径」不变量）。
- **币种集中约束**：用 `single_risk_currency_exposure_max`，只约束风险资产的单一币种暴露，债券/现金类不计入。
- **候选引入闭环**："当前 ETF 是否合适"已补全：同资产类别的 universe/watchlist ETF 可作替代候选；候选须通过最近一次产品准入审查，再经 `/api/strategic/roles/introduce` 引入战略角色。目前无符合条件的替代候选。
- **模型组合是否优于简单组合**：原"复杂策略是否值得保留"已改为此口径，结果顶部直接给出保留复杂度/建议简化/证据不足的结论（当前回测结论倾向"建议简化"）。
- **两层把关分工（2026-06-08 厘清，勿再混）**：**长期战略层**（§8.2 `hard_admission`）只看**结构性**质量——规模/流动性/费率/上市年限/容量 + **限购**；**折溢价不在长期准入里**（status=info、不计入 admitted）。**折溢价/实时申购是执行时点问题**，只由「执行质量闸」在下单/调仓时把关（见 §3）。
- **§18 钉死决策（权威出处 [STRATEGIC_ALLOCATION_DESIGN.md](STRATEGIC_ALLOCATION_DESIGN.md) §18；live 阈值见 `strategy.yaml strategic_policy`，本文只记决策本身）**：① 目标收益口径=**ETF 桶**（全组合预期同屏显示、不混用）；② 场外稳健桶=真稳健（存款/货基，低收益·近 0 冲击）；③ 组合级硬约束：非卫星下限 + 卫星/单卫/成长/单国/单风险币上限 + 黄金·防御下限（具体阈值见 `strategic_policy.caps`）；④ 构建压力预算与展示回撤解耦：`construct_stress_budget`（绝对档）＞ `construct_stress_margin`（相对档：预算=可接受回撤−margin，自动联动）＞ 默认=回撤；**2026-06-10 所有者拍板：目标年化 8%→6%、margin=5pp（当前预算 25%）**——新约束下权威构建复验通过且最优权重与已应用目标完全一致，无需调仓。
- **诊断生成物不入主流程**：`journal/strategic_reviews/` 与时间戳 `reports/` 属历史/诊断生成物，除非明确需要同步诊断快照，否则不要纳入提交。
- **启动器护栏**：`start_windows.bat` / `start_mac.command` 启动前清理占用 5057 端口的旧 dashboard 进程；端口被无关程序占用则停止启动、不误杀。

---

## 3. 架构与关键不变量（改动**勿破坏**）

### 3.1 组件与架构

| 文件 | 职责 |
|---|---|
| `engine/signals.py` | 周度信号引擎：趋势(MA200)/动量(60d)/估值分位/再平衡(5-25)；多源取数+缓存+数据分级；风险预算（全组合口径）；**积木式前瞻预期收益 `building_block_returns`**；首次建仓预览（`first_funding_orders` 缺口优先 + 固定单周上限，见 §3.2「0持仓建仓」）；动作门槛；`trend_alerts`（危机保险） |
| `engine/strategic.py` | 长期战略构建（纯函数·零 I/O）：`construct_strategic_portfolio` 角色/区间/硬约束下选 primary、按收益排序选权重、最终验证；收缩协方差接受判定 |
| `engine/tactical.py` | 双向战术（纯函数）：`construct_tactical_portfolio`；当前 **shadow·未接入可执行**（见 §4） |
| `engine/backtest.py` | 回测：① ETF 可交易段 ② 指数代理长段（价格指数，看回撤轮廓）；**DCA 分批建仓对比**（`run_dca`）；walk-forward |
| `engine/reports.py` | 周报归档 + 月度复盘（看是否守规则，不算盈亏）+ 成交后持仓草稿 + NAV 快照/TWR/MWR |
| `engine/validate_flags.py` + `flags_schema.json` | AI 舆情风险旗标的结构校验（纯函数 `validate_flags_data`，消费前强制校验） |
| `engine/learning.py` + `learning_cards.yaml` | 观察池学习系统（观察≥4周+学完→可讨论纳入；永不可直接买） |
| `engine/app.py` + `engine/web/` | 本地 Web 驾驶舱（Flask + 单页 vanilla JS + 本地 ECharts）；唯一组合根；不实现独立投资逻辑，只调 `engine/` |
| `strategy.yaml` / `portfolio.yaml` / `investor_profile.yaml` | 策略参数 / 持仓 / 个人档案 |
| `engine/data/` | 回测种子数据（committed，离线可复现）；`meta.json` 记来源/复权/区间 |

### 3.2 关键不变量 & 耦合

- **数据诚实**：缺数据标"不可用/缺失"，绝不编造；`grade_data` 分级 完整/缓存可用/过旧/部分缺失；只有"完整/缓存可用"才给再平衡；`allow_trade_with_cache=false` → 含缓存行情时拦截可执行交易。
- **全组合口径**：`signals.whole_portfolio_stress(etf_dd, etf_value, stable_outside)` 把 ETF 桶压力回撤按"稳健桶=0 冲击"折算到全组合；`risk_budget` 同时带 ETF 桶（`target_portfolio_stress_*`）与全组合（`whole_portfolio_*`）数值；**风险闸门与拦截文案都用全组合口径**（`max_acceptable_loss`/`stress_losses` 也用全组合基数；`target_annual_profit` 用 ETF 桶，已标注）。
- **🆕 构建收益口径（积木式驱动构建）**：`signals.building_block_returns` 每只持仓产出 `expected` + `expected_conservative`（**保守口径按置信度缩放**：高置信YTM腿用小折扣 `BB_YTM_CONSERVATIVE_HAIRCUT`≈0.5%，中/低置信腿把 sleeve 折扣 `returns−returns_conservative` 平移到锚定中枢）。`app._run_construct` **在构建前**按 universe 逐只算好，作 `returns_by_code`/`returns_conservative_by_code` 传进 `construct_strategic_portfolio`，替代冻结表驱动权重选择（仅换排序向量，**不进可行性判定**——caps/stress/role 区间不动，故可行性不变）。**至少一腿真锚定**（confidence≠low）才标 `construct_return_basis="anchored"`；YTM 全失败/估值全缺 → 传 `None`、回退冻结表、记 `frozen_fallback` 并诚实提示。**取锚前先保新鲜（2026-06-10）**：`_run_construct` 读 signals.json 前调 `_ensure_signals_fresh_for_construct`——signals.json 缺失 / `risk_budget` 无 `bond_ytm` 键（早于积木锚特性的旧 schema）/ `generated_for` 早于今天 → 自动刷新一次（跑 `signals.py` + 执行质量闸回写，**不归档**，与周报逐字节同源）；三条判据均无状态、刷新后自愈、不抖动（节假日亦只刷一次/天）。刷新失败/离线仍 fail-closed 回退冻结表，`_construct_frozen_note` 据 `signals_refresh` **三态如实区分**（刷新失败 / 已刷新仍取不到 / 本就新鲜仍缺锚），`snap.signals_refresh` 透出。节奏护栏：锚定收益按 0.5% 桶进 `input_fingerprint`（随有意义变动呼吸、不被噪声 thrash，对标机构年度重校）。配置 `strategy.yaml › expected_return`（`bond_ytm_tenor`/`valuation_reversion_years`/`valuation_adj_cap`/`us_ytm_tenor`/`equity_risk_premium`/`ytm_conservative_haircut`）。
- **🆕 0持仓建仓（固定单周上限 + 缺口优先，2026-06-15）**：空仓账户**不跑再平衡**（再平衡对空账户=要求立即满仓），改走 `first_funding_orders` 预览。可投额 = `min(现金, max_weekly)`——**固定金额节流，不得退回按现金百分比**（`first_tranche_pct` 已退役：资金分批到账时百分比会退化成长期约 85% 现金拖累）。分配走**缺口优先**（逐手买"离目标权重最远"的腿，**不过冲守则 `缺口>一手/2`**，`build_first_funding_schedule` 跨周累计）；`min_trade`=200。QDII 溢价闸仍在 **reports 层**后置叠加（signals 的 `first_funding_orders` 不感知溢价，故预算可能分给溢价腿后被后置拦下作未分配——已知局限）。权威设计/原因/局限见 [`DEPLOYMENT_REDESIGN.md`](DEPLOYMENT_REDESIGN.md)。
- **建议权重 `app._suggest_target_weights`**（月度/季度策略审视用，**非每周执行**）：基于**整个 universe**（含未持有品种）；缓冲感知——`etf_share=planned_etf/(planned_etf+stable)`，`etf_dd_budget=min(max_dd/etf_share, 0.40)`；按 sleeve 参数化搜索权益比例（`e_cap` 随经验 0.65/0.85/0.95）；**残差并入当前最大权重项**（早先并入债券会在债券=0 时被 `max(0,..)` 吞掉→合计 1.01）；sleeve 的收益/冲击假设**复用 `signals.ASSET_EXPECTED_RETURN`/`ASSET_SHOCKS`**（勿再各写一份）。注：此路径仍用冻结假设；**积木式锚定只接进了 §10 战略 construct，不是这里**。
- **两处 `DEFAULT_INVESTOR_PROFILE` 必须同步**（`signals.py` 和 `app.py` 各一份，app 现从 signals 导入为单一来源）；新字段 `stable_assets_outside`/`stable_assets_yield`/`planned_etf_capital` 要在 `save_config` 持久化（UI 无输入时按现值回退、不丢）、`_write_investor_profile` 写出、`validate_investor_profile` 校验。
- **估值三态**：`signals.VALUATION_APPLICABLE_ASSETS`（仅 A 股权益）。QDII/黄金/债券/现金 → `valuation_na`（不适用，不当缺失也不当中性）；A 股权益但无可用源 → `valuation_missing`（**非中性**，如实标）；有 index 且取到 → 分位。其中创业板 159915 走 `valuation_proxy`（创业板50 代理·近似）、科创50 588000 / 红利低波 512890 走 `valuation_csindex` 按日自建累积（< 3 年 → `valuation_accumulating` 态，只显当前 PE + "积累中"、percentile=None）。preflight/CLI/主信号视图/周报详情四处都区分这些态。
- **🆕 估值双源（2026-06-11，关掉"乐咕单源"这个唯一取数空洞）**：设计是**历史与当日点解耦**——`fetch_valuation_pct` 乐咕成功时把分位窗口的历史序列**本地化**进缓存（`valuation_<指数>.json › series`）；乐咕挂掉（异常**或响应形状不对**，后者原先直接放弃）→ `_csindex_backup_pe` 按 `VAL_BACKUP_CSINDEX`（沪深300→000300、中证500→000905）从中证官网取**当日一个 PE 点**（取「市盈率2」=滚动TTM，与乐咕同口径），分位仍按本地化历史窗口算 → `source="live_backup"`、result 带 `backup_source/series_through`，后续缓存回放保留 `backup_source` 可追溯。**备援点绝不混入 series**（序列保持纯乐咕，防两家口径毫厘差污染历史）；无本地 series（旧格式缓存）或无映射（创业板50 是国证系）→ 不触中证、按原缓存链回退。
- **DCA / 长回测**：`run_dca`/`_dca_sim`/`_median`（一次性 vs 6/12/24 月滚动窗口）；proxy 段**单个代理缺失只剔除该 sleeve、不整段放弃**；`159915`/`588000` 的 `proxy_index=null`（创业板指 2010/科创50 2019 太短，并入会把"20年段"截断）。
- **westock（腾讯自选股）数据源——ETF 数据的【第一顺位·批量】**，经 `npx -y westock-data-skillhub@1.0.3`（需 Node≥18 + `Bash(npx:*)` 放行；`.claude/settings.local.json` 本机已加、**换机器要重加**）。整个看板只 2 次 npx（kline + etf）覆盖全部 ETF：
  1. **行情【首选源】（signals.py `fetch_hist`）**：顺序 **westock(腾讯,qfq) → 东财 → 新浪 → 缓存**。`prefetch_westock(codes)` 一次批量 `kline`（`_parse_westock_kline_batch`，保留 `amount` 列）填 `_WESTOCK_HIST`。westock `source="westock"`、按"完整"对待。OHLC 仅 2 位小数，对趋势/动量无碍。
  2. **ETF 质量层【首选·批量】（app.py）**：`_prefetch_westock_etf(codes)` 一次批量 `etf` 取折溢价/规模/成交额/**QDII 申购状态**/**成立日**；`_quality_metrics` **westock 优先、akshare 快照(`fund_etf_spot_em`)兜底**；20 日成交额从批量 kline `amount` 出；上市年限用 etf `establishDate`；`_westock_covers_all()` 全覆盖时跳过慢的 akshare 快照。⚠️ westock `etf` 接口偏不稳（盘后/限频常挂）——akshare 快照必须保留为兜底。
  3. **盘中实时价 `/api/etf/spot`**：同样 westock 优先、akshare 快照兜底。
  - backtest.py 未改（仍以 `engine/data/` 种子为主、`--refresh` 走东财/新浪），保持离线可复现。
- **执行质量闸（单闸·含前瞻政策旗标；把"风险提示"接进"动作/权重"，只作用于买入侧）**：
  `_apply_execution_quality_gate`（app.py，`run_signals` 内、归档前）：对**买入类**动作按**实时折溢价 + 前瞻政策旗标**裁决——纯函数 `_exec_quality_decision`（**方向区分·2026-06-15 option B**：仅**溢价**超阈值才拦——敏感≥1.5%/普通≥3%=issue→拦、轻度溢价→warn 别追高；**折价对买入是折扣不拦买**——轻度折价→ok 放行、大幅折价→warn 写明"疑似清盘/停牌/底层失真，核实后再买"；`_classify_premium` 档位仍对称、只让文案随方向正确，方向拦截在 `_exec_quality_decision`。`_policy_flag_blocks` 命中『政策/流动性风险·利空·actionable』旗标=暂缓）。**🆕 申购状态自 2026-06-15 仅作标识、不单独拦买**（所有者拍板）：申购/赎回是一级市场、场内仍可交易，申购受限只 warn（含"场内仍可买、留意溢价"提示）解释溢价为何易失控，**是否拦截以实测溢价为准**——`_purchase_status_note` 受限即返回 warn（不再 issue）。issue→`actionable=False`+`blocked_reasons`；warn/缺失→挂 `exec_quality_note`（**缺失≠中性、不硬拦，只提示自查**）。**只改买入、卖出不动**；回写 `signals.json` 同口径；`archive_report(signals=...)` 用加工后的 signals 归档。**执行时点重验 `_recheck_cycle_suggestions` 同口径查旗标（2026-06-10）**——周中新落的利空旗标在"打开调仓→执行"时同样拦得住。
  > 旧的独立"政策闸" `_apply_policy_gate` 与 `/api/portfolio/target-suggestion`、前端 `applyTargetSuggestion()` 已随"长期战略收敛为权威模型组合"整体移除（commit 3fa4a15），勿按旧文档去找。
  另有**纪律闸方向化（2026-06-10）**：「价不可信」（数据过旧/缓存）双向拦；「组合超风险预算」只拦加仓（减仓恰是减险）；trend_alerts 同样过价不可信闸（`build_trend_derisk(discipline_blockers=...)`），不再绕过。
- **决策周期单一事实源**：首页/本周决策/调仓统一从**活动决策周期**（最新 `reports/<id>/report.json`，`cycle_status=active`）派生，不再各读最新 report 与 signals.json；只带未完成动作；打开调仓重验折溢价/申购；成交登记走 `/api/decision-cycle/execute` 单一事务（失败回滚）；新周期生成把旧周期标 `superseded`；月度复盘按正式周期去重。周期写 `portfolio_version/strategy_version/investor_profile_version` 指纹，配置变更即提示失效。
- **前端**：`marketTrackCodes()` 让"行情与质量"追踪整个 universe；ECharts 本地优先 `/web/vendor/echarts.min.js` + CDN 兜底。（旧 `applyTargetSuggestion()` 已随 target-suggestion 路径移除；应用目标权重走「长期战略→应用模型组合」`applyStrategicConstruct()`。）
  - **🆕 数据源健康账本 + 启动预热（2026-06-11）**：`signals.record_fetch_health(source, ok, error)` 每次**真实触网**后记一笔（缓存跳过=没触网=不记）到 `engine/cache/fetch_health.json`（路径走 `fetch_health_path()` 函数、跟随 CACHE_DIR，测试可重定向）——记 westock行情/东财/新浪日线、乐咕估值、中证估值累积/备援、国债/美债收益率；连败计数成功清零。`GET /api/health/data` 经 `app._source_health()` 并入：`live_status`（ok/stale/warn，连败≥3 → warn）、`sources` 账本、`prices_stale`、`valuation_freshness`（含 `backup_ready`=中证有映射且历史已本地化）、`ytm_freshness`；前端 `loadDataHealth()` 渲进"数据详情"弹层，连败≥3 置顶红条 + 按钮亮 ⚠。`signals.warm_caches()`（CLI `--warm-cache`）= 只刷缓存不算信号，行情/估值/YTM 全走各自缓存跳过；app 启动起 daemon 线程 `_warm_caches_on_start` **一次性**预热（与 bb62be6 删掉的 10 分钟轮询不同，勿混淆）——把"决策时现拉"变成"决策时读定稿缓存"。
  - **🆕 长任务进度通道（2026-06-11，体验#24/#26）**：`signals.report_progress()` 原子写 `engine/cache/progress.json`（尽力而为、绝不影响计算、不入归档）→ `GET /api/signals/status` → 前端 `pollTaskProgress(elId,t0,{tasks,hint})` 轮询展示"第X/N步·阶段名·已用时"。四类任务共用一个通道：`signals`（main() 六阶段插桩+app 层归档）/`construct`（含隐式信号刷新，前端加"自动刷新本周信号"前缀）/`incumbents`（逐只 i/N）/`backtest`（三端点）；`tasks` 数组过滤防上个任务残留串显。
  - **🆕 确认层与调仓校验（2026-06-11，体验#25）**：`confirmDialog({title,body,confirmText,danger})` 页面内确认层**替代原生 confirm()（全仓应为 0 处，勿再新增）**；调仓向导步骤2 `validateRebalanceRows` 输入即校验（整手/正数/价格，错误禁用下一步），交易前确认清单在**第3步** `#rebalChecklistBox`（未勾全禁用确认钮）。趋势减仓建议有「带入调仓」`trendDeriskFill`（预填登记表单、不绕闸门）。
  - **周报渲染（统一）**：`renderWeeklyReport(s,{mode:'live'|'history',container,flags})` 是**唯一**渲染器——`#weeklyReportLive`（live，含可勾选待办）与 `#reportDetailPanel`（history，只读）共用。分**必看/可看/背景**三档（`.wk-must/.wk-why/.wk-bg/.wk-sec`）。动量图按 mode 隔离 id、重渲染前 dispose 防 `ECHARTS[]` 泄漏。**别再恢复**旧的 `renderSignals`/`renderReportDetail` 双份渲染或 `#sigbox`/`#decisionCard`（已删）。
  - **构建展示**：`renderConstruct` 头部「**已按前瞻锚定构建**…ETF 桶预期年化 X%（保守 Xc%）｜冻结假设口径对照 Y%」；`bbBlocks` 折叠表逐只显示 中性锚/估值回归/前瞻预期/出处·置信。`construct_return_basis=frozen_fallback` 时显示降级提示。
  - **浮动盈亏（app.js `costBasisByCode`/`portfolioValueRows`）**：成本基 = **均价 × 当前持有份额**；无买入记录→「成本未知」、执行份额≠持仓→⚠ 估算。调仓 `confirmRebalance` 登记前对近 7 天相同成交软提示（不硬拦）。
- **校验约束**：`validate_strategy` 要求 universe **有且仅有一个 `asset:bond`**；watchlist 与 universe 不得重复；`strategic_policy`/`tactical_allocation` 有 schema 校验。
- ECharts 实例经 `initChart()` 注册到 `ECHARTS[]`，`activateTab` 调 `resizeCharts()`；`static_folder=None`，只服务 `/` 与 `/web/<path>`。

---

## 4. 任务进展 / 待办

### 4.1 已闭环（详情见 [`HISTORY.md`](HISTORY.md) / git；此处只留索引）

- **🆕 申购状态降级为标识（2026-06-15）**：执行质量闸不再单凭"不可/暂停申购"拦买——申购是一级市场、场内仍可交易；申购受限改为只 warn（解释溢价为何易失控），**拦截只看实测折溢价**（敏感≥1.5%/普通≥3%）。改 `_purchase_status_note`/`_exec_quality_decision`（app.py），更新 3 处测试，**465 全绿**。详见 §3.2「执行质量闸」。
- **🆕 0 持仓建仓重构（2026-06-15，本轮）**：空仓周"信号大多被拦"的根因是**建仓节流逻辑 × 绝对门槛与"资金分批到账"不匹配**——`first_tranche_pct`（现金×15%）在资金滴入时退化成长期约 85% 现金拖累。修法：① 退役百分比，改 **固定单周上限 `max_weekly`**（`min(现金, 上限)`，唯一节奏闸）；② 新增 `first_funding_orders` **缺口优先**逐手铺开（不过冲守则 `缺口>一手/2`，跨周累计）；③ `min_trade` 500→200。保留 QDII 溢价闸（reports 层）/风险闸计划资金口径/缓建/熔断。新增 `TestFirstFundingOrders` + 重写 schedule 测试，**464 全绿**；真机走查首周 2/9→**9/9 可执行**。权威设计见 [`DEPLOYMENT_REDESIGN.md`](DEPLOYMENT_REDESIGN.md)。
- **P0 统一决策周期**（阶段 1+2，2026-06-06）：多状态源收敛为单一活动周期；调仓只带未完成动作、打开重验、单事务执行；战略审视与每周执行分离。
- **五维提升 #1–#6**（HISTORY §0C，至 2026-06-08 全 ✅）：历史压力情景 / walk-forward 回测 / 协方差进接受判定 / 趋势减仓建议 / Sharpe+无风险 / 实盘 NAV·TWR·MWR。
- **A 股成长估值**（2026-06-08）：创业板50 代理 + csindex 自建累积，估值三态诚实。
- **同类 ETF 发现**（2026-06-09）：`/api/etf/peers`，自动匹配·人工确认。
- **QDII 前瞻政策闸**（2026-06-09）：旗标真正接进买入闸（此前是 no-op），513100 实测被暂缓；顺手修 `load_json` 漏导入 bug。
- **前端 Preview 真机验证**（2026-06-08）：`.claude/launch.json` `dashboard`/`dashboard-win` 配置可用。
- **多代理全面审查闭环**（2026-06-09）：见 [`REVIEW_2026-06-09.md`](REVIEW_2026-06-09.md)，根因 A/B/C/D 多数 ✅已修。
- **🆕 积木式前瞻收益驱动构建**（2026-06-09，本轮）：见 §2 / §3「构建收益口径」；新增 6 测试，端到端真机验过（驱动 5.5% vs 冻结对照 6.2%，债券高置信 cons 走小折扣）。
- **修 test_exec_quality_gate 测试隔离**（2026-06-10）：原 pre-existing 红是**测试隔离 bug 非产品 bug**——执行质量闸读旗标走 `load_validated_flags`→盘上 `flags.json`，不经被 mock 的 `webapp.load_json`，本机真旗标（513100 政策风险·利空·actionable）命中→前瞻政策闸拦成 blocked。修法：把 `load_validated_flags` 也 mock 成空 flags。全套 **410 全绿**。
- **🆕 构建前自动保 signals 新鲜（2026-06-10）**：定位到「前瞻锚定收益不可用」的根因是 signals.json **陈旧**（早于积木锚特性的旧 schema：`risk_budget` 无 `bond_ytm` 键 + 估值缺 `pe_median`），**非 fetch 报错**——构建只读不抓、读到旧文件即三腿全 low 回退。修法（§3「构建收益口径」）：`_run_construct` 取锚前新增 `_ensure_signals_fresh_for_construct`——缺失/旧schema/跨日 → 自动刷新一次（同周报数据路径、不归档、保持同源），刷完 ≥1 腿锚上即翻 `anchored`；真离线/数据源延迟才续 `frozen_fallback`，`_construct_frozen_note` 按 `signals_refresh` 三态提示。apply 端点靠 `input_fingerprint` 兜底（数据变即 409 重审，不会静默套用未复审数据）。两处直调 `_run_construct` 的单测 mock 新 seam；**410 全绿**。
- **🆕 取数稳定性收口（2026-06-11，本轮）**：原 §4.2 开放项#1 的 ①② 落地——**估值双源**（乐咕历史本地化 + 中证官网当日点备援，见 §3.2「估值双源」）+ **健康账本/启动预热/数据详情面板**（见 §3.2「数据源健康账本」）。新增 13 测试，**459 全绿**。
- **🆕 全面审查修复批（2026-06-10，见 [`REVIEW_2026-06-10.md`](REVIEW_2026-06-10.md)）**：4 高 + 11 中 + 10 低全部修复，**444 全绿**。低优先含：L1 expected_return 配置校验；L3 缓建金额整手化；L7 trend 三态(unknown)；L8 quality 端点传 realtime_reliable；L9 写盘/参数健壮性；L11 跨源 POST 拒绝（before_request 查 Origin）；L13 死分支/费用计数/IRR 文案；L15 删 legacy 构建 dead code；L18 ECharts 防泄漏+空值保护；L19 空持仓引导文案。未动（有意）：L12 fee=0 哨兵语义（已注释为已知局限——前端无"真 0 费"入口，放宽会重开 §4-1 漏洞）；L2 全组合显示口径、L16 趋势保护范围（待所有者拍板 Q5/Q6）；L4/L5/L6 估值路径合一（结构性重构，单独立项）。高：H1 盘中价定稿（`_save_cache` 只落已收盘定稿行，盘上污染缓存已清理）；H2 调仓表单加买/卖方向选择（手动卖出此前会被记成买入双向算反）+ 后端方向/原因矛盾警告；H3 execute 成功后 `apply_execution_to_nav_snapshot` 把当日 NAV 校准为成交后值（防 TWR 假亏损）；H4 strategic 纯数据缺失型未准入 incumbent 也冻结在当前权重（堵 fail-open）。中：M1/M2 闸门分「价不可信（双向拦）/超风险预算（只拦加仓）」两类、trend_alerts 接闸；M3 执行重验查政策旗标；M4 同日重生成不再被旧版成交/否决串状态（`since=周期 created_at`）+ 覆盖前留痕 `reports/<id>/history/`；M5 execute 回滚改"台账+持仓同进退、收尾失败不回滚"；M6 构建指纹补 `incumbent_weights`（抽出纯函数 `_construct_fingerprint`）；M7 YTM 缓存回退限 30 天 + `stale_days`；M8 simulate_tactical 决策点间权重随收益漂移（5/25 基准此前恒不触发）+ 战术置信度硬下限接入 `compute_shadow`；M9 应用模型组合后常驻横幅（周期指纹失效触发）；M10 开页路径补旗标元数据 + "晚于"文案改"早于" + 旗标≥3天给刷新提示；M11 月度复盘建议按身份去重、金额取当月最新版；M12 整手前后端双重校验（买入须 100 整数倍、正数）。文档：README 删"手改 portfolio.yaml"指引、本文删 `_apply_policy_gate`/`applyTargetSuggestion` 幽灵描述、sync 脚本补 strategy.yaml。

- **🆕 UX 优化批次①–④全落地（2026-06-11，体验#24–#27）**：依据 [`UX_PLAN_2026-06-11.md`](UX_PLAN_2026-06-11.md)（用户旅程走查）——①信号生成 7 阶段进度化+首屏骨架屏 ②调仓校验前移+确认清单归位第3步+confirmDialog 替代 4 处原生 confirm ③进度化推广到构建/回测/质量审视+趋势减仓「带入调仓」+周期失效一键重生成 ④首页复盘快捷入口+战略页双导航合并单主线。所有者拍板不做：移动端、ETF 研究紧凑模式（误判撤回）、复盘升顶层、帮助重排。**446 全绿**、逐批真机验收。

### 4.2 开放项（下一个 agent 从这里继续）

#### 2026-06-18 所有者确认：私人投顾可靠性增强（按顺序实施）

- [x] **P0 行情快照一致性**：趋势/动量/估值只消费最近已收盘交易日；盘中数据只进入执行检查；报告拆分 `signal_as_of` / `execution_checked_at` 并生成可追溯 `snapshot_id`。
- [x] **P0 闸后建仓金额重算**：执行质量闸完成后重算实际可执行、暂缓与剩余现金；被拦资金保持现金，绝不自动重分配；未来周次不套用今天的折溢价。
- [x] **P1 实盘绩效归因**：扩展现有 NAV/TWR/MWR，拆出单 ETF 贡献、现金拖累、费用、成交滑点、静态目标组合差异及残差，并保证贡献可核对。
- [x] **P2 ETF 产品风险持续监控**：在现有准入/质量快照上增加规模、成交、折溢价、跟踪、费率、申购和清盘/合并事件的历史趋势与 `info/warn/block` 分级；不自动替换产品。
- [x] **P2 数据源交叉验证**：收盘价、NAV/IOPV、折溢价和规模保存双源值与差异；价格冲突双向停单，NAV/折溢价冲突拦买，规模冲突只降级产品结论；单源必须显式标注。

**完成记录（2026-06-18）**：五项均已落地；双源校验额外区分 published NAV 与 realtime IOPV 的不可比时点，避免制造假冲突；质量页普通刷新不跑慢速跟踪误差，手动刷新按日计算并缓存。Python/前端语法检查通过，**480 项回归测试全绿**，真实 `/api/etf/quality?codes=510300` 烟测通过并写入 `journal/product_risk/2026-06-18.json`。

- [x] **调仓频率按日归并（2026-06-18）**：同一自然日内多次成交视为同一个调仓批次，不重复触发双周/月度/季度间隔；跨日后才按 13/28/84 天计算。单周金额、现金、整手和执行质量闸保持不变。

#### 2026-06-18 长期战略算法专项修复

- [x] **P0 实盘/回测同口径**：战略回测复用 live 的构建压力预算和收益输入接口；冻结假设与实时锚定证据分开披露，不再用不同构建器结果证明 live 配置。
- [x] **P1 角色内权重优化**：多成员角色进入受约束权重搜索，删除隐式等权；最终 1% 投影继续复验角色、卫星、国家、货币、产品冻结和压力约束。
- [x] **P1 目标可行性状态**：拆分 `constraint_status` 与 `target_feasibility`；6% 为期望目标（`target_return_hard_gate:false`），未达时明确警示但不单独禁止应用；硬目标模式仍可配置为 fail-closed。
- [x] **P1 风险模型收口**：固定收缩模型如实命名；启用可配置的协方差覆盖率/压力/有效风险源验收，缺覆盖时 fail-closed。
- [x] **P2 策略档位与配置接线**：区分 `return_first` / `balanced` / `defensive_first` 排序；消费 `target_return_basis` 与稳健桶收益字段，删除死语义。
- [x] **P2 产品风险联动**：日级产品风险 `block` 接入战略构建的“不增配”限制；只冻结/排除，不自动替换产品。

**完成记录（2026-06-18）**：`policy_version` 升至 3；当前 live 构建为约束通过、目标未达、`ready_with_warning`，收益缺口作为风险提示而非伪精确硬闸。战略回测确认 `construct_source=live_construct_override`、压力预算 25%、收益口径 anchored；walk-forward 3/3 折仍倾向简化，但明确只证明冻结收益假设下的政策结构。Python/前端语法检查通过，**489 项回归测试全绿**。

范围、证据和验收口径见 [`STRATEGIC_REVIEW_2026-06-18.md`](STRATEGIC_REVIEW_2026-06-18.md)。

范围与结论见 [`REVIEW_2026-06-18.md`](REVIEW_2026-06-18.md)。明确不做：券商成交单自动导入、资本扩张自动闸门、模型自动改权重、自动下单或新增短周期预测指标。

1. **取数稳定性**：✅ ①估值备用源、②缓存健康检查已落地（2026-06-11，见 §3.2「估值双源」「数据源健康账本」——沪深300/中证500 乐咕挂掉走中证官网备援；创业板50 无映射仍单腿乐咕+30天缓存，影响面只剩 159915 一只，可接受）。剩 ③ 把 westock 行情接进 `backtest.py --refresh`（低优先：backtest 以 `engine/data/` 种子为主、离线可复现，--refresh 走东财/新浪只是慢不是断）。
2. **设计文档 Phase 进度**：
   - **Strategic**（[设计](STRATEGIC_ALLOCATION_DESIGN.md)）：§18 已钉死；权威 construct 已实现并含**积木式锚定收益（本轮）/ 收缩协方差接受判定 / 多情景压力**——Phase A–C 主体已落地，剩 Phase D 的滚动期/参数扰动稳健性与治理打磨。
   - **Tactical**（[设计](TACTICAL_ALLOCATION_DESIGN.md)）：仍 **shadow**（`strategy.yaml › tactical_allocation.enabled:false, mode:shadow`）；§4.11 打分流水线 / §6 带宽 / §7.6 构建已冻结，待 §13.5 回测验收通过后改 `mode:advisory` 接入可执行调仓。开工前置门槛尚有 §4.8 估值覆盖披露、§7 周频回测模拟器两项 ⚠。

---

## 5. 运行 / 命令 / 数据

**运行节奏**：每天可跑 `signals.py` 做数据健康/观察（不代表每天交易）；每周正式决策；每月/季复盘策略与池。低频、克制。

**动作门槛**（`risk_controls`）：保留原始 `rebalance`（信号级偏离），用户可执行动作看 `actionable_rebalance`，0 持仓用 `first_funding_plan`；再平衡**整手化**（建议 Δ股数四舍五入到 100 份，不足一手则诚实压制）；UI 只展示预览，绝不自动写交易/改份额；观察池永不参与首建/再平衡。

**周报归档流程**：`signals.py` → 写/初始化 `flags.json` → `validate_flags.py` → `reports.py` → Web 渲染 `reports/<id>/report.json`；简报带 `report_id`。归档只写紧凑 `report.json`（不缩进、约 15KB），不再落盘 `report.md`（`render_report_md()` 仍保留供按需导出）。

**观察池规则**：`watchlist`（现 511880/511990/511360）只学习/监控，不影响权重、不触发再平衡；未经用户明确"纳入"不得用买/卖措辞。

**常用命令**：
```bash
python -m py_compile engine/signals.py engine/strategic.py engine/tactical.py engine/backtest.py engine/reports.py engine/app.py
python engine/tests/test_engine.py          # 回归测试（无网络，秒级）
python engine/signals.py                     # 生成本周信号 → engine/signals.json
python engine/validate_flags.py --init-empty # 无重大事件时初始化空旗标
python engine/reports.py                     # 归档可视化周报 → reports/<id>/
python engine/backtest.py                    # 回测（--json 出结构化、--refresh 联网重取种子）
python engine/app.py                          # 本地驾驶舱 http://127.0.0.1:5057（PORT=5058 可改端口）
node --check engine/web/app.js               # 前端语法检查
```

**数据与文件（gitignore 现状）**：
- **已入库**（私人仓，用户确认无隐私风险）：`portfolio.yaml`、`investor_profile.yaml`、`strategy.yaml`（配置）；`reports/`（周报归档）、`journal/`（执行/学习/NAV 记录）；种子数据 `engine/data/*.csv` + `meta.json`。
- **仍忽略**（每次运行重写/高频 churn）：`engine/signals.json`、`engine/flags.json`、`engine/cache/`；以及 `.claude/settings.local.json`、`.DS_Store`、`__pycache__/`、`*.pyc`。
- **一键同步**：根目录 `sync.command`（mac）/ `sync.bat`（Windows）→ `git add` 个人数据后 commit + `pull --rebase` + push；`signals.json`/`flags.json` 不同步（本地重算）。`.gitattributes` 固定 `*.bat`=CRLF、`*.command`=LF。
- ⚠️ `.claude/settings.local.json` 不入库 → 换机器后要重新加 `Bash(npx:*)` 才能用 westock。

---

## 6. 回测口径与发现（数字随当前持仓而变）

- ETF 可交易段当前约 **2021-11 → 2026-06（~4.3 年，受科创50 2020 上市拖累交集）**；指数代理长段 **2006 → 2026（~19.6 年）**，长段剔除并分摊黄金/QDII/创业板/科创50（价格指数未含分红，主要看回撤轮廓、非精确收益）。
- **趋势过滤定位为"危机保险"非增收**：长样本里把最大回撤从约 −42% 压到约 −24%，但平静期摊薄收益。`risk_profile=进取/平衡` 下趋势仅作展示信号 + `trend_alerts` 提醒，不自动调仓。
- **DCA 实测**（ETF 段 ~4.3 年、16 滚动窗口）：一次性 1.46x / 分6月 1.47x（56% 窗口跑赢一次性）/ 分12、24 月略逊，回撤均约 −12.8%——符合"上行市一次性通常更优、分批主要降择时后悔"。建仓别拖太久（6 个月一档已拿到大部分平滑效果）。
- **再平衡专题**（5/25 法则、频率选择、22 年代理回测证据）详见 [`REBALANCING.md`](REBALANCING.md)。
