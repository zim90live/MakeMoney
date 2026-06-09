# 双向战术资产配置方案

> 状态：**已实现（影子/shadow，未接入可执行调仓）**，见 `engine/tactical.py`（权威实时标定以 [`HANDOFF.md`](HANDOFF.md) §0A 为准）  
> 定位：面向个人自用投顾产品的周度双向战术调仓系统  
> 目标成熟度：达到可实施、可解释、可回测、可审计的第一版，而不是试验性打分器

## 1. 目标与边界

现有周报主要回答：

> 当前实际仓位是否偏离长期战略目标，是否需要再平衡？

本方案增加第二个问题：

> 即使实际仓位接近战略目标，当前市场状态是否支持临时高配或低配该 ETF？

系统最终同时维护三种权重：

```text
战略权重 strategic_weight
    长期配置锚点，只在月度/季度策略审视中修改

战术目标 tactical_weight
    根据信号、风险预算和组合约束产生的临时目标

当前权重 current_weight
    根据真实持仓、现金和当前价格计算
```

周报根据 `current_weight` 与 `tactical_weight` 的差异给出加仓、减仓或持有建议。

第一版明确支持双向战术建议：

- 正向信号可以建议高于战略权重的战术加仓。
- 负向信号可以建议低于战略权重的战术减仓。
- 不做杠杆、不做空、不清仓单一战略资产。
- 战术层不能修改战略权重，只能在预先定义的带宽内临时偏离。
- 所有建议仍需经过数据、风险、交易质量与执行状态闸门。

## 2. 设计原则

### 2.1 战略与战术严格分离

- `portfolio.yaml.target_weight` 始终表示战略权重。
- 战术目标只存在于决策周期快照，不写回战略配置。
- 战术信号恢复后，目标自动回归战略权重。
- 应用新的战略权重后，现有战术周期立即失效并重新计算。

### 2.2 信号与动作严格分离

```text
原始数据
→ 子信号
→ 战术评分
→ 未约束战术目标
→ 组合约束后的战术目标
→ 动作触发
→ 执行质量检查
→ 最终可执行建议
```

任何单个信号都不能直接产生交易动作。

### 2.3 缺失不等于中性

- `valuation_na`：该资产不适用当前估值模型，不参与估值打分。
- `valuation_missing`：理论上适用，但数据缺失；降低建议置信度和最大倾斜幅度。
- 数据过旧或不可验证时，不生成基于该数据的战术倾斜。
- 不对缺失信号重新归一后放大剩余信号。

### 2.4 先控制模型风险，再追求收益

- 所有参数必须在回测前冻结，不允许根据最近表现反复调参。
- 上线前必须经过影子运行。
- 与静态战略组合比较时，必须计入交易成本、换手和税费假设。
- 评价目标不是单纯提高年化，而是改善净风险调整后收益和决策一致性。

## 3. 总体决策架构

```text
战略配置层
  strategic_weight
       │
       ▼
战术信号层
  价格状态 + 估值状态 + 信号置信度 + 状态机
       │
       ▼
单资产未约束倾斜
  raw_tactical_weight
       │
       ▼
组合构建层
  带宽 / 资金来源 / 主动权重预算 / 压力贡献 / 压力回撤
       │
       ▼
最终战术目标
  tactical_weight
       │
       ▼
动作层
  current_weight vs tactical_weight
  战术动作门槛 + 结构性再平衡门槛
       │
       ▼
执行层
  数据质量 / 单周上限 / 最小交易额 / 折溢价 / 申购状态
```

## 4. 信号模型

### 4.1 更新频率与数据时点

- 正式战术评分每周计算一次。
- 使用周报生成时已完成的最近交易日日终数据。
- 盘中实时价格只用于执行质量检查，不用于改变正式战术评分。
- 估值建议使用周频或日频缓存，但必须记录 `as_of`、来源和历史长度。
- 所有回测必须使用当时可获得的数据，禁止未来数据泄漏。

### 4.2 波动率标准化

不同资产的正常波动尺度不同。国债上涨 3% 与纳指上涨 3% 不能使用同一阈值解释。

对价格类信号统一使用近期波动率标准化：

```text
vol_63 = 63 日收益率年化波动率
vol_floor = 按资产类别设置的最低波动率，防止低波资产分数失控
effective_vol = max(vol_63, vol_floor)
```

建议初始 `vol_floor`：

| 资产类别 | vol_floor |
|---|---:|
| 债券 / 短债 | 4% |
| 黄金 | 12% |
| 防御权益 | 15% |
| 宽基权益 / 全球权益 | 18% |
| 成长权益 | 25% |

### 4.3 趋势子信号

趋势使用价格相对长期均线的距离，并按波动率标准化：

```text
trend_z = (price / MA200 - 1) / (effective_vol / sqrt(252))
s_trend = tanh(trend_z / trend_scale)
```

建议初始值：

```text
trend_scale = 8
```

使用 `tanh` 而不是硬 `clip`，让极端值平滑趋近 `[-1, +1]`，减少阈值附近跳变。

### 4.4 多周期时间序列动量

单独使用 60 日动量容易受短期反转影响。第一版使用两个周期：

```text
mom_63_z  = return_63  / (effective_vol * sqrt(63 / 252))
mom_126_z = return_126 / (effective_vol * sqrt(126 / 252))

s_mom_63  = tanh(mom_63_z  / momentum_scale)
s_mom_126 = tanh(mom_126_z / momentum_scale)

s_momentum = 0.6 * s_mom_63 + 0.4 * s_mom_126
```

建议初始值：

```text
momentum_scale = 2
```

250 日动量先作为研究输出，不进入第一版正式分数，避免与 MA200 趋势高度重复。

### 4.5 价格状态合成

趋势与动量高度相关，不能当作完全独立的两个大因子重复计权。

```text
s_price = 0.55 * s_trend + 0.45 * s_momentum
```

价格状态负责回答：

> 当前市场方向是否支持增加或降低暴露？

### 4.6 估值状态

对有可靠估值分位的资产：

```text
s_valuation_raw = 1 - 2 * valuation_percentile
```

含义：

- 分位 `0%`：`+1`，非常便宜。
- 分位 `50%`：`0`，中性。
- 分位 `100%`：`-1`，非常昂贵。

