# 交接文档 / Project Handoff（单一权威文档）

> 本文件已合并原 `PLAN.md`（路线图）、`CHANGELOG.md`（变更史）、`HANDOFF.md`（项目状态）。
> **下一个接手的 agent：先完整读这份。** 个股推荐 / 高频 / 自动下单都不做。
>
> **定位**：**私人投顾（单一所有者自用）**——**输出带理由的建议、不承诺收益、不自动下单**；人在环，最终拍板与下单永远在所有者手里；**ETF-only**；不编造数据（缺失就如实标"不可用/缺失"）。（自用工具、不对外提供投顾服务，不涉及"类投顾"合规边界；个股推荐/高频/自动下单仍不做。）

---

## 0. 协作规则 / 单一事实源

- 核心代码只在 `engine/`。两个 agent 入口 `.claude/skills/weekly-briefing/SKILL.md`、`.agents/skills/weekly-briefing/SKILL.md` **只是薄包装**，不要把 `signals.py` / `backtest.py` / app 逻辑拷进 agent 目录。
- 改行为：**先改 `engine/` 实现**，再按需更新 `README.md` / 两个 SKILL（仅当接口变化）。
- 每改一处：跑 `$env:UV_CACHE_DIR='F:\MakeMoney\.uv-cache'; uv run --offline --with-requirements engine\requirements.txt python -m unittest engine.tests.test_engine`（当前 **325 用例**）必须全绿；前端改完 `node --check engine/web/app.js`。

## 0A. 2026-06-07 当前权威状态

以下内容覆盖本文后续章节中仍保留的旧流程描述。

- 长期战略已经收敛为一条权威路径：保存长期战略设置后，系统自动计算场外稳健桶与本工具计划最大使用金额，构建模型组合，并在通过约束时直接应用。旧的“建议目标权重”、手动覆盖建议、季度墙和影子组合审查均已移除。
- 当前长期参数：总资金 170 万元、目标年化 7%、规划年限 30 年、最大回撤约束 30%、失业月开销 6000 元、失业缓冲 5 年、压力后储备 12 个月。由此自动计算场外稳健桶 43.2 万元，本工具计划最大使用金额 126.8 万元。
- 当前已应用的权威模型组合：`511010` 30%、`510300` 17%、`510500` 18%、`513500` 20%、`513100` 5%、`159915` 5%、`588000` 5%；`512890` 与 `518880` 为 0%。
- 币种集中约束已改为 `single_risk_currency_exposure_max`，只约束风险资产的单一币种暴露，债券/现金类资产不计入。
- “当前 ETF 是否合适”已补全候选引入闭环：同资产类别的 universe/watchlist ETF 可作为替代候选；候选必须通过最近一次产品准入审查，之后可经 `/api/strategic/roles/introduce` 引入对应战略角色。目前没有符合条件的替代候选。
- “复杂策略是否值得保留”已改为“模型组合是否优于简单组合”，结果顶部直接给出保留复杂度、建议简化或证据不足的结论。当前回测结论倾向“建议简化”。
- “决策与组合”启动时自动加载配置和行情；首页按“我的组合 → 本周决策 → 调仓记录”纵向排列，顶部数据改为小字状态说明，组合表展示目标 ETF 与已购买 ETF，仅保留一个“调仓”入口。
- `start_windows.bat` 与 `start_mac.command` 会在启动前清理占用 5057 端口的旧 dashboard 进程；若端口由无关程序占用则停止启动，不会误杀。
- `journal/strategic_reviews/` 与新的时间戳 `reports/` 目录属于历史/诊断生成物，不再是产品主流程的一部分；除非明确需要同步诊断快照，否则不要纳入提交。

## 0B. 战略引擎对抗式审计阻断项 —— ✅ 5 阻断项 + 4 护栏全部落地（2026-06-08，批 1-4 完成）

**最新状态（2026-06-08）**：批 1（安全闸）+ 批 2（人在环）+ 批 3（建模判断·所有者定黄金/防御 5% 下限）+ 批 4（重建证据）**全部完成**，测试 245→**261** 全绿。5 个阻断项全修、4 条少额真金护栏全就位。**→ 可进入所有者拍板的"少额真金验证"阶段。** **🟡 精化项已全部清零（2026-06-08）**：协方差接入 construct 接受判定（§0C #3）、product_score 缺子分惩罚（effective_total）、单成员角色 footgun + 网格 ceil/floor、应用审计痕迹（mode=applied）均已落地；成长桶显式保守区间亦于收尾用数据驱动算法补上。→ §0B 阻断项 + 护栏 + 精化项全清。

**所有者治理决策（已拍板）**：取消两季度影子（§16.4），**此战略引擎作为本工具唯一的长期战略引擎**；先用**少额真金**验证可行性，再逐步加仓。→ 注意：按 §0A，引擎**早已是 live**，下列漏洞曾是**当前线上行为**（批 1-4 已逐条修复）。

**审计来源**：6 维多 agent 对抗式审计（47 agent / 2.27M tokens / 20 条发现**全部双视角核验为真**），裁决 **go-with-fixes**。完整结果存于
`C:\Users\zim\AppData\Local\Temp\claude\F--MakeMoney\32dcc4e0-dba7-4b5e-970f-78153e985a19\tasks\w34zf3wc8.output`（换机会失效，要点已抄录于下）；工作流脚本 `…/workflows/scripts/strategic-engine-audit-wf_c43a067e-f84.js`。

**两处先前口头结论被审计推翻（别再据此行动）**：① 黄金被压到 0% 的主导是**"保守收益缺口"排序键**，不是 `return_first`；② 切 `selection_priority=balanced/defensive_first` 在 live 配置下是**空操作**（排序元组字节相同）——唯一有效杠杆是**给黄金/防御加下限**或**解耦构建压力预算与展示回撤**。

### 🔴 阻断项（修完才可托付真金）

1. ✅ **已修（批 1，2026-06-08）** **[CRITICAL] 质量缓存缺失/过期 → 硬准入 fail-open**：缓存为空或 >7 天 → §8.2 准入检查被跳过、组合仍标 `passed`、自动应用真实权重。**已落地**：`construct_strategic_portfolio` 加 `require_quality` 参数（live 调用打开）——无质量/准入记录的 code 按**未准入** fail-closed（incumbent 封顶当前权重 freeze、非持仓剔除），不再当"已准入"放行；`_run_construct` 当 `product_quality_status∈{missing,stale}` 或任一角色成员无记录 → 置 `quality_gate.blocked` + 把 `validation_status` 降为 `blocked_quality_data`，apply/save_config 据此拒绝。**实测**：真实配置缓存缺失时 construct 直接 `no_feasible`/blocked、apply 全被拒；注入新鲜全准入缓存则正常构建。
2. ✅ **已修（早于审计的 57f39db 已有 incumbent freeze；批 1 由 #1 补全闭环）** **[HIGH] 被准入拒绝的 ETF 仍获正权重**：`513500/513100/588000`（admitted=false）。**机制**：blocked incumbent 经 `restricted_max` 封顶在当前权重（只减不加），`violations()` 终值校验「权重高于当前 → violated」；非 incumbent 直接剔除。⚠️此前被 #1 的 fail-open 架空（缓存空时 `quality is None` 跳过整段），#1 修完后真正生效。`test_failed_incumbent_cannot_be_increased` 覆盖。**⚠️ 修订（2026-06-08，所有者决定）**：审计当时把"**溢价>±3%**"也算成 #2 的准入阻断；后经所有者厘清——**折溢价是执行时点问题、不属长期准入**，已从 §8.2 硬准入移出（见 §3「两层把关」与变更历史"折溢价移出长期准入"）。所以现在 #2 的"准入拒绝"只剩**真实结构性/限购阻断**（如 QDII 不可申购），折溢价改由执行质量闸在下单时拦。
3. ✅ **已修（批 2，2026-06-08）** **[HIGH] 保存设置即静默重写全部目标权重**：旧 save_config 改任意参数就重跑 construct 并写 `portfolio.yaml`。**已落地**：`save_config` 现在**只持久化 profile/risk/portfolio**（写出的 target_weight = 用户提交值，即当前权重），**不再调用 `_run_construct`/`_apply_constructed_allocation`**、绝不自动改 target_weight；返回 `manual_apply_required`。重配走显式三步：`/api/strategic/construct`（看 diff）→ `/api/strategic/apply`（批 1 的指纹 + ≥15pp 大跳变二次确认）。前端 saveConfig 改为平静的"已保存、权重不变、去模型组合手动应用"。**实测**：用真实持仓提交保存 → construct/apply 均未被调用、写出的权重与提交值逐一相同、disk 未变。
4. ✅ **已修（批 1，2026-06-08）** **[HIGH] apply 无完整性校验**：`/api/strategic/apply` 曾忽略请求体直接应用、`renderConstruct` 不披露质量。**已落地**：客户端回显其评审过的 `input_fingerprint`（`CURRENT_CONSTRUCT.input_fingerprint`），服务端重算、不一致回 **409 stale**；缺指纹回 400；apply/construct 边界披露 `product_quality_status` + `quality_gate`；单产品 target_weight 跳变 **≥15pp**（`LARGE_MOVE_THRESHOLD`）回 **409 needs_confirmation** + diff，须 `confirm_large_moves` 二次确认；前端 `applyStrategicConstruct` 处理两种 409。
5. ✅ **已修（批 4，2026-06-08）** **[HIGH] 上线证据（对比回测）结构性失真**。**已落地**：① **统一剔除未覆盖品种**——无 20 年长代理的成长卫星（159915/588000）现从**所有**被比组合统一剔除并各自归一（`covered` 子集 + `restrict()`），不再被静默按比例摊回其它桶（旧实现把权威构建悄悄抬向美股、与"仅核心"不可比）；剔除权重显式披露 `excluded_weight`（实测 权威构建 13%、当前 10%）。② **债券票息 Calmar 敏感性**——`build_full_panel(bond_carry=)` + 零息(0%)重跑一遍，每行附 `calmar_zero_coupon`；实测"更低权益"Calmar 在 +3% 与 0% 两列都最高（0.29/0.27）→ 结论**不被票息假设驱动**。③ **去退化重复基准**——按代理目标签名去重（gold=0 时"无黄金"≡"权威构建"只测一次）；批3 黄金下限后当前已无重复。CLI/JSON/前端均披露三项。**诚实结论**：可代理子集上"更低权益"风险调整后≥权威构建（倾向简化），但 china_growth 13% 增量价值**不在本回测覆盖内**、不能据此否定它——这是诚实的样本局限。

