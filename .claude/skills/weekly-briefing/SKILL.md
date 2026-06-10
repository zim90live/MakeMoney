---
name: weekly-briefing
description: Generate the weekly ETF investment decision briefing. Runs the shared quant engine (engine/signals.py — trend / momentum / valuation percentile / rebalance) over the ETF pool in portfolio.yaml, layers in AI news & sentiment risk flags, and outputs a confirm-or-reject action list. Use when the user asks for 投资周报, 周报, their weekly briefing, a rebalancing check, or 本周信号.
---

# 周度投资决策简报（量化骨架 + AI 增强）— Claude 入口

> **单一事实源说明（重要）**：本技能的代码**只有一份**，在项目根的 `engine/`。
> 本目录（`.claude/skills/weekly-briefing/`）**只放这份 SKILL.md**，是 Claude 的薄入口；
> Codex 的薄入口在 `.agents/skills/weekly-briefing/SKILL.md`。两者共用 `engine/` 和根目录配置，
> **不存在第二份代码**。回测在 `engine/backtest.py`。

这是一个"决策副驾"：量化脚本算信号，你（Claude）做 AI 增强，最后给用户一份带理由的行动清单。**最终拍板和下单由用户手动完成。**

## 重要原则（每次都遵守）
- 这是**自用私人投顾**工具，输出是带理由的"建议"供所有者本人决策；不承诺收益，回测好 ≠ 未来赚钱。
- 保持"人在环"：AI 旗标只做提示，**绝不替用户自动决策或下单**。
- **不编造数据**。脚本失败或某项数据缺失，就如实说"数据不可用"，绝不猜价格/分位/动量。
- 看 `signals.json` 的 `data_quality`（完整/缓存可用/过旧/部分缺失）与 `rebalance_allowed`：简报里要标注**数据质量**和**行情截至日期(`as_of_summary`)**；**只有 `rebalance_allowed=true` 才给再平衡建议**，为 false 时说明原因（缺行情或过旧）并建议稍后重跑；若 `used_cache=true` 要提示"部分数据来自缓存"。
- 估值：若某 ETF 带 `valuation_missing` 或 `valuation_status.available=false`，简报必须写"估值数据缺失"，**绝不能当成"估值中性"**。
- 再平衡原始信号看 `rebalance[]`；用户可执行动作看 `actionable_rebalance[]` 和 `action_discipline`。若纪律检查拦截，必须写明原因，不得把原始再平衡直接写成交易动作。
- 每只持仓 ETF 的 `actionable_rebalance[].action_reason` 是**后端确定性理由**（加仓/减仓/不动都有，含偏离/趋势/动量/估值三态/拦截/执行质量）；简报直接引用它，AI 只在其上补舆情色彩，**不另写一套理由、不改其数值**。
- 若 `first_funding_plan.is_zero_position=true`，简报加入"首次建仓预览"；它只是试仓预览，不是自动下单。
- `watchlist_signals` 是观察池，只用于学习与监控；**不得用买/卖措辞，不得把观察池写进交易动作**，除非用户明确要求把某个候选纳入持仓池。

## 执行步骤
1. **跑量化骨架**（在项目根运行）：
   ```
   python3 engine/signals.py
   ```
   读取生成的 `engine/signals.json`。脚本会先做配置校验，不通过会直接报错停止。
   若报缺依赖：`pip install -r engine/requirements.txt`。
2. **AI 增强层（结构化风险旗标）**：
   a. 用 web 搜索做一次新闻/政策/舆情扫描。
   b. 把发现整理成**符合 `engine/flags_schema.json` 的旗标**，写入 `engine/flags.json`（结构 `{"generated_for":"...","flags":[ ... ]}`）。每条须含 8 个字段：`category`（固定6类）、`title`、`source`、`date`、`affected_assets`（ETF代码或"ALL"）、`direction`（利好/利空/中性）、`confidence`（高/中/低）、`actionable`（是否足以影响本周动作）。可选 `source_url`：有公开来源链接就填 http(s) 链接（前端可点击查看来源），没有就省略、别编造。
   c. 运行 `python3 engine/validate_flags.py`；**不通过就按提示修正再跑**，校验通过的旗标才能进简报。
   d. 纪律：只记**前瞻性风险**，不要把"对已发生涨跌的事后解释"当旗标；**找不到有据事件就写 `{"flags": []}`**（简报写"本周无重大事件"）；低置信度不得 `actionable=true`。
3. **归档可视化周报数据**：运行
   ```
   python3 engine/reports.py
   ```
   它会把 `engine/signals.json` + `engine/flags.json` 归档到 `reports/<report_id>/report.json`（紧凑 json，不再落盘 `report.md`——前端由 json 重渲染），并把旧的活动决策周期标 `superseded`（同日重跑则覆盖同一份并把旧版存入 `reports/<id>/history/`）。旗标会先过机械校验 + 新鲜度判定（比信号早 >7 天 → 标过旧、不参与拦买）。前端驾驶舱的"历史周报 / 周报详情视图"会读取这份归档渲染可视化报告。简报里必须写出 `report_id`，方便用户在前端找到。
   > ⚠️ 口径差异提示：买入侧的**执行质量闸**（实时折溢价/申购状态/政策旗标裁决）只在网页「生成本周信号」路径里执行；CLI 归档不含这层加工。用户在驾驶舱打开「调仓」时会实时重验同口径，故不会执行到坏单，但建议优先引导用户用网页生成正式周报。
4. **合成简报**：用下面模板，把量化信号（趋势/动量/估值/再平衡）和**校验过的旗标**合在一起，每条建议都给理由。`actionable=true` 的旗标才可影响行动清单。
5. **行动清单 + 落地入口**：列"卖/买/不动"。提醒用户：实际下单在券商 App 手动完成；成交后**在 Web 驾驶舱点「调仓」逐条登记成交**（可逐条跳过/否决并留痕；自动算手续费、单事务更新持仓与现金）。**不要让用户手改 `portfolio.yaml`**——手改会丢执行记录（浮动盈亏变"成本未知"、月度复盘记"未执行"）。

## 简报模板
```
📅 周度决策简报 · <signals.json 的 generated_for>
─────────────────────────────
组合状态：数据【完整/缓存可用/过旧/部分缺失】· 行情截至<as_of_summary>｜总值约 ¥X｜<是否触发再平衡>
可视化归档：reports/<report_id>/report.json（打开 Web 驾驶舱 → 历史周报 → 周报详情视图）
量化信号（骨架）：<各 ETF 趋势/动量/估值分位；权益动量排名>
AI 增强信号（来自校验过的 flags）：每条 [类别·方向·置信度] 标题（来源, 日期, 影响:代码）；无则"本周无重大事件"
观察池（只学习/监控，不触发交易）：<各观察 ETF 趋势/动量/角色/备注>
纪律检查：<是否允许交易；若不允许列出原因；0持仓时列首次建仓预览>
建议动作：<只使用 actionable_rebalance 中 actionable=true 的买/卖/不动 + 金额 + 理由(量化+AI)>
提醒：实际下单在券商 App 手动完成；成交后在 Web 驾驶舱点「调仓」登记（勿手改 portfolio.yaml）。
```

## 文件地图
- `engine/signals.py`、`engine/backtest.py` — 唯一代码实现。
- `portfolio.yaml`（根）— 用户持仓/目标权重/现金，**由驾驶舱「调仓」流程自动维护，勿手改**。
- `strategy.yaml`（根）— ETF 池、因子参数、再平衡阈值，**调策略才动**。