估值分数必须乘以可靠度：

```text
valuation_reliability =
    source_reliability
  * freshness_reliability
  * history_length_reliability

s_valuation = s_valuation_raw * valuation_reliability
```

建议最低要求：

- 历史不足 3 年：估值不进入正式战术分。
- 历史 3 至 5 年：可靠度最多 `0.5`。
- 历史至少 5 年且数据新鲜：可靠度可到 `1.0`。
- `valuation_na`：可用权重为 `0`，不视为故障。
- `valuation_missing`：可用权重为 `0`，同时降低总体置信度。

估值状态负责回答：

> 当前价格状态值得配置多大的正向或负向战术幅度？

估值不能单独触发大幅加仓或减仓。

### 4.7 因子交互

第一版不采用简单线性平均作为最终分数。以下两类组合具有更高置信度：

- 便宜且价格状态改善：提高正向倾斜。
- 昂贵且价格状态恶化：提高负向倾斜。

```text
positive_confirmation = max(s_price, 0) * max(s_valuation, 0)
negative_confirmation = max(-s_price, 0) * max(-s_valuation, 0)

s_interaction = positive_confirmation - negative_confirmation
```

### 4.8 信号覆盖率与置信度

推荐正式因子预算：

```text
价格预算 = 0.70
估值预算 = 0.30，其中：
  估值直接贡献 = 0.20
  价格×估值确认贡献 = 0.10
```

因此 `0.70 / 0.20 / 0.10` 与“价格 70% / 估值 30%”并不冲突：交互项依赖估值可用，属于估值预算的一部分。

不对缺失因子重新归一。组件不可用时，该组件贡献为 `0`：

```text
price_contribution       = 0.70 * s_price       if price_available else 0
valuation_contribution   = 0.20 * s_valuation   if valuation_available else 0
interaction_contribution = 0.10 * s_interaction if valuation_available else 0

s_raw = price_contribution + valuation_contribution + interaction_contribution
```

信号覆盖率用于解释和动作资格，不再重复乘入分数或倾斜：

```text
coverage =
    0.70 * price_coverage
  + 0.30 * valuation_reliability
```

其中：

```text
price_coverage =
    0.55 * trend_available
  + 0.45 * (
        0.60 * momentum_63_available
      + 0.40 * momentum_126_available
    )
```

各 availability 为 `0` 或 `1`；`valuation_reliability ∈ [0,1]`。

`data_quality_multiplier` 必须由该 ETF 自身的 provenance 派生，不能直接使用组合级 `grade_data`：某只 ETF 使用缓存或过旧，不应降低所有其他 ETF 的分数。最终展示置信度：

```text
confidence = coverage * data_quality_multiplier
```

建议：

| 状态 | data_quality_multiplier |
|---|---:|
| 完整 | 1.00 |
| 缓存可用且允许参与评分 | 0.60 |
| 过旧 / 部分缺失 | 0.00 |

数据质量只乘一次：

```text
s_quality = s_raw * data_quality_multiplier
```

估值缺失时，其 `0.20 + 0.10` 预算自然归零，价格贡献上限仍为 `0.70`，不会被重新放大，也不会再被覆盖率二次惩罚。

v1 动作资格：

```text
if confidence < minimum_action_confidence:
    不允许进入新的 positive_active / negative_active
    已有 active 状态只能保持、恢复或执行严格降低 whole_portfolio_stress 的动作
```

建议 `minimum_action_confidence = 0.55`。`confidence` 不再乘入 `raw_tilt`，避免与固定预算缺失惩罚重复计算。

> **当前组合的估值覆盖限制**：现有可靠历史估值主要覆盖沪深300与中证500。QDII、黄金为 `valuation_na`；红利低波、创业板、科创50目前为 `valuation_missing`。因此当前真实组合约 70% 权重的战术评分主要由价格状态驱动，双向策略中的“低估加仓 / 高估减仓”只对少数资产有效。正式上线结论必须显著披露这一限制；扩展估值数据源是 Phase D 的重要增强项，但不得用不可靠数据阻塞或伪装 v1。

### 4.9 方向保护规则

为了避免“下跌中因为便宜而机械加仓”或“上涨中因为昂贵而过早卖出”，增加以下保护：

方向保护的条件使用**置信度缩放前**的 `s_price / s_trend / s_momentum / s_valuation` 判断，但钳制对象统一是经过数据质量缩放后的 `s_quality`。方向保护结束后得到 `s_guarded`，再进入死区。

#### 下跌接刀保护

```text
if s_price <= -0.35:
    s_guarded = min(s_quality, 0)
```

价格状态明显为负时，估值便宜不能产生正向战术加仓，但仍可减弱负向减仓幅度。

#### 强趋势追高限制

```text
if s_price >= +0.50 and valuation_available and s_valuation <= -0.65:
    s_guarded = min(s_guarded, +0.20)
```

趋势很强但估值极贵时，可以持有或小幅高配，不允许产生大幅追高建议。

#### 极端风险加速

```text
if price_coverage == 1 and data_quality_multiplier == 1
   and s_trend <= -0.75 and s_momentum <= -0.60:
    s_guarded = min(s_guarded, -0.55)
```

趋势与动量同时严重恶化时，提高风险减仓最低强度。

### 4.10 死区与连续分数

```text
if abs(s_guarded) < deadband:
    effective_score = 0
else:
    effective_score =
        sign(s_guarded)
        * (abs(s_guarded) - deadband)
        / (1 - deadband)
```

建议初始值：

```text
deadband = 0.20
```

死区外重新映射到 `[0, 1]`，确保刚越过死区时只产生很小倾斜，而不是突然跳到较大仓位。

### 4.11 权威有序打分流水线

以下函数是 v1 唯一权威计算顺序。实现、回测、解释层和单元测试必须调用同一套纯函数，不允许各自重写。

