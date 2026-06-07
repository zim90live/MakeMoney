# Track C Phase B 就绪报告（2026-06-07）

> 来源：多 agent 并行侦察（4 路数据源 live 探测 + 9 只 incumbent 审视）→ 合成。
> 权威依据：`STRATEGIC_ALLOCATION_DESIGN.md`（§8 准入评分 / §11 incumbent / 附录A §18 决策）+ `strategy.yaml: strategic_policy`（policy_version=1）。
> 诚实声明：区分「live 已验证」与「读码推断」；`obtainable≠yes` / `live_verified=false` 均显式标注，不把读码冒充可得。

## 1. 数据可得性总表

| 数据需求 | obtainable | 9 只覆盖 | 接入方式 | Phase B 定位 |
|---|---|---|---|---|
| 折溢价/规模/1日成交额 | **yes** | akshare 8/9（缺 511010）、westock 9/9 | 复用 `app.py:_quality_metrics`（零新写） | **直接落地 §8 硬准入** |
| 申购状态 | **yes（单源脆弱）** | westock 单源 9/9、akshare 无兜底 | `_quality_metrics.extra.purchase_status` | 落地但缺失须 **fail-closed** |
| 20日均成交额 | **yes** | 9/9（`_akshare_avg_turnover_20d` + westock 双兜底） | 现成 | **直接落地（流动性5%闸）** |
| 管理费/托管费 | **yes** | 9/9（费率是档案属性、不依赖成分） | **新写** `_etf_fee(code)` 包 `ak.fund_fee_em(code,'运作费用')` | **需新接入（总成本闸）** |
| 跟踪误差 TE | **best_effort** | ETF 净值腿 9/9；指数腿仅价格指数 | `fund_etf_fund_info_em` 累计净值 vs `stock_zh_index_daily` | **降级为「相对跟踪离散度」**；债/金 N/A |
| 持仓重合（Jaccard/行业） | **partial** | 股票级 7/9（缺 511010债/518880金） | `fund_portfolio_hold_em` + `fund_portfolio_industry_allocation_em`（季频） | **partial，缺失即降资格** |
| 全收益指数源（TE 基准） | **no（live 确认）** | — | akshare 无全收益 A 股指数（h00300 实测 0 行） | **不可得**——TE 绝对值不可信的根因 |

**关键限制**：① TE 因无全收益指数，分红缺口污染绝对值，只能产出横向排序信号；② 持仓重合季频滞后~2 个月、债/金无成分、QDII↔A股 Jaccard 天然为 0、跨 A股/QDII 行业需 GICS↔国民经济分类映射；③ 申购状态 + 511010 行情依赖 westock 单源，缺失 fail-closed；④ `ak.fund_fee_em` 无表头需位置解析（col1管理费/col3托管费、正则去 `%（每年）`），Windows GBK 终端 print 中文乱码（写 UTF-8）。

## 2. 9 只 incumbent 处置

| code | 名称 | 角色/tier | 权重 | cap | 处置 | 一句理由 |
|---|---|---|---|---|---|---|
| 511010 | 国债ETF | government_bond/core_defensive | 7% | within | **keep** | 唯一久期/流动性压舱物、零重合；削卫星超额时承接（权重或略升）。§11 久期审视：短久期(~5y)凸性弱→role_fit=adequate，单条 review 复核 |
| 510300 | 沪深300ETF | china_core_equity/core | 15% | within | **keep** | 诚实中国大盘核心、撑 70% 非卫星地板 |
| 512890 | 红利低波ETF | defensive_equity/diversifier | 9% | within | **keep** | 真实防御风格增量（非同因子冗余）、命名无高估 |
| 510500 | 中证500ETF | china_core_equity/core | 15% | within | **keep** | 中盘核心、与 510300 成分互斥（真实市值段分散） |
| 518880 | 黄金ETF | gold/diversifier | 8% | within | **keep** | 池内冗余最低、无替代物，唯一通胀/危机独立分散——**绝不为腾权重而砍** |
| 513500 | 标普500ETF | us_core_equity/core | 21% | within | **keep** | 唯一美国核心锚、无备用品；是「被重合」方而非冗余源 |
| **513100** | **纳指ETF** | growth_satellite/satellite | **13%** | **🔴 exceeds** | **🔴 trim** | 同时顶满 3 条上限；至少削至 ≤10% |
| **159915** | **创业板ETF** | growth_satellite/satellite | **6%** | **🔴 exceeds** | **🔴 trim+review** | 贡献成长桶 6pp；与 588000 高冗余，须 §7.3 二选一对决 |
| **588000** | **科创50ETF** | growth_satellite/satellite | **6%** | **🔴 exceeds** | **🔴 review** | role_fit=redundant；对决落败则升 replace_candidate |

## 3. 锁定 §18 上限下的 4 条 binding 约束

成长卫星篮子 `513100+159915+588000 = 25%` 同时击穿三条，**外加一条 A 股 sleeve 集中度**（fan-out 审视新发现，我此前聚合事实漏掉）：