### 🟡 加资金前应修（中/低）

- ✅ **已修（批 3）** `single_satellite_max` **暴露建模**：给每个 universe instrument 加显式 `exposure_id`（construct/backtest 的 `exposure_of` 优先用它，永不退回 proxy_index/code——修了红利低波因 proxy=sh000300 被当成沪深300 的隐患）；single_satellite_max 已锚定每只 ETF 的全组合最终权重（`evaluate_instruments` 用 projected 权重）；加 `test_single_satellite_cap_binds` 证明上限真能 binding。
- ✅ **已修（2026-06-08）** `product_score` **关键子分缺失反而抬分**（原 `strategic.py:263-266`）：旧 `total` 仅按可得子分归一 → 把差的关键子分（如费率）藏成缺失反而抬高 total，而 `quality_penalty/product_key` 只读 total → 数据贫乏 ETF 反超透明的（违反"missing≠neutral"）。**已落地**：`product_score` 新增 `effective_total`——关键子分（成本/流动性/规模）缺失=惩罚而非丢弃（把缺失关键权重留在分母、视作 0 分；无关键缺失时 == total；关键全缺随 total→None 全额惩罚）；新增 `_effective_score` 助手，`product_key`（primary 选型）与 `quality_penalty` 改读 effective_total（向后兼容只含 total 的手工 quality dict）。`total` 保持诚实、仅供展示。测试 303→**306**（+3：藏差子分不反超展示总分 / primary 选型用有效分 / 惩罚用有效分）。
- ✅ **已修（批 3 + 收尾）** `return_haircut` **边界校验 + 成长桶显式区间**：`validate_strategy` 校验 `0≤return_haircut≤0.15` + per-sleeve 区间边界 + 断言 `conservative≤central≤optimistic`。**收尾（2026-06-08，数据驱动）**：成长/QDII 桶不再用对称 ±3%——`backtest.compute_return_intervals` 按**历史年化波动率缩放折扣**（系数标定为让 A 股核心得 3%）+ **成长桶保守值封顶在核心权益保守值 4%**（最坏情形不假设乐观成长跑赢普通股票，因数据显示纳指波动其实低于沪深300、污染来自乐观的"中枢"而非波动）；数值经 `backtest.py --return-intervals` 算出登记到 `strategy.yaml`（global_equity 5.71%/10.29%、global_growth 4%/12.61%、china_growth 4%/12.86%）。修掉了"成长桶保守收益≥权益中枢污染排序键"。
- ✅ **已修（批 3·解耦+下限部分；§0C #3 收尾协方差）** **单向量线性压力 + 黄金压 0**：已加非零黄金/防御下限（各 5%，所有者拍板，写入 `strategic_policy.roles`）；已**解耦构建压力预算与展示回撤**（`construct_stress_budget`，null→默认=max_acceptable_drawdown）。**✅ 协方差已接进 construct 接受判定（§0C #3，2026-06-08）**：`cov_stress=z×年化波动×etf_share` + opt-in 闸（`enforce_cov_stress`/`min_effective_bets`）+ live `_run_construct` 真传协方差，详见 §0C #3。
- ✅ **已修（2026-06-08）** **取消 shadow 后无应用审计痕迹**（原 `app.py:1533-1545`）：apply 改 `portfolio.yaml` 不记 who/when/fingerprint/old→new diff，算出的 `input_fingerprint` 被丢弃。**已落地**：`reports.save_strategic_apply`（落 `journal/strategic_applies/<id>.json`：mode=applied + fingerprint/policy_version/quality_status/old→new 权重 diff/触发源/ISO 时间戳）+ `load_strategic_applies`；`/api/strategic/apply` 成功写盘后 best-effort 记审计（写失败**不回滚**已应用组合——组合即真相、可由 git diff 复核；错误透传前端）并在响应回 `audit`；新增 `GET /api/strategic/applies`（最近 50 条）。单一所有者无认证 → source 记触发入口作 who 代理。测试 306→**308**（+2：reports 写读 + 端点落审计）。**注**：原"归档孤立 shadow 快照"已 moot——strategic `mode=shadow` 快照写入器随 §16.4 取消时已删（现存 `shadow` 全是 tactical 影子、另一活跃特性，不涉及此项）。**遗留（可选·归 §0C #5 UI）**：审计列表前端展示，读端点已就绪。
- ✅ **已修（2026-06-08）** **单成员核心角色 + 网格取整可触发伪 `no_feasible`**（`strategic.py` `_enumerate_role_allocations`）：旧 `int(round(lo/step))/int(round(hi/step))` 把非 step 倍数的区间界悄悄抬/压过界（floor 0.02→0.0 破下限、cap 0.08→0.10 破上限），区间窄于一格时静默产空枚举→裸 no_feasible。**已落地**：① 网格改 **ε-guarded `ceil(lo)/floor(hi)`**（保守内逼近、绝不越界；1e-9 消 FP 抖动——实测对全部 live 倍数界与 round 完全一致、零回归）；② 新增 `_structural_infeasibility`：枚举为空时给可读病因——(a) 区间窄于一格放不下网格点、(b) 角色下限 > 其全部选中成员受限上限之和（单成员失败准入 incumbent 封顶在当前权重、抬不到下限的 footgun），主构建 no_feasible 分支用它替代「no portfolio satisfies…」泛化语；两者都**保留人工覆盖**（所有者可放宽下限或换/准入替代品）。测试 308→**311**（+3）。**注**：live 全部区间界皆 0.05 倍数 → 此前无实际误判，属潜在健壮性修复。

### 修复编排（批次）

- ✅ **批 1 安全闸（已完成 2026-06-08，纯正确性）**：阻断项 #1 + #2(闭环) + #4 + 单产品权重 ≥15pp 二次确认护栏。改 `strategic.py`(require_quality fail-closed)/`app.py`(_run_construct 质量闸 + apply 指纹/大跳变闸 + save_config auto_apply_held + `_large_target_moves`)/`app.js`(质量披露 + 指纹回显 + 两种 409 处理)/测试 245→**255**。**换机后下一步从批 2 开工。**
- ✅ **批 2 人在环（已完成 2026-06-08）**：阻断项 #3——`save_config` 彻底不再自动应用（不跑 construct、不写 target_weight），重配走显式三步。改 `app.py`(save_config 精简)/`app.js`(saveConfig 平静提示)/测试(去 4 个旧自动应用用例、加 2 个"绝不自动应用/权重不变"用例，255→**253**)。**换机后下一步从批 3 开工。**
- ✅ **批 3 建模判断（已完成 2026-06-08）**：黄金/防御各 5% 下限（所有者拍板）+ 解耦压力预算（`construct_stress_budget`）+ `single_satellite_max` 暴露建模（显式 `exposure_id`）+ 假设边界校验。改 `strategy.yaml`(policy_version 1→2/floors/exposure_id/construct_stress_budget)/`app.py`(_run_construct 用 exposure_id + 解耦预算)/`signals.py`(validate_strategy 假设边界)/`backtest.py`(exposure_id 对齐)/`app.js`(显示压力预算)/测试 253→**259**。**实测**：真实配置构建 黄金 5%/防御 5%（此前 0%）、validate 通过、预算 null→30%。**未尽**（见 🟡）：成长/QDII 桶显式保守区间（需假设标定决策）、协方差接入 construct 接受判定。
- ✅ **批 4 重建证据（已完成 2026-06-08）**：阻断项 #5——对比回测现可作诚实证据。改 `backtest.py`(build_full_panel 加 bond_carry / simulate_strategic_comparison 统一剔除未覆盖品种 + 去退化重复 + 零息 Calmar 敏感性 + excluded_weight 披露)/`app.js`(前端披露三项 + Calmar零息列)/测试 259→**261**。**实测**(21 年全收益面板)：可代理子集上"更低权益"Calmar 最高(0.29，零息 0.27 仍最高→不被票息驱动)，倾向简化；但 china_growth 13% 增量价值不在覆盖内。**全部 5 个阻断项 + 4 条护栏已落地** → 可进入"少额真金验证"阶段（所有者治理决策）。

### 少额真金期护栏（§small_capital_guardrails）

1. **质量数据 fail-closed**：缓存缺失/>7 天 或 任一选中 instrument 无准入记录 → 拒绝 apply 并禁用按钮，绝不让未审核 instrument 进真实权重。
2. **单产品权重跳变闸**：单次 apply 中任一 target_weight 变动 >~15-20pp 需显式二次确认并展示当前 vs 新 diff（防一次重配静默搬动大额，如 0.07→0.30）。
3. **强制 diff + 指纹完整性**：只应用用户评审过的那版 construct（客户端回显 fingerprint，服务端重算、不符即拒）；绝不从 save_config 自动应用。
4. **初始真金敞口绑定已验证覆盖**：未被真实长样本回测覆盖、或未单独小额验证的成长卫星（159915/588000）和任何失败准入 ETF，**先不投**；首期资金上限锚定"已通过准入且已回测"的权重比例。

### 关键实现要点（本轮答疑确认，别踩）