```text
score_asset(inputs):
    1. 校验输入时点与可用性
       price_available, price_coverage, valuation_available
       valuation_reliability, data_quality_multiplier

    2. 计算价格子信号
       s_trend
       s_mom_63
       s_mom_126
       s_momentum = 0.6*s_mom_63 + 0.4*s_mom_126
       s_price = 0.55*s_trend + 0.45*s_momentum
       不可用的价格子项记 0，并反映到 price_coverage；不重归一

    3. 计算估值子信号
       if valuation_available:
           s_valuation_raw = 1 - 2*percentile
           s_valuation = s_valuation_raw * valuation_reliability
       else:
           s_valuation = 0

    4. 计算交互项
       if valuation_available:
           s_interaction =
               max(s_price,0)*max(s_valuation,0)
               - max(-s_price,0)*max(-s_valuation,0)
       else:
           s_interaction = 0

    5. 固定预算合成，不重归一
       s_raw =
           0.70*s_price
           + 0.20*s_valuation
           + 0.10*s_interaction

    6. 数据质量缩放
       s_quality = s_raw * data_quality_multiplier
       coverage = 0.70*price_coverage + 0.30*valuation_reliability
       confidence = coverage * data_quality_multiplier

    7. 方向保护，钳制 s_quality
       s_guarded = s_quality
       跌势接刀保护
       强趋势追高限制
       完整高质量价格信号下的极端风险加速

    8. 死区映射
       s_guarded 在死区内 → effective_score = 0
       死区外 → 连续映射至 [-1,+1]

    9. 输出全部中间值
       子信号、贡献项、coverage、confidence、s_raw、
       s_quality、方向保护命中项、effective_score
```

#### 手算样例

假设纳指战略权重 `13%`，完整日终数据：

```text
s_trend       = -0.60
s_mom_63      = -0.50
s_mom_126     = -0.30
s_momentum    = 0.6*(-0.50) + 0.4*(-0.30) = -0.42
s_price       = 0.55*(-0.60) + 0.45*(-0.42) = -0.519

估值分位       = 85%
s_valuation_raw = 1 - 2*0.85 = -0.70
估值可靠度      = 0.80
s_valuation     = -0.70*0.80 = -0.56

s_interaction = -(0.519*0.56) = -0.291
s_raw = 0.70*(-0.519) + 0.20*(-0.56) + 0.10*(-0.291)
      = -0.504

data_quality_multiplier = 1
s_quality = -0.504
coverage = 0.70*1 + 0.30*0.80 = 0.94
confidence = 0.94

方向保护：s_price <= -0.35，只限制不能转为正分；本例仍为 -0.504
deadband = 0.20
effective_score = -((0.504-0.20)/(1-0.20)) = -0.380

进取 beta_down = 0.55
raw_tilt = 13% * 0.55 * -0.380 = -2.72pp
raw_tactical_weight = 10.28%
```

该样例必须固化为单元测试，所有数值允许的舍入误差不超过 `1e-6`。

## 5. 状态机与迟滞

仅使用死区不足以抑制每周反复切换。每只 ETF 必须保存战术状态：

```text
neutral
positive_watch
positive_active
negative_watch
negative_active
recovering
```

建议进入与退出规则：

下表中的 `score` 统一指 §4.11 输出的 `effective_score`。

| 状态变化 | 条件 |
|---|---|
| neutral → positive_watch | `score >= +0.25` |
| positive_watch → positive_active | 连续 2 个正式周期 `score >= +0.25`，或单周期 `score >= +0.60` |
| positive_active → recovering | `score < +0.10` |
| neutral → negative_watch | `score <= -0.25` |
| negative_watch → negative_active | 连续 2 个正式周期 `score <= -0.25`，或单周期 `score <= -0.60` |
| negative_active → recovering | `score > -0.10` |
| recovering → neutral | 连续 2 个周期处于死区 |
| recovering → active | 再次满足相应 active 条件 |

补充约束：

- 正负方向直接反转时，除非分数跨过 `±0.60` 极端阈值，否则至少经过一个 `recovering` 周期。
- 每只 ETF 执行战术调整后设置一周冷却期；冷却期内只允许执行后验 `whole_portfolio_stress` 严格低于执行前的动作。
- 恢复战略权重分两期完成，避免一次性追涨或反向交易。

### 5.1 状态读取与持久化契约

状态机每次计算前，必须从**上一份正式决策周期**读取该 ETF 状态，而不是从任意最近生成的报告读取。

正式周期定义复用 `reports._formal_reports_for_review` 的口径：

- 每个自然日最多一份正式周期。
- 同日重复刷新只使用最后一份，不增加连续周期计数。
- 当前周期生成时，从严格早于当前周期的最后一份正式周期读取状态。
- 没有历史状态时初始化为 `neutral`，连续计数和冷却计数均为 `0`。
- 战略配置、战术模型版本或 ETF universe 发生变化时，对受影响资产重置为 `neutral`。

每只 ETF 持久化：

```text
tactical_state
├── state
├── direction
├── entered_at_cycle_id
├── consecutive_enter_count
├── consecutive_recovery_count
├── cooldown_remaining_cycles
├── last_effective_score
├── last_tactical_weight
└── transition_reason
```

写入当前决策周期的 `tactical_diagnostics[code].state_after`，下一正式周期读取该字段。历史报告只读，不另建可被覆盖的“当前状态”文件，避免状态源分裂。

## 6. 从分数到单资产战术目标

### 6.1 风险偏好参数

`risk_profile` 不改变信号方向，只调整允许的倾斜幅度和主动预算。

建议初始配置：

| 参数 | 保守 | 平衡 | 进取 |
|---|---:|---:|---:|
| `beta_up` | 0.15 | 0.25 | 0.35 |
| `beta_down` | 0.35 | 0.45 | 0.55 |
| 单资产上行带宽 | 战略权重的 15% | 25% | 35% |
| 单资产下行带宽 | 战略权重的 35% | 45% | 55% |
| 组合主动权重预算 | 5% | 8% | 12% |

即使定位升级为自用投顾产品，也建议负向调整幅度大于正向调整幅度。原因是下行保护的可验证性通常高于主动追求超额收益。

### 6.2 未约束倾斜

```text
beta =
    beta_up   if effective_score > 0
    beta_down if effective_score < 0

raw_tilt = strategic_weight * beta * effective_score
raw_tactical_weight = strategic_weight + raw_tilt
```

### 6.3 单资产带宽

每只 ETF 可在 `strategy.yaml` 覆盖默认带宽。