| 上限 | 阈值 | 现状 | 超额 | 触发者 |
|---|---|---|---|---|
| single_satellite_max | ≤10% | 纳指 13% | **+3pp** | 仅 513100 |
| satellite_max | ≤20% | 合计 25% | **+5pp** | 成长三只 |
| growth_factor_max | ≤20% | 合计 25% | **+5pp** | 成长三只 |
| **single_country_equity_max** | **≤45%** | **中国 A 股 51%**(510300+510500+512890+159915+588000) | **+6pp** | **整个 A 股 sleeve** |

**整改路径（Phase C construct 求解，词典序 return_first）**：① 纳指 13%→≤10%；② 成长合计 25%→≤20%（如 10/5/5）；③ 159915 vs 588000 §8.3 对决，胜者随篮子压配、负者升 replace_candidate；④ 中国 A 股 51%→≤45% 需再降~6pp（与成长削减部分重叠——创业板/科创50 属中国成长；但仅成长削减不足以达 45%，可能触及中国核心/防御微调）；⑤ 腾出权重回流非卫星（国债上限40%有空间），回填美核心 513500 受国别预算仅余~11pp/货币~21pp 硬天花板。

> 其余 6 只对超标**零贡献**，整改绝不误伤——尤其黄金（真实分散）、513500（唯一美核心锚）。

## 4. 冗余与命名（§7.3 / §11）

- **标普500 vs 纳指**：池内最强重合（纳指100 ⊂ 标普大盘科技段），但角色互补（核心锚 + 成长卫星）非冗余；削重合**削纳指不动标普**。⚠️ 纳指**命名高估**——§设计明令「不得表述为新增全球分散」，实为单一国别/货币/科技集中的成长卫星。
- **创业板 vs 科创50**：§11 明示「评估是否只保留一只」；同 china_growth、同成长因子、同压力情景，588000 已判 redundant。最大冗余配对，二选一焦点。
- **重合 confidence 总体 low**：穿透数据评分前未取，全按暴露定义保守判，挂 `overlap_confidence=low`；取股票级成分后升 medium/high。

## 5. Phase B 落地序列

**5.1 先做（零阻塞，可直接接）**
- 复用 `_quality_metrics`/`_classify_premium`(L277)/`_classify_scale`(L292)/`_purchase_status_note`(L491)/`_akshare_avg_turnover_20d`(L619) → §8 硬准入四项 + 流动性5%闸（`planned_single_trade ≤ 5%×avg_daily_turnover`）。取数先 `prefetch_westock+_prefetch_westock_etf` 预热，再按 `_westock_covers_all` 决定是否回退慢的 `_etf_spot_snapshot`(~44s)。
- **新写 `_etf_fee(code)`**：`ak.fund_fee_em` 位置解析 + 月级缓存 + try/except 回退 None（缺失中性、不阻塞）。

**5.2 三层目录骨架**（沿用 `cache_dir` 文件缓存）
```
data_sources/  费率(月级) · 行情四项(进程内120s) · TE腿(月级落盘增量) · 重合(按季key落盘)
scoring/       §8.3 product_score 六子分(跟踪0.25/成本0.20/流动性0.20/规模0.15/折溢价0.10/运营0.10)
               每子分记 source/时点/可用状态/置信度；缺失即降资格不中性填补
construct/     §18 六上限+角色range，直接读 strategy.yaml:strategic_policy，词典序硬约束
```

**5.3 被数据闸卡住（降级，不阻塞）**
- TE → 「相对跟踪离散度」横向排序；债/金 N/A；ETF 腿用累计净值；分页慢必须落盘增量缓存。
- 重合 → 债/金 overlap=0+missing；QDII↔A股 Jaccard=0（非bug）；跨口径行业需映射表，缺则不计该配对；季度刷新。

**5.4 §18 约束进 construct**
- 直接读 `strategic_policy`（不重复定义）；4 条 binding 约束进引擎；`return_first` 下削成长腾出权重优先回流核心/债/金，加美核心受国别~11pp/货币~21pp 天花板（进 §10.4 投影）。
- **缺失 fail-closed**：申购状态、511010 行情、TE/费率/重合缺失 → 「不达准入/待复核」，绝不默认通过、不编数。

## 6. 所有者新决策点（已裁定 2026-06-07）

锁定 §18 默认上限**额外触发**了「中国 A 股权益 51% > 45%」——fan-out 审视发现、我此前聚合漏算的第 4 条 binding 约束。

**裁定：保留 `single_country_equity_max = 0.45`（设计默认，config 不变）。** 引擎将同时建议削成长（≤20%）**和**削中国 A 股整体（51%→≤45%，再~6pp）。所有者明确选择纪律约束而非主场押注，与 §18#4 选「设计默认 caps」一致。Phase C construct 须把这 4 条 binding 约束（single_satellite/satellite/growth_factor/single_country_equity）全部进求解。