- **fail-closed 必须做成"不让加（权重封顶在当前值/freeze）"，不是"整只剔除/逼到无解"**：美股核心角色在 universe 里只有 `513500` 一只，若把不合格的整只剔除，该角色无合格成员 → 引擎直接 `no_feasible`、唯一引擎产不出组合。
- **闸绝不能造成"死胡同"（优雅降级 + 保留人工覆盖）**：闸只拦"加仓不合格 ETF"这一个动作，**不拦**减仓、也不拦换到合格资产（`510300/510500/511010/518880/512890` 均合格无溢价问题）；改善烂组合的路（减贵的 + 加合格的好的）永远开着。construct 遇到不可填角色时应**"建好可行部分 + 显式标注未填部分（如美股因溢价只持有不加）+ 不强买"**，输出"通过+部分冻结"而非硬 `no_feasible`。任何阻断都必须保留**人工覆盖**入口（人在环：所有者可有意识地接受溢价/手动调整）——闸只拦"自动应用"，从不拦"有意识的手动决定"。关键澄清：当前组合真正弱点（**黄金0/防御0**）归**建模 bug（批 3）而非准入闸**——黄金/防御都是合格 A 股资产，批 3 修完可用全合格资产搭出更优组合、零准入阻碍。所以"新的不给过 + 当前不好"不构成死胡同。
- **目前已应用战略的处置**：作为"持有"→ 过（只在"加仓超过当前权重"时才 violated，守现状不触发，不逼卖）；作为"引擎重构的新目标"→ 那三只 QDII 加仓被拦。513500/513100 是 QDII，**溢价/限购是常态**，能否过取决于**运行那一刻的实时溢价**——闸不是永久否决战略，是"现在别贵着加"。
- 换机修前先重拉实时准入状态复核：`python engine/app.py` 后开战略审视，或直接看 `_etf_quality_for` 的 admission 输出（06-07 缓存里 admitted=false 的是当时溢价，明天可能已回到 ±3% 内）。

## 0C. 五维评分提升计划 —— 把分阶段审查暴露的短板逐项补到 8+（2026-06-08 立项）

**来源**：2026-06-08 对全项目做分阶段审查（阶段0 功能盘点 / 阶段1 正确性 / 阶段2 风控 / 阶段3 赢率证据，279 单测全绿），并按 5 维打分。本节是把分数全部抬到 ≥8 的执行计划。**单一权威，执行进度在每项前用 ⏳未开始 / 🔨进行中 / ✅完成 标记。**

**审查分数（满分 10）**：工程正确性 **8.5** / 认知诚实 **9.0** / 风控能力 **5.5** / 赢率证据 **4.0** / 产品适配 **7.0**；综合 ≈6.5–7。最弱两项：
- **风控**：压力是单一冲击向量（`equity -30%` 比历史 2008(-73%)/2015(-46%) 温和）、无协方差、无汇率、MA200“止损”线上只提醒不执行。
- **证据**：所有“更优”均样本内；最强的趋势过滤线上不执行；“子期三段”是同一全样本权重切片、非真 OOS。

**诚实天花板（别误读“8 分”）**：维度 4 的 8 分 ≠ “证明能赚钱”。前瞻证据要时间。这里的 8 = “工具做的每个主张，严谨度都配得上、绝不过度声称”。2 只成长卫星（159915/588000）无长数据 → 升级后**仍标“未评估”**。

### 工作项（按性价比排序，带验收线）

1. ✅ **[维度3·最高杠杆] 多情景历史压力（已完成 2026-06-08，引擎层）**：用已有长面板按各资产类 2008/2015/2018/2020/2022 **真实峰谷**标定危机冲击向量，周度风险预算新增“若 20XX 重演”最坏情景披露。**落地**：`backtest.py` 加 `compute_crisis_scenarios()` + `--stress-scenarios` CLI（据 `engine/data/idx_*.csv` 种子离线标定；锚=窗口内跌最深的权益代理，全资产用同一对峰谷日期算横截面，如实捕捉债/金对冲）；`signals.py` 加 `HISTORICAL_CRISIS_SCENARIOS` 常量 + `load_historical_scenarios` + `estimate_stress_scenarios`（同情景内对冲资产受益抵损，体现真实分散），`main()` risk_budget 增 `historical_scenarios/worst_scenario/whole_portfolio_worst_scenario_drawdown_at_planned/worst_scenario_note`；测试 286（+7 `TestStressScenarios`）全绿。**实测（真实配置）**：旧单情景全组合 17.5% → **2008 标定情景 ETF 桶 −38%、按计划满仓(126.8万)折算全组合 −28.5%**（旧拍脑袋向量把尾部低估约 11pp；28.5% 仅微低于 live 30% 预算、对 §1 的 20% 口径则已击穿）。各情景 ETF 桶口径：2008 −38% / 2015 −23.5% / 2022 −14.4% / 2020 −13.8% / 2018 −12.2%。**阶段2“极端充分性·无法验证”→ 现已“已验证”**。
   - ⚠️ **刻意不改**：① 战略构建接受闸仍读旧 `load_stress_scenarios`（把 −71% 权益喂进 construct 会逼出极端保守组合，属 #3 接受判定改造）；② 硬 `breached` 仍按单情景（新情景仅 `scenario_breached` 软披露）。**遗留**：前端 app.js 落位归 #5；`worst_scenario_note` 已在 `signals.json` 但 UI 未渲染。
   - ✅ **口径已统一（2026-06-08 所有者拍板：一切以 live 为准）**：§1 已对齐 live（max_dd 30% / planned 126.8万 / stable 43.2万 / target 7%），旧 20%/100万/70万/8% 废弃。引擎用的就是 live 值，无需再改。
2. ✅ **[维度4] 真 walk-forward + 证据台账（已完成 2026-06-08，引擎层）**：把战略对比的“子期三段”（同一份全样本权重切片）升级为**真 walk-forward**——每折只用【过去】数据估协方差→构建权威组合→机械派生简化基准（`derive_comparison_portfolios`，非事后挑选）→在 **held-out 未来段**评估；并产出证据台账给每条“更优”定档。**落地**：`backtest.py` 加 `walk_forward_strategic()` + `--walk-forward` CLI（扩张窗口，训练严格早于测试，无前视）、`EVIDENCE_CLAIMS`/`build_evidence_ledger()` + `--evidence` CLI（档：logic<in_sample<walk_forward<live）；测试 286→**288**（+2 `TestWalkForwardEvidence`，含 no-lookahead 断言）全绿。**实测（真实长面板）**：3 折（测试段 2011–16 / 2016–21 / 2021–26）中 **3 折简化≥构建** → “建议简化”**样本外仍成立**（此前仅样本内）。台账定档：`simplify` 升为 **walk_forward**；`MA200趋势/估值均值回归/分散/DCA` 诚实保留 **in_sample** 并各带局限；无任何主张被标 live。
   - **遗留（归 #5）**：台账与 walk-forward 结论的前端落位——UI 里给每条“更优”显示其证据档、确保措辞不强过 tier。引擎产出已就绪（`--evidence --json` / `--walk-forward --json`）。
   - **诚实边界**：规则仍是看着历史写的（非全新数据）；战略构建权重以假设为主、协方差只是次要输入，故 walk-forward 主要检验“简化结论”在 disjoint 未来段是否稳定；成长卫星 159915/588000 无长代理、不在覆盖内。真·实盘 OOS 仍须 #6 记账积累。
3. ✅ **[维度3] 协方差进 construct 接受判定（已完成 2026-06-08）+ FX 现状厘清**：
   - ✅ **协方差进接受判定（§0B 遗留 [未做] 已闭环）**：`strategic.construct_strategic_portfolio` 加 `cov_stress_z`（默认 2.0）；`evaluate_instruments` 算**协方差隐含全组合压力** `cov_stress = z×年化波动×etf_share`（真实相关、覆盖有协方差的子集）+ 覆盖率 `cov_covered`；`violations()` 加两个 **opt-in** 闸（`caps.enforce_cov_stress` / `caps.min_effective_bets`，缺省关——开了也只让"更安全"、不制造死胡同）；snapshot 暴露 `covariance_vol/covariance_stress/covariance_covered_weight`。**关键**：`app.py _run_construct` 现**懒加载 build_full_panel 算周频协方差**传入（读缓存种子、离线快），令 **live 构建的接受判定真正用上协方差**（此前 live 传 covariance=None、只有回测路径有）。`app.js renderConstruct` 加「协方差压力 X%（真实相关·覆盖 Y%）｜有效风险源 Z」。测试 292→**296**（+4 `TestCovarianceAcceptGate`，含"impossible 分散度→无解""真实相关压力<预算→不死胡同"）。**实测（Preview 真机）**：live 构建 linear 25.8% vs **协方差 20.2%（真实相关credit分散）、覆盖 90%、eff_bets 3.92、status passed**；秒级。
   - 🟢 **FX 现状（厘清后发现 live 路径已覆盖）**：construct 接受判定的最坏情景集**已含「人民币升值」**（QDII −10%），且 `caps.single_risk_currency_exposure_max=0.55` **已硬约束美元单一货币暴露**，UI 显示「风险货币」。**故 live 风控的 FX 既有压力情景、又有暴露上限**——FX 不是 live 缺口。
   - ⏳ **唯一 FX 残留（数据依赖，已记 deferred）**：长**回测面板** QDII 仍用裸美元指数收益、**不做 USD/CNY 折算**（`build_full_panel` 注释已标"忽略汇率"），#1 的历史危机标定同理未含当期 CNY 移动。补它需 20 年 USD/CNY 种子（联网取一次后入库，保离线可复现）——属数据获取任务，**不挡 live 风控**，待有网时一次性补 `idx_usdcny.csv` 并在 panel 给 QDII sleeve 叠加汇率收益。