```text
upside_band   = strategic_weight * upside_band_ratio
downside_band = strategic_weight * downside_band_ratio

lower_bound = max(
    strategic_weight * minimum_retention_ratio,
    strategic_weight - downside_band
)

upper_bound = min(
    strategic_weight + upside_band,
    single_asset_absolute_cap
)
```

建议：

- `minimum_retention_ratio = 0.40`：战术层最多减掉战略仓位的 60%，不清仓。
- `single_asset_absolute_cap = 0.30`：任何单只 ETF 战术目标不超过 ETF 桶的 30%。
- 债券作为资金缓冲资产，可设置不同带宽。
- 所有 profile 表中的“战略权重的 X%”均为相对比例；进入公式前必须按上述方式转成绝对权重百分点。

## 7. 组合构建与资金去向

### 7.1 不直接归一化风险资产

简单归一化可能造成：

> 纳指减仓后，被迫按比例加仓创业板和科创50。

因此组合构建必须显式定义资金去向。

### 7.2 资金来源与承接顺序

为现有唯一债券 ETF 定义 `reserve_asset: "511010"`。

v1 中 `reserve_asset` **不参与独立战术评分和状态机**。它仍可展示趋势、动量等观察信息，但这些信息不改变其战术目标。它是组合构建的资金缓冲器，其最终权重由战略权重、风险资产释放资金、正向加仓资金需求、上下界和压力约束共同决定。这样避免“债券自身负向评分要求减仓，但系统又要求它承接风险资金”的双重身份冲突。

#### 风险资产减仓释放资金

```text
风险资产减仓
→ 优先增加 reserve_asset
→ reserve_asset 达到上限后保留现金
```

#### 风险资产战术加仓资金来源

```text
先使用 ETF 桶内可用现金
→ 再降低 reserve_asset 至其下限
→ 不卖出其他风险资产为单一风险资产追涨，除非其自身也有负向战术信号
```

#### 多资产同时高配

当正向需求超过可用资金时，v1 对所有正向需求使用同一个缩放系数：

```text
positive_scale = available_funding / total_positive_demand
allocated_positive_i = positive_demand_i * positive_scale
```

各资产原始需求已经由自身分数、beta 和带宽决定；统一同比缩放能够保持这些相对关系，且比再次引入一套“分配优先级分数”更容易解释、测试和复现。更复杂的边际风险资金分配移至 Phase D。

### 7.3 主动权重预算

第一版使用容易解释的主动权重偏离预算：

```text
active_weight_budget =
    (
      sum(abs(tactical_asset_weight - strategic_asset_weight))
      + abs(tactical_cash_target - strategic_cash_target)
    ) / 2
```

该指标不是统计意义上的 Tracking Error，产品文案统一称为：

> 战术偏离预算

超过预算时，按所有倾斜比例整体缩放，直至满足限制。

当前战略配置的 `strategic_cash_target = 0`。必须把目标现金纳入公式，否则“资产减仓 10pp → 现金增加 10pp”会被错误计算为仅 `5pp` 主动偏离。

### 7.4 v1 风险集中度约束

v1 **不实现协方差与统计风险贡献**。原因是不等上市期、短历史资产和收缩估计方法尚未形成可靠契约，贸然加入会成为模型中最弱且最难审计的部分。

v1 使用现有资产类别压力冲击作为唯一风险尺度：

```text
stress_contribution_i = tactical_weight_i * abs(asset_shock_i)
marginal_stress_delta_i =
    tactical_whole_stress_after_i - tactical_whole_stress_before_i
```

建议约束：

- 单只 ETF 的允许压力贡献上限：

  ```text
  max(config_asset_ratio * strategic_total_stress, strategic_asset_stress_contribution)
  ```

- 单一 sleeve 的允许压力贡献上限：

  ```text
  max(config_sleeve_ratio * strategic_total_stress, strategic_sleeve_stress_contribution)
  ```

- 使用战略压力作为固定分母，避免其他资产减仓后，某只未加仓资产仅因占比被动上升而违规。
- “降低风险动作”精确定义为执行后 `whole_portfolio_stress` 严格低于执行前；相等不算降低风险。
- 协方差、收缩估计、不等历史处理和统计风险贡献移至 Phase D，需单独设计与验证后才能进入正式模型。

### 7.5 全组合压力回撤约束

对最终 `tactical_weight` 复用 `whole_portfolio_stress`：

```text
if tactical_whole_stress > max_acceptable_drawdown:
    收缩所有增加压力风险的正向倾斜
```

必须区分动作方向：

- 降低压力回撤的减仓或增配债券动作允许执行。
- 增加压力回撤的动作收缩或阻止。
- 不再因为风险预算超标而统一阻止所有动作。

### 7.6 权威有序组合构建算法

以下 `construct_tactical_portfolio()` 是 v1 唯一权威组合构建顺序。实现、影子输出、回测和解释层必须复用同一纯函数。

核心原则：

> 所有约束处理只能收缩风险资产的战术倾斜；释放出的权重只能进入 reserve 或现金，不能重新分配给其他风险资产。

因此后置约束不会重新破坏前置约束，也不需要循环投影或求不动点。

