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
- 这是教育/辅助工具，输出是"建议"，**不构成投资建议**；回测好 ≠ 未来赚钱。
- 保持"人在环"：AI 旗标只做提示，**绝不替用户自动决策或下单**。
- **不编造数据**。脚本失败或某项数据缺失，就如实说"数据不可用"，绝不猜价格/分位/动量。
- 看 `signals.json` 的 `data_quality`（完整/缓存可用/过旧/部分缺失）与 `rebalance_allowed`：简报里要标注**数据质量**和**行情截至日期(`as_of_summary`)**；**只有 `rebalance_allowed=true` 才给再平衡建议**，为 false 时说明原因（缺行情或过旧）并建议稍后重跑；若 `used_cache=true` 要提示"部分数据来自缓存"。
- 估值：若某 ETF 带 `valuation_missing` 或 `valuation_status.available=false`，简报必须写"估值数据缺失"，**绝不能当成"估值中性"**。
- 再平衡原始信号看 `rebalance[]`；用户可执行动作看 `actionable_rebalance[]` 和 `action_discipline`。若纪律检查拦截，必须写明原因，不得把原始再平衡直接写成交易动作。
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
   b. 把发现整理成**符合 `engine/flags_schema.json` 的旗标**，写入 `engine/flags.json`（结构 `{"generated_for":"...","flags":[ ... ]}`）。每条须含 8 个字段：`category`（固定6类）、`title`、`source`、`date`、`affected_assets`（ETF代码或"ALL"）、`direction`（利好/利空/中性）、`confidence`（高/中/低）、`actionable`（是否足以影响本周动作）。
   c. 运行 `python3 engine/validate_flags.py`；**不通过就按提示修正再跑**，校验通过的旗标才能进简报。
   d. 纪律：只记**前瞻性风险**，不要把"对已发生涨跌的事后解释"当旗标；**找不到有据事件就写 `{"flags": []}`**（简报写"本周无重大事件"）；低置信度不得 `actionable=true`。
3. **归档可视化周报数据**：运行
   ```
   python3 engine/reports.py
   ```
   它会把 `engine/signals.json` + `engine/flags.json` 归档到 `reports/<report_id>/report.json` 和 `report.md`。前端驾驶舱的"历史周报 / 周报详情视图"会读取这份归档渲染可视化报告。简报里必须写出 `report_id`，方便用户在前端找到。
4. **合成简报**：用下面模板，把量化信号（趋势/动量/估值/再平衡）和**校验过的旗标**合在一起，每条建议都给理由。`actionable=true` 的旗标才可影响行动清单。
5. **行动清单 + 确认入口**：列"卖/买/不动"，结尾给 `[确认全部] [逐条调整] [全部否决]`。提醒用户成交后更新根目录 `portfolio.yaml` 的 `shares` 与 `cash`，或在 Web 驾驶舱记录执行结果。

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
      [确认全部]   [逐条调整]   [全部否决]
提醒：成交后请更新 portfolio.yaml 的 shares 与 cash。
```

## 文件地图
- `engine/signals.py`、`engine/backtest.py` — 唯一代码实现。
- `portfolio.yaml`（根）— 用户持仓/目标权重/现金，**用户每周改**。
- `strategy.yaml`（根）— ETF 池、因子参数、再平衡阈值，**调策略才动**。