4. ✅ **[维度3+4] 趋势提醒→建议动作（已完成 2026-06-08）**：MA200 跌破从被动 `trend_alerts` 升级为**具体减仓建议**（移到债券/防御）+ 回测量化的回撤差，补上 Phase 2 点出的"回测归功趋势保护、线上却不执行"缺口。**落地**：`backtest.py` 加 `trend_protection_benefit()` + `--trend-benefit` CLI（长面板"趋势过滤 vs 静态"最大回撤差）；`signals.py` 加 `TREND_PROTECTION_BENEFIT` 常量 + `load_trend_protection` + `build_trend_derisk`（纯函数：每只跌破 MA200 的权益→减仓金额=当前市值、reserve=universe 的 `asset:bond`、actionable 受 min_trade 约束），`main()` trend_alerts 改为带 `derisk_amount/reserve_code/actionable/blocked_reasons`、新增 `trend_protection` 字段，CLI 危机保险段同口径；`app.js wkAlerts` 渲染「趋势减仓建议」（带金额→债券 + "不动手约多扛 X% 回撤" + "建议不是指令、去调仓执行"）。**仍人确认、不自动下单**。测试 296→**302**（+6 `TestTrendDerisk`）。**实测（标定）**：21 年长面板 静态最大回撤 −42% → 趋势过滤 −21%（**少扛约 21pp**，年化反而 11.7% vs 11.5% 不被 whipsaw 拖累）。**实测（Preview 真机）**：live 红利低波 512890 跌破 → 「✅ 减 约 ¥1,053 → 国债ETF；不动手约多扛 21pp 回撤」直接显示在本周决策。**诚实**：回撤差样本内、规则看着历史写；线上仅建议、由人按金额到「调仓」执行。
5. ✅ **[维度1+5] Sharpe 加 rf + UI 落位/状态收尾（已完成 2026-06-08）**：
   - ✅ **UI 落位"看见"（已完成 2026-06-08）**：把 #1/#2 的产出渲染进驾驶舱。**落地**：`app.js` `wkRiskBudget` 在「本周决策 → 查看完整判断依据」里新增「**历史尾部压力（据真实峰谷标定）**」——显示 worst_scenario_note（2008 重演 ETF 桶 −38% / 按计划满仓全组合 −28.5%）+ 5 情景可展开表；`renderEvidenceLedger` + `tierBadge` 在「战略对比」面板下新增「**证据台账**」（每条主张带证据档徽标 + 真 walk-forward 三折明细），由 `loadStrategicBacktest` 跑完对比后自动加载；`app.py` 加 `POST /api/strategic/evidence`（跑 `backtest.py --evidence --json`）。**实测（Preview MCP）**：周报尾部段渲染出 −28.5% + 五情景表；`/api/strategic/evidence` 200/4s 返回 5 主张（4 in_sample + simplify=walk_forward）、verdict「样本外仍倾向简化」、3 折明细；`node --check` + 288 测试全绿。⚠️ Flask `debug=False` 不自动重载，改 app.py 后需重启 dashboard（start_mac/windows）。
   - ✅ **Sharpe 加 rf（已完成 2026-06-08，维度1 那一刀）**：`backtest.py` 加 `RISK_FREE_RATE=0.02` 常量 + `sharpe_ratio(cagr, vol, rf)` 纯函数（**真夏普 =（年化−rf）/波动**），替换两处裸 `cagr/vol`（`run_tactical_comparison` + 主 ETF 表）；CLI 两张表加「夏普=（年化−无风险2%）/波动」脚注；顺手删了 `_print_tactical_table` 的重复表头。前端不显示 sharpe，无影响。测试 288→**292**（+4 `TestSharpeRatio`，含"旧裸口径高估 rf/vol"断言）。**阶段1 唯一瑕疵已清，维度1 8.5→9。**
   - 🟡 **状态收尾（评估后基本已覆盖）**：§5 阶段1/2 已落地「同日只采最后一份正式决策周期」+「首页/本周决策/调仓统一读活动周期」+「配置指纹失效保护」。当前无可复现的具体不顺手点；如所有者再遇到状态不一致，指认具体症状再收。**#5 视为完成。**
6. ✅ **[维度4·长线] 实盘 NAV 记账（发现 WS3 已建·已接进证据台账，2026-06-08）**：核查发现 §5 P2-2 早已落地为 **WS3**——`reports.py` `save_nav_snapshot`（每份正式周报落一份 `journal/nav/<date>.json`）+ `cash_flows_from_executions` + `compute_twr`/`compute_mwr`/`performance_summary`（TWR 时间加权、MWR=XIRR、剔除注入本金、沪深300 基准、费用单列、诚实注脚）+ `GET /api/performance` + `app.js #performancePanel` 渲染 + 现金增减不污染 TWR/MWR。**时钟已在走**（NAV 快照自 2026-06-07）。**本轮新增**：把真实业绩接进 #2 证据台账——`build_evidence_ledger` 加 `live_track_record` 行，`tier` 随快照数升级（**≥8 周快照 + TWR 可得 → `live`**，否则诚实标 `实盘·积累中` + 快照数/起始日）；`app.js` 给该行专属「实盘·积累中」徽标。测试 302→**303**（+1，并把"无主张冒充 live"断言精确为"除实盘行外"）。**实测**：`/api/performance` 返回 2 快照、TWR −2.9%(1日)、MWR 待现金流符号变化、注脚齐全；证据台账渲染「工具的真实风险调整收益（实盘）｜实盘·积累中｜2 个 NAV 快照、自 2026-06-07（需 ≥8 周点亮 live 档）」。**诚实天花板**：维度4 到真 8 须 ≥8 周快照积累——**代码已就位、时钟在走、台账可见地累积**，剩下是时间不是代码。**→ §5 的"P2-2 暂缓"说法已废，以本条为准。**

### 维度2护栏（别在升级中弄坏诚实）
新增的每个风险/证据数字都要带“证据等级”标签；UI 里没有任何主张强过它的证据档。

**预计 ~6–7 工作日工程量（实盘记账除外）。执行从 #1 开工。**

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
**必须诚实保留**：7% 即便对 ETF 桶也偏进取（回测 ETF 段约 4–6%、长代理段约 8% 但伴 −40%+ 回撤）；且 §0C #1 标定显示 **2008 级尾部下 ETF 桶 −38%、按计划满仓折算全组合 −28.5%，逼近 30% 预算**——需权益重仓 + 容忍股票级波动，工具要把权衡量化讲清，绝不暗示稳赚。

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

1. ✅ **P2-2 真实业绩跟踪（已落地为 WS3，§0C #6 收尾）**：`reports.save_nav_snapshot`（每周报落 `journal/nav/`）+ `compute_twr`/`compute_mwr` + `performance_summary`（剔除注入本金、沪深300 基准、费用单列、诚实注脚）+ `GET /api/performance` + `#performancePanel`；已接进证据台账 `live_track_record` 行（≥8 周快照点亮 `live` 档）。时钟自 2026-06-07 在走。详见 §0C #6。~~暂缓~~ 说法作废。
2. ✅ **已接入（2026-06-08，两条腿方案）** **A 股成长估值**：选源核查——`stock_index_pe_lg`(legulegu) 确认不收 创业板指/科创50/中证红利低波(KeyError)；百度 `stock_zh_valuation_baidu` 把指数码当**同名个股**返回(陷阱:中证红利"PE"35 实为佳电股份,官方仅 8.48)、已排除；csindex 官方有精确当前 PE 但静态文件只滚动 ~20 天、**算不出长分位**。落地：① **创业板 159915 → 创业板50 代理**(legulegu 2009~、强相关；config `valuation_proxy`；UI 标"代理·近似"，**可触发 cheap/rich**)；② **科创50 588000 / 红利低波 512890 → csindex 官方按日自建累积**(config `valuation_csindex`=000688/H30269；`fetch_valuation_csindex` 把 ~20 天窗口并进 `journal/valuation/<code>.json` 按日去重、**时钟从接入日(2026-06-08)起走**；历史 < `VALUATION_ACCUM_MIN_YEARS`(3 年) → `valuation_accumulating` 态：**只显示当前 PE + "分位积累中(N 月)"、percentile=None、绝不冒充信号**；满 3 年后自动升级为自身精确分位)。前端 `valCell` 渲染 代理/积累 两态 + hover tooltip 说明数据历史(已 Preview 真机验)。测试 311→**316**(+5)。**注**：创业板指 csindex 也 404(深证指数)故只能代理；csindex 市盈率1=PE-TTM(对照 legulegu 沪深300 13.7 验)。
3. ✅ **已接入（2026-06-09，自动匹配·人工确认）** **同类 ETF 发现**：费率(`_etf_fee`/`fund_fee_em`)、跟踪误差(`_etf_tracking_dispersion`)、规模(westock etf)本已逐只可取；缺的「同类清单」——选源核查：xq 详情接口坏(`KeyError 'data'`)、名称模糊匹配**过匹配**(搜"沪深300"含红利/价值/增强=不同指数)、`fund_etf_spot_em` 无费率/规模列。**落地**：`_etf_spot_list`(全市场清单 ~30s/14 页 → 当日文件缓存 `cache/etf_spot_list.json`) + `_name_matches_peer`(用『<关键词>ETF』**精确子串**、且匹配位前字符非数字以排除"300红利低波"这类不同指数) + `_etf_peers`(按成交额取前 N、拉费率、**费率升序**、incumbent 永远纳入) + `GET /api/etf/peers`；前端 incumbent 表每行「找同类」按钮 → 费率/流动性对比，明确标"**自动匹配·需人工确认 / 研究发现非动作**"，满意的加 watchlist 走既有准入闭环。测试 +3，Preview 真机验。**实测**：510300 → 25 只沪深300同类全 0.20%(已商品化、无更便宜的)。
4. ~~**⚠️ QDII 溢价实盘提醒**~~ **✅ 已落地为「执行质量闸」**（见 §3）：本周决策里 QDII 加仓任务在**溢价≥1.5% 或不可/暂停申购**时自动降级为「暂缓」并给原因，`current_suggestions`/调仓建议同口径。**遗留开放项**：政策闸已就位但需**真 flag** 才生效——若要让它对"12月 QDII 限购"等传闻反应，须先**查证并写一条** `政策风险/利空/高`（affected_assets 含 513100/513500）的 flag（建议在 `/周报` 研究环节做）。
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

## 10. 变更历史（精简，最新在上）