```text
construct_tactical_portfolio(
    strategic_weights,
    effective_scores,
    bounds,
    reserve_asset,
    active_weight_budget,
    stress_limits,
    max_whole_stress
):
    辅助函数 settle_reserve_and_cash(risk_targets):
       desired_reserve = 1 - sum(risk_targets)
       reserve_target = clip(desired_reserve, reserve_lower_bound, reserve_upper_bound)
       tactical_cash_target = 1 - sum(risk_targets) - reserve_target
       断言 tactical_cash_target >= -1e-9
       返回 reserve_target, max(tactical_cash_target, 0)

    1. 初始化
       strategic_weights 合计必须为 1；当前真实现金不属于战略权重输入
       对所有风险资产：
           raw_target_i = strategic_i * (1 + beta_i * effective_score_i)
           bounded_target_i = clip(raw_target_i, lower_i, upper_i)
       reserve_target = strategic_reserve
       tactical_cash_target = 0

    2. 汇总风险资产负向释放与正向需求
       对 bounded_target_i < strategic_i 的风险资产：
           保留减仓后的 bounded_target_i
           negative_release += strategic_i - bounded_target_i
       对 bounded_target_i > strategic_i 的风险资产：
           positive_demand_i = bounded_target_i - strategic_i

    3. 确定性分配正向倾斜并结算 reserve / 目标现金
       available_funding =
           negative_release
           + max(strategic_reserve - reserve_lower_bound, 0)
       若总 positive_demand > available_funding：
           positive_scale = available_funding / total_positive_demand
           对全部正向需求使用同一 positive_scale
       得到风险资产候选目标后，调用 settle_reserve_and_cash()

    4. 应用战术偏离预算
       计算 active_weight_budget_used，包含 tactical_cash_target
       若超限：
           对所有风险资产相对 strategic 的倾斜使用同一缩放系数
           通过单调二分确定满足预算的最大可行系数
           调用 settle_reserve_and_cash()
           不增加任何其他风险资产的战术倾斜绝对值

    5. 应用单资产与 sleeve 压力集中限制
       按 ETF code 升序检查单资产，再按 sleeve 名称升序检查 sleeve
       对违规项只收缩其“增加压力的倾斜”，直至达到限制
       每次收缩后调用 settle_reserve_and_cash()
       负向倾斜不得因集中度限制被反向放大
       若战略组合自身已超过配置限制，则该项可行上限取
       max(配置限制, 战略组合原始集中度)，战术层不得进一步恶化，但不负责修复战略配置

    6. 应用全组合压力回撤限制
       若 tactical_whole_stress > max_whole_stress：
           仅对增加 whole_portfolio_stress 的正向倾斜同比缩放
           通过单调二分确定最大可行缩放系数
           调用 settle_reserve_and_cash()
       若 strategic 组合自身已超预算：
           不要求战术组合修复全部战略风险
           但 tactical_whole_stress 不得高于 strategic_whole_stress

    7. 最终现金与 reserve 结算
       reserve_target 保持在 [reserve_lower_bound, reserve_upper_bound]
       所有无法合法分配的目标权重保留为 tactical_cash_target
       不强制 ETF 权重归一到 1

    8. 执行全部守恒和约束断言
       通过 → 返回 tactical_weights + tactical_cash_target + diagnostics
       失败 → 回退 strategic_weights + tactical_cash_target=0
              标记 portfolio_construction_failed
```

#### 确定性要求

- 需要顺序检查的集合默认按 ETF code 或 sleeve 名称升序。
- 主动预算与压力预算二分固定迭代 `40` 次，并返回保守侧可行值；这不是组合约束循环，而是单一单调标量求解。
- 浮点计算使用全精度，最终展示时才舍入。
- 同输入、同模型版本必须逐字段产生相同输出。

#### 必须成立的不变量

```text
sum(tactical_asset_weights) + tactical_cash_target == 1 ± 1e-9
所有资产权重 >= 0
不使用杠杆
所有资产位于上下界内
reserve 位于自身上下界内
active_weight_budget_used <= active_weight_budget + 1e-9
单资产与 sleeve 压力集中度不超限
tactical_whole_stress 满足 §7.6 第 6 步规则
任何约束收缩均不得增加其他风险资产权重
```

若战略组合自身已超过某项压力集中配置限制，上述“不超限”指“不超过战略基准与配置限制二者中的较高值”；回退战略组合必须始终是合法失败结果。

#### 最小手算构建样例

```text
输入：
  战略：风险资产 A 40%，风险资产 B 30%，reserve 30%（战略权重合计 100%）
  A 原始战术目标 50%，上限 48%
  B 原始战术目标 20%，下限 18%
  reserve 上下界 10% / 35%
  主动偏离预算充足，压力限制未触发

步骤：
  A 经带宽后需要 +8pp
  B 经带宽后释放 10pp
  B 释放的 10pp 中，8pp 用于满足 A 的正向倾斜
  剩余 2pp 进入 reserve：reserve 30% → 32%

输出：
  A 48%，B 20%，reserve 32%，目标现金 0%
  权重合计 100%
  无风险资产间被动再分配
```

该样例及“约束失败回退战略组合”的样例必须固化为单元测试。

> **目标现金与真实现金必须区分**：`tactical_cash_target` 是模型希望保留的目标现金权重；当前真实现金只在动作生成与执行阶段用于判断买入是否已有资金。真实现金较多不会提高战术目标，也不会使目标权重总和超过 100%。

## 8. 动作生成

### 8.1 两种动作来源

周报必须分别展示：

#### 结构性再平衡

```text
current_weight vs strategic_weight
```

用于修复长期配置偏离，继续使用现有 5/25 规则。

#### 战术调整

```text
current_weight vs tactical_weight
```

用于执行市场状态带来的临时高配或低配。

最终动作应合并为指向同一个 `tactical_weight` 的净动作，但必须保留原因拆解。

### 8.1.1 与首次建仓的合流

`first_funding_plan` 只表示**整个 ETF 桶当前完全为 0 持仓**时的首次建仓流程；某个单独 sleeve 的 `shares=0` 不应被当作独立首次建仓状态。

合流规则：

#### 整个 ETF 桶为 0 持仓

```text
先计算所有风险资产分数
普通分数保持 neutral，不影响首批配置
只有 abs(effective_score) >= immediate_threshold 的资产可立即进入 active
first_funding_plan 使用“战略权重 + 已立即激活的战术倾斜”分配首批资金
首批总额仍受 first_tranche_pct 与 max_weekly_trade_amount 限制
```

- 首建路径绕过“必须已有 active/recovering 状态”的一般动作资格闸，但不绕过 `immediate_threshold`、带宽、压力预算、数据质量和执行质量闸。
- 普通 `positive_watch / negative_watch` 信号不得改变首次建仓分配，避免用未经连续确认的单周信号改变首批配置。
- 极端正向战术资产可以获得略高的首批分配。
- 极端负向战术资产可以获得较低首批分配，但只要其战略权重大于 0，仍受 `minimum_retention_ratio` 保护。
- 存在立即激活的战术倾斜时，首建动作来源标记为 `first_funding+tactical`；否则标记为 `first_funding`。
- 不另外生成结构性再平衡或战术动作，避免同一周期重复下单。

#### ETF 桶已有任意持仓，但某个 sleeve 为 0

```text
该 sleeve 与其他资产一样按 current_weight=0 对 tactical_weight 比较
不走 first_funding_plan
```

- 若其战术目标和动作门槛允许，则产生普通净买入动作。
- 若负向战术信号令战术目标很低，可能继续持有 0 份，不机械补齐战略权重。

#### 与 `cycle_suggestions` 的关系

每只 ETF 每个周期最多产生一条净动作：

```text
action_key = cycle_id + code + side
source ∈ {
  first_funding+tactical,
  structural,
  tactical,
  structural+tactical
}
```

`cycle_suggestions` 只消费合并后的净动作，不再分别拼接首次建仓、结构再平衡和战术建议。

### 8.2 战术动作门槛

战术调整不能完全复用 5/25，否则多数合理倾斜不会触发。

建议触发条件：

```text
abs(tactical_weight - current_weight) >= tactical_abs_threshold_pp
或
abs(tactical_weight - current_weight) / max(strategic_weight, epsilon)
    >= tactical_rel_threshold
```

建议初始值：

```text
tactical_abs_threshold_pp = 1.0
tactical_rel_threshold = 0.10
```

同时必须满足：

- 建议金额达到 `min_trade_amount`。
- 状态机处于 `positive_active`、`negative_active` 或 `recovering`。
- 整桶首次建仓按 §8.1.1 的专用规则，不受上一条一般状态资格限制。
- 调整后的目标确实改善与 `tactical_weight` 的距离。
- 单周总交易额不超过上限。

### 8.3 冲突处理

结构性再平衡与战术信号可能方向冲突。

例：

```text
战略目标 13%
当前权重 10%
战术目标 9%
```

虽然当前低于战略目标，但战术目标更低，最终应建议减仓至 9%，而不是结构性加仓。

统一规则：

```text
最终建议方向只由 current_weight → tactical_weight 决定。
战略偏离仅作为解释，不单独产生与战术目标冲突的交易。
```

解释文案：

> 当前仓位低于长期战略目标，但负向战术信号令本周期目标进一步降至 9%，因此仍建议减仓；信号恢复后再逐步回归 13%。

### 8.4 金额与份额

```text
desired_amount = abs(tactical_weight - current_weight) * portfolio_value
```

- 买入份额继续按一手向下取整。
- 卖出不得超过真实持仓。
- 当前真实现金优先用于执行买入；不足部分只能来自同周期已确认卖出或 reserve 减仓，不能因为目标组合理论可行就假设资金已经到账。
- 小于最小交易金额时保留为“待积累偏离”，不产生可执行动作。
- 同方向未执行偏离可以跨周期累积，但每个新周期重新计算，不沿用过期金额。

## 9. 执行闸门

现有执行闸继续保留，并按方向升级。

### 9.1 数据质量闸

- 正式评分使用的数据缺失或过旧：禁止新增战术倾斜。
- 可允许向战略权重回归，或允许明确降低风险的动作。
- 缓存数据若策略禁止交易，不生成可执行战术动作。

### 9.2 风险预算闸

- 阻止增加压力回撤的动作。
- 允许降低压力回撤的动作。
- 每个动作应计算执行前后压力回撤变化，并写入解释。
- “降低风险”唯一判定：执行后 `whole_portfolio_stress <` 执行前；不能仅凭“卖出权益”或“买入债券”标签判断。

### 9.3 ETF 执行质量闸

- 买入继续检查折溢价和申购状态。
- 高溢价买入被暂缓时，战术目标不变，但动作状态为 `blocked_now`。
- 卖出不因高溢价被阻止。
- 执行前重新检查实时质量。

### 9.4 交易成本闸

新增净收益门槛：

```text
expected_tactical_benefit > estimated_round_trip_cost * cost_multiplier
```

第一版无法可靠预测收益时，使用更保守的替代规则：

- 预估双边成本包含佣金、滑点和折溢价风险。
- 建议金额必须达到成本的至少 `10` 倍。
- 对高换手资产提高门槛。

## 10. 建议解释与产品呈现

### 10.1 周报顶部结论

```text
本周战术判断：风险偏积极
战略组合 → 战术组合：主动偏离 4.2%，全组合压力回撤 17.0% → 18.1%
建议：战术加仓 2 项、战术减仓 1 项、结构性再平衡 1 项、持有 5 项
```

### 10.2 每只 ETF 的完整解释

以下示例与 §4.11 冻结测试向量对齐；组合约束未进一步收缩该资产：

```text
纳指ETF 513100

战略权重       13.0%
当前权重       13.1%
战术目标       10.28%
本周建议       减仓约 2.82pp / 按当前组合价值换算

价格状态       -0.519
  趋势         -0.600
  63日动量     -0.500
  126日动量    -0.300
  合成动量     -0.420

估值状态       -0.560  历史分位 85%，可靠度 0.80
交互确认       -0.291  昂贵且价格状态恶化
数据置信度      94%
质量缩放后分数 -0.504
最终战术分     -0.380

约束调整
  原始战术目标  10.28%
  本例未触发组合级进一步收缩
  实际压力影响由组合构建函数计算

恢复条件
  最终战术分回到 -0.10 以上并按状态机连续确认，分批回归战略权重
```

### 10.3 必须展示的口径

- 战略权重、战术目标、当前权重。
- 动作来源：结构性 / 战术 / 两者共同。
- 每个子信号、可靠度、最终分数。
- 原始倾斜与约束后倾斜。
- 资金来源或资金去向。
- 对组合压力回撤和压力贡献的影响。
- 状态机状态、进入时间、恢复条件。
- 被阻止时的明确原因。

### 10.4 决策状态

沿用当前决策周期状态，并增加动作来源：

```text
pending
blocked_now
executed
skipped
rejected
expired
```

用户跳过或否决战术动作后，必须记录原因。下一周期重新计算，但历史决定保留用于策略复盘。

### 10.5 多周状态与执行时间线

以下样例说明迟滞、冷却、恢复分批和“改善与战术目标距离”如何共同工作：