- **可调再平衡频率 + 崩盘熔断 + 再平衡专题文档（2026-06-08，所有者需求："按现实波动改频率"）**：先做了三轮分析（全程22年表 / 子区间翻盘 / 行情脾气窗口，见 `REBALANCING.md` + 可复现脚本 `engine/analysis/rebalance_compare.py`），确认①"调vs不调"是最稳结论②各再平衡规则差距小③**"频率"才是真正杠杆**（同一条5/25 每日→每年，年化/回撤随频率变）④排名随行情翻盘（震荡市频繁赢、单边趋势偷懒赢）。据此加**可调再平衡频率**：`strategy.yaml › factors.rebalance.check_frequency`(weekly默认/biweekly/monthly/quarterly = 两次再平衡最短间隔 0/13/28/84 天) + `circuit_breaker_pp`(默认15，**任一品种偏离≥此值无视频率强制可再平衡**，防低频时错过崩盘)。signals.py：纯函数 `frequency_gate_state` + `latest_execution_date`（按 journal/executions 文件名取最近成交日）→ 低频档要求"距上次成交满 N 天"才再平衡，但熔断级偏离绕过；`action_discipline`/`params` 多报频率/熔断/距上次成交天数。validate_strategy 校验两个新键。app.py：`GET /api/rebalance-policy` + `POST /api/rebalance-frequency`（正则改 yaml、保留注释、不碰持仓/目标权重）。前端：决策页「再平衡设置」按钮 + 弹窗（展示5/25规则现状 + 频率下拉 + 熔断/响应权衡说明），`rebalanceRuleText` 周报里也显示当前频率。**默认 weekly = 行为与之前完全一致**（min_gap=0、当前组合无品种≥15pp 偏离，熔断休眠）。真机验收（preview）：弹窗渲染/下拉四档/保存往返(monthly↔weekly)/无 console 错误。测试 272→**279**（频率闸：每周不闸/每月窗口内外/无记录不闸/未知档回退/文件名解析/校验）。详见 [REBALANCING.md](REBALANCING.md)。strategy.yaml 仅加两个键、默认值；portfolio.yaml 未改。
- **长期战略可读性 + 配置理由 + 再平衡规则可见（2026-06-08，所有者三连反馈）**：① **构建结果去变量名**——`renderConstruct` 把残留的英文/代码字段全部中文化：`policy_allocation` 角色键走 `_ROLE_ZH`、国别 CN/US→`_COUNTRY_ZH`(中国/美国)、货币 CNY/USD→`_CCY_ZH`(人民币/美元)、`validation_status`→`_VSTAT_ZH`(passed→通过等)、质量状态 cached/missing/stale→`_QSTAT_ZH`(已缓存·新鲜/缺失/已过期)；真机实测构建视图 `leftoverUnderscores=[]`。② **「为什么这样配」结构化解释**——新增纯函数 `strategic.build_construct_rationale(policy, snap, name_of, quality, incumbent_weights)`：返回 `{objective, roles:[{role,tier,weight,range,purpose,band,members:[{code,name,weight,reason}]}], notes}`——objective 讲算法优先级(缩保守缺口→控压力预算→收益/集中度取舍、收益非承诺)；每角色给中文用途(`_ROLE_PURPOSE`/`_TIER_PURPOSE`)+「为什么这个比例」(区间内/顶上限/落下限)；每只 ETF 给入选理由(唯一品种/质量分领先/**限购冻结在现有权重**)；notes 收集触发的卫星/成长上限 + 限购冻结清单。`_run_construct` 挂 `snap["rationale"]`(construct/snapshot 共用)，前端 `renderRationale` 渲染为「为什么这样配」折叠块(默认展开)+ `styles.css` `.rationale/.ratRole/.ratMembers`。**真机实测**：6 角色全有用途/比例说明、标普500+纳指正确标「因当前限购，冻结在你现有权重、不再加仓」、notes 含卫星顶上限+限购冻结。③ **再平衡规则可见**——数据本就有(`s.params.rebalance_abs_pp/rebalance_rel` + `action_discipline.min/max`)但只在代码里；新增 `rebalanceRuleText(s)` 并在「交易纪律」前加「再平衡规则」段 + 无动作时的「再平衡」段都打出规则全文：「偏离目标 ≥5 个百分点 或 相对偏离 ≥25% 才触发（5/25 法则）；单笔 ≥¥500、单周 ≤¥50,000；行情缺失或过旧则本周不动手」。④ **去无用嵌套 DIV**——`长期配置是否合理` lens 早已单栏，删掉遗留的两栏外壳 `.strategyCompare/.strategyResult/.resultLabel`(HTML+死 CSS)，`#constructBox` 直接挂 `.strategyLens`。测试 270→**272**（rationale 角色/限购冻结/上限 notes + 无可行空态）。portfolio.yaml 未改。
- **我的组合：添加/提取现金（2026-06-08，所有者需求）**：新增 `POST /api/portfolio/cash`（action=add/withdraw + amount）——只调整 ETF 桶可投现金余额，校验金额>0、提取不超当前现金，更新 `portfolio.yaml` cash（保留持仓不动），并记一条 `journal/cashflows/<id>.json`（`reports.save_cash_flow`/`load_cash_flows`，新 `CASHFLOWS_DIR`）。**这只是现金收支、不是 ETF 成交、不进 TWR/MWR**（业绩只算已投入 ETF）。前端「我的组合」头部加「添加现金/提取现金」两个按钮 → `adjustCash(action)`（prompt 金额 + confirm + 调端点 + loadConfig 刷新组合视图/现金 chip）。测试 265→**270**（add/withdraw/超额拒绝/非法拒绝/journal 往返）。`GET /api/portfolio/cashflows` 备查。
- **战略流程易用性三连（2026-06-08，所有者反馈"切换就计算/对比给了建议然后呢/为什么这么难"）**：① **步骤点击只导航、不自动计算**——`goStrategyStep` 去掉 await loadIncumbents/loadConstruct/loadStrategicBacktest，改为只 openStrategyLens，真正计算由各步右上角按钮手动触发（`loadStrategicBacktest` 成功后才置 `validated`）。② **对比给出"各组合怎么配仓"+"然后呢"**——`simulate_strategic_comparison` 返回 `weights`(各被比组合实际配仓)+`names`；前端 renderStrategicBacktest 加「各组合分别怎么配仓？」明细 + 「这结果该怎么用」行动块（建议简化→去设置调低卫星/权益上限再重构、不是手改单只权重；保留复杂度→照第3步应用；省事→直接用第3步）+ 标注"权威构建为不含今日限购的长期形态、与第3步可应用版可能不同"。③ **一键生成长期战略**——`generateStrategy()`（quality-status 不新鲜则先审视刷新→loadConstruct→落结果）+ flowHead 加「🪄 一键生成长期战略」按钮，给"只想拿结果"的最短路径。**真机验收**（preview）：一键按钮在、点步骤2不触发 incumbents 计算、对比视图配仓明细+行动块齐全、无 console 错误。测试 265（simulate 测试加 weights/names 断言）。
- **折溢价移出长期准入（2026-06-08，所有者质疑"长期战略为何看当前折溢价"）**：所有者一针见血——折溢价是**执行时点**问题，不该决定 30 年的长期战略。把它从 §8.2 `hard_admission` 移出：`premium` 不再是关键检查、改 status=`info`（既不 fail 也不 gap、不计入 admitted）；高溢价/缺折溢价都不再阻断长期准入。**限购仍保留**为真实阻断（QDII 不可申购→冻结不加，所有者选的"折溢价移出、限购仍冻结"）。折溢价改由既有「执行质量闸」在下单/调仓时把关（§3 两层把关分工）。**根因连带解决**："非交易时段构建不了/全是考虑替换/卡在第2步"——因为折溢价是唯一非盘中不可靠的输入，移出后构建只依赖结构性数据（任何时间可得）。**实测**（周末）：审视 7 只→保留/评审、2 只真限购 QDII→暂不加仓；步骤条②✓新鲜、③解锁、construct **passed**（黄金 5%）；前端非交易时段横幅由红色警报改为平静提示"折溢价已不计入长期准入、长期战略可正常进行"。修订 §0B #2（溢价不再算准入阻断）。测试仍 **265**（test_premium_does_not_block_admission + 关键缺失改用规模示例）。
- **ETF 审视：非交易时段折溢价误判修复 + 处置区分数据缺失（2026-06-08，所有者反馈"全是考虑替换、卡在第2步"）**：根因有二。① **处置不分"真实阻断"与"数据缺失"**——`hard_admission` 缺关键数据→admitted False，`incumbent_disposition(admitted False)` 一律 `replace_candidate`（"考虑替换"），把"取不到数据"误报成"该换"。修：`incumbent_disposition` 加 `has_blockers`，**仅数据缺失（无真实 blocker）→ 新处置 `review_data`（待复核·先持有）**，真实阻断才 `replace_candidate`（UI 改名「暂不加仓」）；`assess_incumbents` 从 admission.blockers 算 has_blockers 并入行。② **非交易时段折溢价是陈旧数据→误报折价超限**（实测周末 沪深300 −3.25%、黄金 −3.44%，这类高流动 ETF 盘中绝无 3% 折价）。修：新增 `_is_trading_session()`（A股 周一~五 9:30–11:30/13:00–15:00）；`_etf_quality_for(realtime_reliable=)` 非盘中**置空折溢价**（按待复核 gap，不硬判），incumbents 端点传 `_is_trading_session()` 并回 `trading_session`。③ **流程不再假新鲜**：`/api/strategic/quality-status` 加 `data_ok`（≥60% 成员有真实准入判定才算可用）+ `trading_session`；`fresh = cached & 齐全 & data_ok` → 非交易时段步骤条第②步显示"数据取不到·交易时段重试"、第③步保持锁定（而非假"✓ 新鲜"诱导进死胡同）。前端：审视面板顶部加红色横幅（非交易时段/数据取不到时）、处置中文化（待复核·数据缺失/暂不加仓）+ 每条 hover「怎么做」、图例补两条、替代候选空态讲清"先持有等恢复"。**真机验收**（preview，今天周末）：7 只→待复核·数据缺失、2 只 QDII（标普/纳指 真限购）→暂不加仓、横幅+第②步"数据取不到·重试"、无 console 错误。测试 263→**265**（is_trading_session + data_gap→review_data + quality-status data_ok/数据不可用）。
- **ETF 审视面板可读性（2026-06-08，所有者反馈）**：① **去变量名**——`renderIncumbents` 把角色键/层级/产品状态译成中文（`_ROLE_ZH`/`_TIER_ZH`/`_PSTAT_ZH`，未知键回退原文不报错）：china_core_equity→A股核心权益、core_defensive→核心防御、scored/degraded/insufficient→数据完整/偏少/不足；角色合计行、组合角色列、候选行、构建视图的 policy_allocation 一并译。② **ETF 名称统一**——根因是 `strategy.yaml` universe 有 5 条缺 `name`（国债/沪深300/红利低波/中证500/黄金 ETF）→ 这些行回退成裸代码。补全 universe `name` 后所有视图统一「中文简称+股票号」。③ **建议给解决方案**——"候选替换"改名「考虑替换」并加 `_DISP_ACTION` 每条"怎么做"（保留=无需动作/减配=下次调仓减回/评审·二选一=保留更优一只/考虑替换=准入未过暂不加仓、有合格候选才换否则持有等恢复）；每行建议加 hover tooltip；替代候选区为空时解释清楚"没候选就先持有不动、等准入恢复，别带病加仓"。**真机验收**（preview）：角色/层级/状态全中文、名称统一、建议+解决方案清晰、无 console 错误。无测试影响（263 仍绿）。
- **长期战略·线性流程步骤条（2026-06-08，交互改造）**：所有者反馈"第一次建仓前想重跑战略评估，但流程难理解"。根因：战略页是"判断透镜"模型，而任务本质是有先后的准备流程，且批1的 fail-closed 引入了隐藏依赖（构建依赖先跑 ETF 审视刷新质量缓存，界面没提示）。改造（保留透镜）：战略页顶部加 5 步引导步骤条 ① 确认设置 ② 刷新ETF质量准入 ③ 构建模型组合 ④ 验证复杂度(可选) ⑤ 应用为目标权重，每步显示状态(✅/待办/🔒)+「下一步」提示，**③ 在 ② 未刷新时锁定**（把隐藏依赖摆明）。新增轻量探针 `GET /api/strategic/quality-status`（缓存新鲜度/成员覆盖，驱动②状态、不跑慢审视）；`app.js` 加 `STRATEGY_FLOW`/`loadStrategyFlow`/`renderStrategyFlow`/`goStrategyStep`，并挂到 activateTab(review)/renderConstruct/loadIncumbents/saveConfig；`index.html`+`styles.css` 加步骤条。**真机验收**（preview_start dashboard）：步骤条渲染正确、② 待刷新/③ 锁定、提示文案正确、无 console 错误；修了一处 CSS 源顺序 bug（响应式 `flex` 覆盖被基础规则盖过→窄屏每步 160px 高）。测试 262→**263**（quality-status 端点）。
- **批3收尾·数据驱动收益区间（2026-06-08，§0B 🟡 成长桶保守区间）**：所有者反馈"无力手定保守年化、要靠谱算法"——改为数据驱动。新增 `backtest.compute_return_intervals`（+ `backtest.py --return-intervals` 可复算）：折扣按各 sleeve **历史年化波动率**缩放（系数标定为让 A 股核心得 default 3%），且**成长桶(china_growth/global_growth)保守值封顶在核心权益保守值**——因实测纳指波动(21.5%)其实**低于**沪深300(24.6%)，"成长桶保守≥核心"的污染源自乐观的中枢而非波动，故纯波动缩放不够、须加"最坏情形不假设乐观成长跑赢普通股票"的封顶。算出并登记到 `strategy.yaml`：global_equity 5.71%/10.29%、global_growth 4%/12.61%、china_growth 4%/12.86%（其余 sleeve 维持对称默认）。**实测**：validate 通过、纳指/创业板保守 0.07→**0.04**(≤核心中枢、污染消除)、构建仍 passed(保守预期 4.2%→3.75%、更偏向防御/黄金)。测试 261→**262**(波动缩放+成长封顶，mock 取数纯逻辑)。portfolio.yaml 未改。
- **批 4 重建对比回测证据（2026-06-08，§0B 阻断项 #5）**：让 §12.3/§16.3 对比回测能作诚实上线证据。① **统一剔除未覆盖品种**——无 20 年长代理的成长卫星(159915/588000)现从所有被比组合统一剔除并各自归一(`covered`+`restrict()`)，不再静默按比例摊回(旧实现把权威构建悄悄抬向美股、与"仅核心"不可比)；剔除权重显式披露 `excluded_weight`。② **债券票息 Calmar 敏感性**——`build_full_panel(bond_carry=)` + 零息重跑，每行附 `calmar_zero_coupon`(检验结论是否被 +3% 零波动票息驱动)。③ **去退化重复基准**——按代理目标签名去重(gold=0 时"无黄金"≡"权威构建")。CLI/JSON/前端(renderStrategicBacktest 加 Calmar零息列 + 三项披露)同步。**实测**(21 年全收益面板)：剔除 权威构建 13%/当前 10% china_growth；"更低权益"Calmar 0.29(零息 0.27)两列皆最高→倾向简化但**不被票息驱动**；china_growth 增量价值不在覆盖内(诚实样本局限)。测试 259→**261**(bond_carry 零息更低 + simulate 三项披露端到端)。**至此 §0B 5 阻断项 + 4 护栏全部落地** → 可进少额真金验证。portfolio.yaml 未改。
- **批 3 建模判断（2026-06-08，§0B 批 3）**：所有者拍板**黄金 5% / 防御(红利低波)5% 下限**——修了引擎把二者压到 0% 的建模 bug（审计点名的"当前组合真正弱点"）。① **下限**：`strategy.yaml` gold/defensive_equity range 下限 0.00→0.05，policy_version 1→2。② **解耦压力预算**：新增 `strategic_policy.construct_stress_budget`（null→默认=max_acceptable_drawdown），`_run_construct` 用它作 construct 硬约束、与展示回撤解耦；snap 多报 `construct_stress_budget`/`display_max_drawdown`，前端显示"压力 X%（预算 Y%）"。③ **暴露建模**：每个 universe instrument 加显式 `exposure_id`，construct/backtest 的 `exposure_of` 优先用它（永不退回 proxy_index/code——修了红利低波因 proxy=sh000300 被误当沪深300 的隐患）；single_satellite_max 已锚定每只 ETF 全组合最终权重。④ **假设边界校验**：`validate_strategy` 校验 `return_haircut∈[0,0.15]` + per-sleeve 区间边界 + 断言 `conservative≤central≤optimistic`。**实测**（真实配置 + 新鲜质量缓存、写盘 mock）：构建 黄金 5%/防御 5%（此前 0%）、validate 通过、预算 null→30%、9 只暴露仍各自独立（exposure_id 不误合并）。测试 253→**259**（floor/single_satellite binding/stress_budget decoupled/haircut+ordering 校验×3）。**未尽**：成长/QDII 桶显式保守区间(需假设标定)、协方差接入 construct 接受判定（见 §0B 🟡）。portfolio.yaml 未改（floors 只在用户显式重建+应用时生效）。
- **批 2 保存设置去自动应用（2026-06-08，§0B 阻断项 #3）**：`save_config` 不再在保存路径跑 construct 或写 target_weight——**只持久化 profile/risk/portfolio**（写出的权重 = 用户提交值/当前权重），返回 `manual_apply_required`。重配走显式三步：`/api/strategic/construct`（看 diff）→ `/api/strategic/apply`（批 1 的指纹完整性 + ≥15pp 大跳变二次确认）。前端 saveConfig 改为平静提示"已保存、权重不变，去「战略与复盘 → 长期配置是否合理」重新构建并确认应用"。**实测**：真实持仓提交保存 → `_run_construct`/`_apply_constructed_allocation` 均未调用、写出权重与提交逐一相同、portfolio.yaml 未变。测试 255→**253**（删 4 个旧自动应用用例：only_applies_passed/preserves_when_not_feasible/holds_on_quality_block/holds_on_large_move——它们验证的保存路径自动应用已移除；加 2 个：never_auto_applies_target_weights[断言 construct/apply 均不调用]、writes_submitted_target_weights_unchanged）。质量闸/大跳变护栏仍在 apply 端点（批 1）完整保留。
- **批 1 安全闸落地（2026-06-08，详见 §0B）**：堵住战略引擎"真金可达"的三条阻断项。**#1 质量 fail-open（CRITICAL）**——`construct_strategic_portfolio` 加 `require_quality`：无质量/准入记录的 code 按未准入 **fail-closed**（incumbent 封顶当前权重 freeze、非持仓剔除）；`_run_construct` 当质量缓存 `missing/stale` 或任一角色成员无记录 → 置 `quality_gate.blocked` 并把 `validation_status` 降为 `blocked_quality_data`，apply/save_config 据此拒绝。**#2 失败准入 ETF 拿权重**——经 #1 补全闭环（此前 `quality is None` 跳过整段使 freeze 失效，现真正生效）。**#4 apply 完整性 + 大跳变护栏**——`/api/strategic/apply` 改为：客户端回显评审过的 `input_fingerprint`、服务端重算不符回 **409 stale**（缺指纹 400）；单产品 target_weight 跳变 **≥15pp**(`LARGE_MOVE_THRESHOLD`)回 **409 needs_confirmation** + diff，须 `confirm_large_moves`；apply/construct 披露 `product_quality_status`/`quality_gate`。**save_config**：质量被闸或大跳变时返回 `auto_apply_held`、不再静默改权重（彻底去自动应用留批 2）。前端 `renderConstruct` 披露质量+禁用按钮、`applyStrategicConstruct` 回显指纹并处理两种 409、`saveConfig` 显示 held 原因。**实测**（Flask test_client + 真实配置）：缓存缺失时 construct=`no_feasible`/blocked、apply 全被拒；注入新鲜全准入缓存则正常构建、+15pp(513500 20→35%)正确触发二次确认。测试 245→**255**（apply 指纹/大跳变/确认/缺指纹、save_config 两种 held、construct require_quality fail-closed×2、_run_construct 质量闸、_large_target_moves 边界）。**无代码动 portfolio.yaml**（冒烟误写已 `git checkout` 还原）。**下一步=批 2**（saveConfig 彻底不自动应用）。
- **战略引擎对抗式审计 + 治理决策（2026-06-08，详见 §0B）**：所有者决定取消两季度影子、此引擎作为唯一长期战略引擎、少额真金验证再加仓。6 维多 agent 对抗式审计（47 agent，20 条发现全核验为真）裁决 **go-with-fixes**：骨架（角色区间/上限/压力预算/确定性投影）稳，但有**真金可达**的阻断项——① 质量缓存缺失/过期→硬准入 fail-open 仍标 passed（CRITICAL）；② 失败准入 ETF（513500/513100/588000）仍获权重、当前已应用组合 30% 在其中；③ 保存设置即静默重写全部目标权重；④ apply 忽略请求体、无 diff 完整性；⑤ 对比回测结构性失真（删成长卫星/债券假票息/退化重复基准）不可作上线依据。修复编排分 4 批 + 4 条少额真金护栏。**关键实现要点**：fail-closed 须做成"封顶在当前权重/freeze"而非"整只剔除"（美股角色仅 513500 一只，剔除会 no_feasible）；现状作"持有"可过、作"加仓新目标"被拦。**纠正两处先前口头结论**：黄金清零主导是"保守缺口"键非 return_first；balanced 切换是空操作。**无代码改动**（本轮仅审计 + 写 §0B 待办，换机后实修）。
- **周报按自然日归档（同日刷新覆盖，不再堆秒级目录）**：`reports._report_day_id()`（`YYYY-MM-DD`）取代 `archive_report` 里 `_now_id()`（到秒）作 report_id——**同一天反复点「刷新本周判断」覆盖同一份周报**，跨日才新建并 supersede 上一份。**执行记录仍用 `_now_id()` 到秒**（一天可多笔成交、绝不互相覆盖）。同日复用同一 cycle_id → `journal/decisions/<id>.json` 的 skip/execute 标记**得以保留**（旧的到秒方案重刷会丢）；`_supersede_active_cycle` 因 `active.id==new_id` 同日不自我 supersede。复盘/业绩/状态机不受影响（早已按 `generated_for` 自然日只取最新一份；NAV 也早已同日覆盖）。所有 id 解析走 `created_at`/`[:10]`/`[:7]`，对自然日 id 一样成立。测试 242→**245**（TestReportArchival：自然日 id 不含秒/同日覆盖单份/跨日 supersede 上一份）。
- **Track C Phase B Step 1（ETF 费率 + §8.2 硬准入接入）**：新建 `engine/strategic.py`（战略层纯函数 v1 单模块，按 §4 责任组织）——`parse_etf_fee()`（解析 `ak.fund_fee_em` 无表头输出→管理费/托管费/综合费率，按标签就近定位不硬编码列下标）+ `hard_admission()`（§8.2 规模/容量/流动性/折溢价/申购/上市年限/费率七检查，关键字段缺失→降资格待复核不 fail-open、fee/年限软 gap 不阻断）+ `ADMISSION_DEFAULTS`（流动性5%/容量1%/规模2亿/上市1年/折溢价±3%）。`app.py`：`_etf_fee(code)` 网络包装（`ak.fund_fee_em`，进程内周级缓存，失败回退 None 不阻塞）+ 接进 `_etf_quality_for`/`etf_quality` 端点（按 planned_etf_capital×目标权重算容量、max_weekly 算单笔流动性）。`app.js`：质量卡加「综合费率」格 + 「§8 准入 ✓/✗ + 拦截/缺数据」条。**live 实测**费率链路：510300=0.20%、513500=0.80%、518880=0.60%（与侦察一致）。测试 198→**213**（TestEtfFeeParse 5 + TestHardAdmission 10）。
- **Track C Phase B Step 2（§8.3 产品分 + 三层目录骨架）**：`strategic.py` 加 `product_score()`（六子分：跟踪0.25/成本0.20/流动性0.20/规模0.15/折溢价0.10/运营0.10，**缺失=None 不中性填补**、总分仅在可得子分按可得权重归一、显式给 coverage/confidence/flags、关键子分缺失→降资格 status=degraded/insufficient、不含收益项不被近期涨幅抬分）+ 六个子分纯函数 + `build_catalog()`（strategic_policy.roles + universe + 当前权重 → 角色→产品三层骨架 + 区间状态 within/below/above）。接进 `_etf_quality_for`/`etf_quality`（共享 cand，tracking_dispersion 暂 None 待 Step 3）；`app.js` 质量卡加「§8.3 产品分 + 覆盖率/置信度/flags」条。**live 实测**：build_catalog 在真实配置上正确判 growth_satellite 0.25→above[0,0.20]、其余 within；纳指 product_score=0.77/覆盖75%(跟踪缺)/degraded。测试 213→**223**（TestProductScore 7 + TestBuildCatalog 3）。
- **Track C Phase B Step 3（跟踪离散度 + incumbent 审视表）**：`strategic.py` 加 `tracking_dispersion()`（§8.4 best-effort：年化 std(ETF累计净值收益 − 代理指数收益)，<20点→None；价格指数→只横向排序非绝对TE）、`weighted_jaccard()`（§7.3 重合，任一空→None 不默认低重合、QDII↔A股 自然为0）、`incumbent_disposition()`（keep/trim/review/replace_candidate）、`assess_incumbents()`（build_catalog + 逐只准入/产品分 + 单卫星上限 → 处置表）。`app.py`：`_etf_tracking_dispersion(code,proxy_index)`（fund_etf_fund_info_em 累计净值 vs stock_zh_index_daily，日级缓存，QDII/黄金 proxy=null→None）接进 product_score 的 tracking 子分（仅战略审视入口取、普通质量页不拖慢）；新端点 `GET /api/strategic/incumbents?te=`（逐只跑准入+产品分→assess_incumbents 表 + catalog + policy_version）。`index.html`+`app.js`：策略审视加「ETF incumbent 审视」按钮 + 表（角色/层/权重/§18上限/§8准入/§8.3产品分/处置 + 角色区间条）。**live 实测**端点：真实配置上 6 keep（双核心/红利/债/金/标普）+ 3 trim（成长三只 above），纳指 single_cap_exceeded→trim——与就绪报告处置逐一吻合、但现在是**实时计算**非 agent 推断。测试 223→**233**（TestTrackingAndOverlap 7 + TestIncumbentAssess 3）。
- **Track C Phase B Step 4（持仓重合 + §11 二选一）——Phase B 收官**：`strategic.py` 加 `overlap_matrix()`（两两加权 Jaccard）+ 扩 `assess_incumbents(asset_of, holdings_by_code)` 双路冗余：① **结构精简**（同卫星角色+同 asset 多成员=二选一候选，free/无网络，**正是这条捕到创业板/科创50**——它们持仓 Jaccard 其实低、redundancy 是 role/factor 级）；② **持仓重合**（同角色内 Jaccard≥0.30）。`app.py`：`_etf_holdings(code)`（`fund_portfolio_hold_em` 季频取最新一期→{股票:占比}，债/金→None、QDII 返美股代码，月级缓存）；incumbent 端点加 `?overlap=1`（慢、默认跳过）+ 始终传 asset_of（二选一 free）。`app.js`：表加「二选一」「高重合」标签 + 补算持仓重合按钮。**live 实测**真实配置：创业板/科创50→**review·二选一**、纳指→trim·单只超10%、其余 keep——与就绪报告处置**逐字吻合**。测试 233→**235**（overlap_matrix + consolidation + holdings_redundant）。**Phase B 收官**：§8 准入✓ 费率✓ §8.3 产品分✓ 三层目录✓ TE✓ 持仓重合✓ incumbent 审视表✓。
- **Track C Phase C Step 1（权威战略构建引擎 §10，shadow）**：`strategic.py` 加 `construct_strategic_portfolio()`——§10 唯一权威顺序的 v1：`_enumerate_role_allocations`（角色区间网格、合计=1、递归剪枝）生成候选 → §18 上限(satellite/single_satellite/growth/single_country/single_currency/non_satellite)+ 压力预算**拒绝不可行** → 词典序选择（gap→stress→[return_first/balanced/defensive_first 切换 收益 vs 集中度]→简洁）→ 等权分配到产品 → `_deterministic_projection` 投影 → 最终验证；无可行解→`no_feasible_portfolio`（绝不返回超预算建议）。`_deterministic_projection` **下沉为 strategic.py 权威实现**、app.py 别名复用（去重）。新端点 `GET /api/strategic/construct`（shadow：snapshot + 与当前权重 comparison，**不替代** `_suggest_target_weights`，§16.4 需两季度影子才迁移）+ `index.html`/`app.js` 影子对比视图。**live 实测**真实配置：status=passed、2109 候选/358 可行，自动得 卫星25→**20%**、CN 权益51→**43%**（国别上限 binding）、成长**20%**、压力 16.8%<20%、return_first 把标普 21→35%。⚠️**观察**：return_first 在压力预算下把低收益 defensive(红利低波)压到 0%——符合进取定位，但若要保留防御分散可切 `selection_priority: balanced` 或给 defensive 加下限（Phase C Step 2/所有者判断）。v1 单点收益+单情景压力；收益区间/收缩协方差/多情景压力为 **Phase C Step 2**。测试 235→**240**（TestConstructStrategic：可行+上限全守/确定性/无可行/选择档/投影别名同源）。
- **Track C Phase C Step 2（收益区间 §9.1 + 多情景压力 §9.3）**：`signals.load_assumptions` 加 `returns_conservative/optimistic`（central ± `return_haircut`，缺省 0.03；sleeve 可显式 `return_conservative/optimistic` 覆盖）；新增 `load_stress_scenarios` + `DEFAULT_STRESS_SCENARIOS`（七情景：全球股灾/中国股灾/美科技重估/利率急升/通胀/人民币升值/QDII溢价，每情景完整资产冲击向量，可在 strategy.yaml `stress_scenarios:` 覆盖定档）。`construct_strategic_portfolio` 加 `returns_conservative`（词典序"目标缺口"改**保守口径**，§10.3）+ `scenarios`（压力取**最坏情景**损失，§9.3）；metrics 多报 `expected_etf_return_conservative`/`target_gap_conservative`/`worst_scenario`。construct 端点传区间+七情景；前端显示收益区间 + 最坏情景。strategy.yaml `assumptions.defaults` 加 `return_haircut: 0.03`。**live 实测**：真实配置预期 7.2%(央)/**4.2%(保守)**、缺口 0.8%/**3.8%(保守)**、最坏情景=**全球权益危机**、压力 16.8%→**18.6%**(多情景比单情景更狠、仍<20%预算)。配置不变(上限仍 binding)但风险/收益口径变诚实。测试 240→**246**（TestConstructStrategic +2 多情景/保守缺口、TestReturnIntervalsAndScenarios 4）。
- **Track C Phase D Step 1（战略组合对比回测 §12.3/§16.3）**：`strategic.derive_comparison_portfolios()`（当前/权威构建/仅核心/无卫星/无黄金/更低权益，各自归一，纯函数）；`backtest.simulate_strategic_comparison()` 复用 `build_full_panel`(全收益+黄金长面板) + `_run_with_nav`(持仓漂移+成本) 把每个组合跑出 CAGR/波动/回撤/Calmar/换手；`backtest.py --strategic` CLI（自建面板、无需 ETF 段）。**live 实测**（21 年全收益代理段 2005~2026，剔除创业板/科创50/QDII 无长序列）：当前 +12.6%/−50.3%/Calmar0.25 vs **权威构建 +12.2%/−46.8%/0.26**（略降收益换 −3.5pp 回撤+降波动）vs 更低权益 +12.0%/**−43.9%/0.27**。**§16.3 诚实结论**：构建组合在风险调整后**优于当前**（降回撤/波动），但「更低权益」更简单、Calmar 反而更高——保留卫星的复杂度**没有清晰的风险调整后增量**（代理段已剔成长卫星，故部分被muted）。这正是 §12.3 用证据挡住"为复杂而复杂"。测试 246→**247**（TestConstructStrategic.test_derive_comparison_portfolios）。
- **Track C Phase C Step 3（收缩协方差 §9.2 + 风险贡献 §12.1）**：`strategic.py` 加 `shrinkage_covariance`（Ledoit-Wolf 式向"恒定相关"目标收缩、纯 python 小矩阵、周频、<20 期→None 退化）+ `portfolio_volatility` + `risk_contributions`（风险贡献分解 + 有效风险来源数=风险贡献 HHI 倒数，§12.1）。对比回测 `simulate_strategic_comparison` 计周频收益→cov→每组合算风险贡献，CLI 加「有效风险源」列 + 风险模型脚注。**live 实测**（21年/周频1117期/平均相关0.15/收缩0.3）：有效风险源 当前3.6/权威构建3.3/仅核心2.9/更低权益3.4——当前最分散、仅核心最集中。测试 247→**251**（TestCovarianceRisk 4）。
- **Track C Phase D Step 2（稳健性 + 治理快照 + 端点/前端）——Track C 工程收官**：① **稳健性(§12.4)**——对比回测加滚动子期 Calmar（构建优势是否跨期一致）+ 假设 ±20% 收益扰动重构（构建是否稳定守住 §18 上限）；live：构建在 ±20% 扰动下恒 passed/卫星20%/压力19%（caps 主导→对假设误差稳定）。② **治理快照(§14)**——`reports.save_strategic_review`/`load_strategic_reviews` → `journal/strategic_reviews/<id>.json`；`POST /api/strategic/review/snapshot`（跑构建 + `input_fingerprint` sha256 + policy_version + 当前/构建权重 + user_decision → 落盘，**两季度影子记录机制**）+ `GET /api/strategic/reviews`；construct 端点也回 `input_fingerprint`。③ **端点/前端**——`POST /api/strategic/backtest`（子进程 `backtest.py --strategic --json`）+ `backtest.py --strategic --json` 分支；前端策略审视加「对比回测」按钮 + 表（构建vs当前vs简化 + 有效风险源 + 滚动/扰动稳健性）+ 「存档审视快照」按钮。`_run_construct` 抽出 construct/snapshot 共用避免漂移。测试 251→**254**（TestStrategicReviewSnapshot 2 + 扰动稳定性 1）。**Track C 工程主体收官**：A 正确性 ✅ §18 政策 ✅ B 选择 ✅ C 构建(§9.1区间/§9.2协方差/§9.3多情景/§10权威/§10.4投影) ✅ D 验证(§12.3对比/§12.4稳健/§14治理) ✅。**剩纯运维**：§16.4 两季度影子(时间闸)→通过后翻迁移开关替代 `_suggest_target_weights`。
- **Track C Phase B 就绪侦察（2026-06-07，多 agent 并行）**：4 路数据源 live 探测 + 9 只 incumbent 审视 → `STRATEGIC_PHASE_B_READINESS.md`。**数据结论**：§8 硬准入(折溢价/规模/成交额/申购)复用现成 `_quality_metrics` 直接落地；管理费 `ak.fund_fee_em` 需新写 `_etf_fee`；跟踪误差 best-effort(无全收益 A 股指数→降级「相对跟踪离散度」、债/金 N/A)；持仓重合 partial(季频、债/金无成分、QDII↔A股 Jaccard=0)。**处置**：6 keep(国债/沪深300/红利低波/中证500/黄金/标普500)、纳指 trim(13→≤10)、创业板/科创50 §11 二选一。**新发现第 4 条 binding 约束**：中国 A 股权益=51%>45%(single_country_equity_max，此前聚合漏算)——所有者裁定**保留 45%**(不调高)，引擎将同时削成长(≤20%)+削中国 A 股(→≤45%)。无代码改动（纯侦察+决策记录）。
- **Track C §18 投资政策书钉死（2026-06-07，policy_version=1）**：动 Phase B/C 代码前定下 10 项决策，写入 `strategy.yaml: strategic_policy`（惰性记录、Phase B 起读）+ `STRATEGIC_ALLOCATION_DESIGN.md «附录 A»`。所有者拍板 3 项：①收益口径=**ETF 桶**（全组合预期同屏显示）；②场外 70万=**真稳健**(2%/0冲击垫)；④组合级上限=**设计默认**（卫星≤20%/单卫星≤10%/成长≤20%/单国45%/单货币55%）。**关键后果**：当前成长卫星 25%、纳指 13% 超标 → 新引擎将建议成长→≤20%、纳指→≤10%（所有者**主动选纪律约束**、非给现有激进仓背书）。其余 7 项工程默认（数据源：折溢价/规模/成交额/申购已有、费率部分、跟踪误差/重合尽力取缺失即降资格；准入§8.2/替换§8.7；Ledoit-Wolf 协方差与 Track B Phase D 合一；§9.3 七情景压力；词典序 return_first；约束投影复用 Phase A）。无代码改动、无新测试（纯决策记录）。
- **Track C Phase A（长期战略建议器正确性修复｜原 WS9 升级，权威规格 `STRATEGIC_ALLOCATION_DESIGN.md`）**：把战略建议器从"规则模板"向"可信引擎"收的第一刀，**纯正确性、不扩 universe**。① **A1 合法零值保留**——新增 `signals.resolve_policy_number()`（§5.2：缺失→默认+`defaulted`、合法 0 保留、非法→`invalid` 不静默修正），替掉 `app._suggest_target_weights` 与 `signals.build_signal` 里 `float(x or 默认)` 吞掉合法 0% 目标/回撤的 bug（`target_annual_return`/`max_acceptable_drawdown`/`horizon_years`）；`risk_budget`/建议载荷带 `policy_input_status`。② **A2 确定性投影 + 任意修改后重算**——`_deterministic_projection()`（最大余数法、合计恰 1、各项≥0、**不再"塞给最大项"**）取代旧取整；`_strategic_metrics()`/`_validate_strategic()` 纯函数；`_apply_policy_gate(…, strat=)` 在 pro-rata 重分配后**重算 stress/contribs/whole/expected 并重新验证**（消除"政策闸后显示改前过期风险数字"），置 `metrics_recomputed_after_gate`。③ **A3 validation_status 门控**——建议输出带 `validation_status`(passed/violated)+`constraint_diagnostics`；前端 `validation≠passed` 时**禁用「应用建议权重」并拦截**（即便绕过按钮）+ 横幅列冲突。④ **A4 静态长段回测复用全收益面板**——`backtest.py` ② 段优先 `build_full_panel`（全收益含分红+黄金分散），仅在不可得时回退价格指数，并显著披露 `basis`/`dropped`（§12.2 禁止静默剔除冒充真实组合）。注：政策闸 pro-rata 重分配本身（§10.2 反模式）留 Phase C 换确定性投影；本阶段先确保重算+门控不让过期/越界蒙混。测试 186→**198**（TestStrategicPhaseA：零值保留/投影守恒+不塞最大项/确定性/验证三态/0%回撤如实 violated/政策闸重算）。**待续**：Track C Phase B（三层目录+ETF 准入评分）、Phase C（收益区间+收缩协方差+权威构建）、Phase D（持仓漂移战略回测+治理快照+两季度影子）。
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