| 正式周期 | effective_score | 状态变化 | 战术目标 | 允许动作 |
|---|---:|---|---:|---|
| W1 | -0.32 | `neutral → negative_watch` | 仍按战略 13% | 不交易，等待确认 |
| W2 | -0.40 | `negative_watch → negative_active` | 10.8% | 从当前 13% 减至 10.8%；执行后进入 1 周冷却 |
| W3 | -0.48 | 保持 `negative_active`，冷却中 | 10.3% | 仅因继续减至 10.3% 严格降低 whole stress，允许继续减仓 |
| W4 | -0.05 | `negative_active → recovering` | 第一恢复档约 11.7% | 允许分批恢复一半，不一次回到 13% |
| W5 | 0.00 | 保持 `recovering`，连续恢复计数 2 | 13.0% | 完成第二档恢复，回归战略权重 |
| W6 | +0.30 | `neutral → positive_watch` | 仍按战略 13% | 不交易，等待正向确认 |

若 W4 的执行质量闸阻止恢复买入，状态仍为 `recovering`，下一周期重新计算目标与剩余距离；不得沿用旧成交金额。

## 11. 建议配置结构

建议在 `strategy.yaml` 新增独立模块：

```yaml
tactical_allocation:
  enabled: false
  mode: shadow
  reserve_asset: "511010"
  reserve_participates_in_scoring: false

  signals:
    price_weight: 0.70
    valuation_weight: 0.30
    trend_weight_within_price: 0.55
    momentum_63_weight: 0.60
    momentum_126_weight: 0.40
    trend_scale: 8.0
    momentum_scale: 2.0
    deadband: 0.20

  confidence:
    cached_data_multiplier: 0.60
    minimum_action_confidence: 0.55
    valuation_min_years: 3
    valuation_full_years: 5

  state_machine:
    enter_threshold: 0.25
    immediate_threshold: 0.60
    exit_threshold: 0.10
    confirmation_cycles: 2
    recovery_cycles: 2
    cooldown_cycles: 1

  profiles:
    保守:
      beta_up: 0.15
      beta_down: 0.35
      active_weight_budget: 0.05
    平衡:
      beta_up: 0.25
      beta_down: 0.45
      active_weight_budget: 0.08
    进取:
      beta_up: 0.35
      beta_down: 0.55
      active_weight_budget: 0.12

  constraints:
    minimum_retention_ratio: 0.40
    single_asset_absolute_cap: 0.30
    reserve_lower_bound: 0.03
    reserve_upper_bound: 0.35
    upside_band_ratio:
      保守: 0.15
      平衡: 0.25
      进取: 0.35
    downside_band_ratio:
      保守: 0.35
      平衡: 0.45
      进取: 0.55
    max_asset_stress_contribution: 0.35
    max_sleeve_stress_contribution: 0.45

  actions:
    tactical_abs_threshold_pp: 1.0
    tactical_rel_threshold: 0.10
    recovery_tranches: 2
```

`enabled: false` 且 `mode: shadow` 是上线初始状态。通过验收后再切换为正式建议。

## 12. 数据与持久化契约

### 12.1 决策周期新增字段

```text
decision_cycle
├── tactical_model_version
├── tactical_config_version
├── signal_as_of
├── portfolio_input_fingerprint
├── strategic_weights
├── tactical_weights
├── tactical_cash_target
├── active_weight_budget_used
├── strategic_stress
├── tactical_stress
├── portfolio_construction_diagnostics
├── tactical_actions
└── tactical_diagnostics
```

### 12.2 单资产诊断结构

```json
{
  "code": "513100",
  "inputs": {
    "price_source": "frozen_test_fixture",
    "price_as_of": "2026-06-05",
    "price_value_used": 2.22,
    "history_start": "2020-01-01",
    "valuation_source": "frozen_test_fixture",
    "valuation_as_of": "2026-06-05",
    "input_fingerprint": "sha256:..."
  },
  "strategic_weight": 0.13,
  "current_weight": 0.131,
  "raw_tactical_weight": 0.1028,
  "tactical_weight": 0.1028,
  "state": "negative_active",
  "score": {
    "trend": -0.60,
    "momentum_63": -0.50,
    "momentum_126": -0.30,
    "momentum": -0.42,
    "price": -0.519,
    "valuation": -0.56,
    "interaction": -0.291,
    "raw": -0.504,
    "quality_adjusted": -0.504,
    "coverage": 0.94,
    "confidence": 0.94,
    "effective": -0.38
  },
  "constraints": [],
  "risk_impact": {
    "whole_stress_delta": null,
    "stress_contribution_delta": null
  }
}
```

`input_fingerprint` 必须由本次评分实际使用的价格序列、估值序列摘要、参数版本和数据时点共同生成。跨机器或不同数据源得到不同输入时，允许输出不同，但必须能够识别为不同输入；同一指纹必须产生相同评分结果。

### 12.3 模型版本

任何以下变化必须升级 `tactical_model_version` 并使活动周期失效：

- 信号公式。
- 参数权重。
- 状态机阈值。
- 风险约束。
- 估值数据源或计算口径。
- 资金承接规则。

## 13. 回测与验证框架

### 13.1 必须比较的策略

至少比较：

1. 静态战略组合。
2. 当前 5/25 再平衡策略。
3. 仅负向战术风险覆盖层。
4. 双向战术配置策略。
5. 双向策略去掉估值后的版本。
6. 双向策略去掉状态机后的版本。

这样才能判断收益来自哪里，以及复杂度是否真的有价值。

### 13.2 回测实现要求

- 使用周频正式决策点。
- 信号使用决策点之前可获得的数据。
- 估值数据必须按历史时点重建；无法重建时，不允许用当前估值分位回填历史。
- v1 估值臂的历史回测预计主要只覆盖沪深300与中证500；其余资产按当时真实的 `valuation_na / valuation_missing` 处理。因此回测结论必须拆分披露“有估值覆盖资产”和“仅价格状态资产”，不得把局部估值效果外推到整个组合。
- 使用真实 ETF 可交易历史作为主要结果。
- 指数代理长历史只用于压力轮廓，不作为最终收益结论。
- 计入佣金、滑点、买卖价差与合理折溢价成本。
- 模拟最小交易金额、整手约束、单周交易上限和执行质量阻断。
- 记录无法成交、被闸门阻止和延迟成交的动作。

### 13.3 评价指标

收益与风险：

- CAGR
- 年化波动率
- 最大回撤
- Calmar
- Sharpe / Sortino
- 最长水下时间
- 下行捕获率 / 上行捕获率

交易与稳定性：

- 年化换手率
- 年均动作次数
- 平均持有周期
- 单次动作后的 4/12/26 周收益分布
- 信号反转率
- 被死区、迟滞和冷却期过滤的动作数
- 战术偏离预算使用率

产品一致性：

- 建议可执行率
- 执行质量阻断率
- 因数据缺失降低置信度的比例
- 每条建议是否具有完整解释链

### 13.4 时间切分

必须采用：

- 样本内：仅用于确定合理参数范围。
- 样本外：用于正式评价。
- Walk-forward：按时间滚动冻结参数并测试。
- 参数扰动：核心参数上下浮动至少 `20%`，验证结果是否稳定。

禁止只选择表现最好的单组参数。

### 13.5 上线验收门槛

双向战术策略进入正式建议模式前，应同时满足：

#### 模型正确性

- 所有纯函数和状态转换有单元测试。
- 缺失估值不会被当中性或被剩余信号放大。
- 风险降低动作不会被风险预算闸误阻止。
- 组合权重、资金和交易金额守恒。
- 同一输入与模型版本产生完全可复现结果。

#### 样本外表现

相对当前 5/25 策略，扣除成本后至少满足：

- 最大回撤不恶化超过 `2pp`。
- Calmar 或 Sortino 至少一个改善 `10%`。
- CAGR 不恶化超过 `1pp`，或若恶化则最大回撤改善至少 `20%`。
- 年化换手率不超过 `150%`。
- 参数扰动后主要结论不反转。

这些不是收益承诺，而是防止上线明显无效或过拟合的最低工程门槛。

#### 影子运行

- 连续运行至少 `8` 个正式周度周期。
- 只展示建议，不进入可执行调仓列表。
- 每周记录：建议、约束、实际市场后续表现、用户是否认同。
- 不出现无法解释的方向反转或明显数据口径错误。

影子运行通过后，才将 `mode: shadow` 改为 `mode: advisory`。

## 14. 复盘体系

### 14.1 周度复盘

每周记录：

- 战术分是否变化。
- 状态机为何进入、保持、退出。
- 动作是否被执行、跳过、否决或阻断。
- 阻断是否避免了不良交易质量。

### 14.2 月度复盘

重点回答：

- 战术建议是否过于频繁。
- 正向与负向建议分别贡献了什么。
- 哪些因子长期主导分数。
- 哪些资产因估值缺失长期只能依赖价格信号。
- 战术偏离是否真实改善风险调整后表现。

### 14.3 模型治理

- 参数只允许在月度或季度模型审视中修改。
- 每次修改必须记录原因、旧值、新值和预期影响。
- 修改后重新进入影子模式，不直接用于真实建议。
- 不因连续几周亏损临时改变模型。

## 15. 实施路线

### 15.1 开工前置门槛

以下契约未完成前，不得开始正式战术动作实现：

1. §4.11 权威有序打分流水线、手算样例和对应单元测试冻结。
2. §5.1 上一正式周期状态读取、同日去重、连续计数和模型变更重置规则冻结。
3. §6.3 相对带宽转绝对权重的公式冻结。
4. §4.8 与 §13.2 的估值覆盖限制写入影子周报和回测结果。
5. v1 明确只使用压力冲击，不实现协方差风险贡献。
6. §8.1.1 首次建仓、结构再平衡和战术动作合流规则冻结。
7. 周频事件驱动回测模拟器拥有独立任务线，并与影子计算使用相同纯函数。
8. §7.6 权威组合构建算法、守恒断言、最小手算样例和失败回退测试冻结。
9. §12.2 输入来源、时点与 `input_fingerprint` 审计字段冻结。

### 阶段 A：纯计算与影子输出

- 在 `signals.py` 增加纯函数形式的子信号、评分、状态转换和战术目标计算。
- 先固化 §4.11 权威流水线、手算样例与状态读取契约；未完成前不得产生影子分数。
- 不改变现有 `actionable_rebalance`。
- 周报增加“影子战术建议”区域。
- 保存模型诊断与状态历史。
- 与阶段 A 并行搭建周频事件驱动回测模拟器骨架；它是上线验收长杆，不能等影子功能全部完成后才开始。

### 阶段 B：战术组合约束与回测

- 实现资金来源、reserve asset、主动权重预算和压力贡献约束。
- 扩展 `backtest.py` 支持周频双向战术策略和真实动作门槛。
- 完成消融、walk-forward、成本与参数敏感性测试。

### 阶段 C：正式投顾建议

- 影子运行和验收门槛通过后，生成 `tactical_actions`。
- 将战术动作接入统一决策周期和调仓流程。
- 首页清楚区分结构性再平衡与战术调整。
- 月度复盘增加模型有效性与建议质量分析。

### 阶段 D：后续增强

- 引入更可靠的全球与成长资产估值数据。
- 单独设计协方差估计窗口、不等历史处理、收缩估计与更新频率；验证通过后再考虑统计风险贡献约束。
- 增加相关性状态、宏观状态或横截面相对强弱，但每次只新增一个可消融验证的因子。
- 使用真实 TWR/MWR 和基准组合评价战术层的实际贡献。

## 16. 第一版明确不做

- 不使用 AI 新闻文本直接计算仓位分数。
- 不根据单条政策新闻自动大幅调仓。
- 不做杠杆、做空、期权或个股建议。
- 不让估值单独触发大幅逆势加仓。
- 不让趋势单独把战略资产降至零仓位。
- 不自动修改战略权重。
- 不在没有历史时点估值数据时伪造估值回测。
- 不用单次回测最优参数直接上线。

## 17. 成功定义

该方案成功，不意味着每次建议都正确，也不意味着一定跑赢静态组合。

成功应表现为：

1. 周报能够在战略仓位附近给出有纪律的双向战术建议。
2. 每条加仓、减仓和持有建议都有完整、可复现的解释链。
3. 数据缺失、风险约束和交易质量能够诚实影响建议。
4. 战术建议不会因轻微信号变化频繁反转。
5. 回测、样本外测试与影子运行表明复杂度带来了可验证价值。
6. 用户始终能区分长期战略配置、临时战术目标和真实当前持仓。

本方案的核心不是“预测下周涨跌”，而是把市场状态转化为有界、可解释、可复盘的双向主动配置决策。
