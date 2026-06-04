# Project Handoff

## Collaboration Rule

This project is jointly maintained by Claude and Codex.

Keep one implementation source of truth:

- Core code lives in `engine/`.
- Claude entrypoint: `.claude/skills/weekly-briefing/SKILL.md`.
- Codex entrypoint: `.agents/skills/weekly-briefing/SKILL.md`.
- The agent skill files are thin wrappers only. Do not copy `signals.py`, `backtest.py`, or app logic into agent folders.

When changing behavior, update the shared `engine/` implementation first, then update README / skill instructions only if the interface changed.

## Current Status

The project is now a local ETF allocation assistant with:

- Weekly signal engine: `engine/signals.py`.
- Backtest engine: `engine/backtest.py`.
- Shared weekly report archive layer: `engine/reports.py`.
- Structured AI risk flags: `engine/flags_schema.json` and `engine/validate_flags.py`.
- Local visual dashboard: `engine/app.py` and `engine/web/index.html`.
- Watchlist / observation pool in `strategy.yaml`.
- Action thresholds / first-funding preview via `risk_controls`.
- Visual weekly report history under `reports/<report_id>/`.
- Manual execution journal under `journal/executions/`.
- Review history archived under `REVIEW/`.
- Example portfolio template: `examples/portfolio.example.yaml`.
- Regression tests (stdlib unittest, no network): `engine/tests/test_engine.py`.
- Monthly review aggregation (rule-adherence, not P&L): `reports.monthly_review()` + `GET /api/review/monthly` + dashboard "月度复盘" panel.
- Risk stress breakdown by asset class shown in the dashboard "亏损压力测试" box (reads `risk_budget.stress_contributions`).
- Watchlist learning system: `engine/learning.py` + `engine/learning_cards.yaml` + `GET /api/watchlist/learning` + `POST /api/watchlist/learning/ack`. Unlock = observed >= 4 weekly reports AND learning acknowledged; "unlocked" only means *discuss promotion*, never buyable. Acks persist to `journal/learning/` (gitignored).
- AI risk flags now render in full in the dashboard via a shared `renderFlags()` (direction color / confidence / affected assets / actionable badge / source + date). `flags_schema.json` gained an optional `source_url`; `validate_flags.py` enforces http(s); frontend only links http(s) (javascript: is stripped).
- ETF quality (`GET /api/etf/quality` + `_etf_quality_for`) now also computes premium/discount and fund scale from `ak.fund_etf_spot_em()` (IOPV-based, premium = price/IOPV - 1). QDII / gold / money assets use a lower premium threshold (>=1.5% = issue, "don't buy now"). Snapshot is process-cached for 120s; when unavailable it is honestly marked unknown, never fabricated. Pure helpers `_classify_premium` / `_classify_scale` / `_spot_row_metrics` are unit-tested.

The tool is still an education / decision-support system. It does not place trades and must not be described as guaranteed investment advice.

## Data And Files

Personal / generated files:

- `portfolio.yaml` is personal account state and is ignored by git.
- `engine/signals.json` is generated and ignored by git.
- `engine/flags.json` is generated and ignored by git.
- `engine/cache/` is live market / valuation cache and ignored by git.
- `reports/` contains generated visual weekly report archives and is ignored by git.
- `journal/` contains manual execution records and is ignored by git.

Reproducibility files:

- `engine/data/` contains backtest seed data and should be kept for offline reproducibility.
- `engine/data/meta.json` records data source, adjustment status, date range, and row counts.

System files:

- `.DS_Store`, `__pycache__/`, and `*.pyc` are ignored.

## Verified Commands

Syntax check:

```bash
python3 -m py_compile engine/signals.py engine/backtest.py engine/validate_flags.py engine/reports.py engine/app.py engine/learning.py
```

Run regression tests (fast, offline):

```bash
python3 engine/tests/test_engine.py
```

Generate weekly signals:

```bash
python3 engine/signals.py
```

Run backtest:

```bash
python3 engine/backtest.py
```

Initialize empty AI risk flags when there is no major weekly event:

```bash
python3 engine/validate_flags.py --init-empty
python3 engine/validate_flags.py
```

Archive a weekly report for visual dashboard rendering:

```bash
python3 engine/reports.py
```

This reads `engine/signals.json` and `engine/flags.json`, then writes:

```text
reports/<report_id>/report.json
reports/<report_id>/report.md
```

Run local dashboard:

```bash
python3 engine/app.py
```

Launcher files:

```bash
./start_mac.command
start_windows.bat
```

If port `5057` is occupied:

```bash
PORT=5058 python3 engine/app.py
PORT=5058 ./start_mac.command
```

## Latest Local Verification

As of 2026-06-03:

- `engine/signals.py` runs successfully on Windows with UTF-8 console output handling.
- Latest signal data is as of `2026-06-03`.
- Data quality is `完整`.
- Valuation cache works for `510300` and `510500`.
- `watchlist_signals` is emitted for observation-only ETFs. It must not drive trade actions.
- `action_discipline`, `actionable_rebalance`, and `first_funding_plan` are emitted by `engine/signals.py`.
- `engine/validate_flags.py` runs successfully on Windows with UTF-8 console output handling.
- `engine/reports.py` creates visual report archives that the dashboard can read.
- `engine/backtest.py` runs successfully.
- The local web API was verified:
  - `GET /api/config` works.
  - `POST /api/signals` works and archives a report.
  - `GET /api/reports` and `GET /api/reports/<report_id>` work.
  - `GET /api/market/kpis` returns ETF curve/KPI data and execution markers.
  - `GET /api/executions` and `POST /api/executions` work.
  - `POST /api/backtest` works.

The current `portfolio.yaml` was created from the zero-position template but has test cash entered:

- `cash: 30000`
- all ETF `shares: 0`

Live rebalance amounts are still only a first-funding preview until real account cash and shares are confirmed.

## Weekly Report Archive Flow

Both Web and agent-triggered weekly briefings now use the same archive path:

1. Run `engine/signals.py` to write `engine/signals.json`.
2. Write or initialize `engine/flags.json`.
3. Run `engine/validate_flags.py`.
4. Run `engine/reports.py`.
5. Open the Web dashboard and use `历史周报` / `周报详情视图` to render the archived report.

Important:

- `/周报` skill instructions now explicitly require `python3 engine/reports.py`.
- The final chat briefing should mention the `report_id`.
- `reports/<report_id>/report.json` is the visual dashboard source of truth for that week.
- `report.md` is a text fallback and human-readable archive.

## Watchlist

Observation pool lives in `strategy.yaml` under `watchlist`.

Current candidates:

- `511880` 银华日利: cash management.
- `511990` 华宝添益: cash management.
- `511360` 短融ETF: cash enhancement / short bond.
- `513500` 标普500ETF: global equity core candidate.
- `513100` 纳指ETF: global growth satellite candidate.
- `159915` 创业板ETF: China growth satellite.
- `588000` 科创50ETF: China growth satellite.

Rules:

- Watchlist is for learning and monitoring only.
- Watchlist does not affect portfolio weights.
- Watchlist does not trigger rebalance.
- Weekly reports should have a separate observation section.
- Do not use buy/sell wording for watchlist unless the user explicitly asks to promote a candidate into the holdings universe.

## Operating Rhythm

Recommended rhythm:

- Daily: run `python3 engine/signals.py` only as a data health / cache / observation check.
- Weekly: run formal decision briefing and consider portfolio actions.
- Monthly or quarterly: review strategy parameters and ETF pool.

Daily runs should not imply daily trading. The project is intentionally low-frequency.

## Action Thresholds

`strategy.yaml` contains `risk_controls`:

- `min_trade_amount`: ignore tiny actions.
- `max_weekly_trade_amount`: cap weekly deployment / adjustment.
- `first_tranche_pct`: for zero-position accounts, deploy only a fraction of cash first.
- `allow_trade_with_cache`: when false, cached live data blocks executable trade actions.

Rules:

- Preserve raw `rebalance`; it describes signal-level deviation.
- Use `actionable_rebalance` for user-facing executable actions.
- Use `first_funding_plan` for zero-position onboarding.
- The web UI may show previews, but it must not write trades or update shares automatically.
- Watchlist never participates in first funding or rebalance.

## Backtest Findings

ETF tradable segment, about 2020-02-05 to 2026-06-02:

- Trend-filter strategy: about `+4.2%` annualized, max drawdown about `-10.2%`.
- Static allocation without trend filter: about `+5.9%` annualized, max drawdown about `-16.0%`.
- `510300` buy-and-hold: about `+4.4%` annualized, max drawdown about `-45.1%`.

Long proxy index segment, about 2006-01-16 to 2026-06-02:

- Static proxy allocation: about `+7.9%` annualized, max drawdown about `-42.2%`.
- Trend-filter proxy strategy: about `+8.3%` annualized, max drawdown about `-23.8%`.
- CSI 300 proxy buy-and-hold: about `+8.7%` annualized, max drawdown about `-72.3%`.

Interpretation:

- Trend filtering should be framed as crisis insurance, not as a normal-market return enhancer.
- Current `risk_profile` is `平衡`, so live weekly signals should treat trend as a display / risk flag, not an automatic allocation switch.

## Web Dashboard Notes

Single self-contained `engine/web/index.html` (inline CSS + vanilla JS + ECharts CDN, no build step). Layout was reorganized from ~18 stacked panels into:

- An always-on "decision zone" (status chips + decision guide + goal coach + 本周信号) that answers "what do I do this week" without navigation.
- A tab bar for everything else: 行情与质量 / 我的组合 / 复盘与历史 / 观察池 / 回测. Tabs are switched by `activateTab(name)`; the markets tab is lazy (loads on first activation via `loadMarketsTab`), backtest loads on button.
- 行情与质量 = per-ETF unified cards: one combined loader fetches `/api/market/kpis` + `/api/etf/quality` with the SAME `?codes=` (server defaults to holdings when omitted) and joins by `code`; curves render first, the slow premium/quality block patches in (two-phase, `Promise.allSettled`).
- 复盘与历史 = report master-detail (list + single `#reportDetailPanel`, merging the old `openReport` summary and `renderReportViz`).
- 工作台说明 → floating help FAB (`#helpFab` → `#helpPanel`), which also hosts the 术语速查 glossary.
- 术语 (趋势/动量/估值/回撤/再平衡/最长水下/折溢价/规模/MA200) render as inline hover tooltips via `glossary(term)` (CSS `.term .tip`).
- 数据源健康 folded into the status-chip "数据详情" popover (`#healthPanel`).

Coupling notes for future edits:

- `renderGoalCoach` now writes via stable ids (`#gcTargetReturn/#gcPrincipal/#gcYearGoal/#gcLoss5/10/15/#gcGoalHint/#gcLossHint`), NOT positional `querySelectorAll(...)[i]` — keep it that way.
- ECharts instances are registered in the `ECHARTS[]` array via `initChart()`; `activateTab` calls `resizeCharts()` so charts created in a hidden tab resize correctly when shown.
- `static_folder=None` and only `/` is served; the page stays one file (no extra static routes needed). A local preview config lives in `.claude/launch.json` (port 5090).

It still: edits cash/shares/weights/`risk_profile`; calls `engine/signals.py` + `engine/backtest.py`; implements no independent investment logic; archives reports via `engine/reports.py`; marks executions on ETF charts; saves execution records to `journal/executions/`.

Current limitations:

- It rewrites `portfolio.yaml` in a compact generated format, so manual comments in that file will be lost after saving from the UI.
- ECharts is currently loaded from CDN; if offline, the frontend falls back to a simpler canvas chart.
- Execution records are manual notes only; they do not update `portfolio.yaml` or place trades.
- It does not yet implement risk budgets.
- It does not yet generate broker-ready trade tickets.

## Investment Boundary

The project currently covers ETF allocation only. This is intentional.

Do not add individual-stock recommendations unless the user explicitly asks to change project scope. If individual stocks are ever added, they should start as watchlist / risk-monitoring only, not buy/sell recommendations.

For user-facing portfolio suggestions:

- State assumptions clearly.
- Avoid promising returns.
- Prefer staged small entries over all-in deployment.
- Use ETF-only allocations.
- Make clear that final order placement is manual and user-controlled.

## Recommended Next Steps

Priority 1: connect real account state.

- Enter the real cash amount in `portfolio.yaml` or through the web dashboard.
- Since the account currently has zero ETF holdings, test with a small first tranche rather than deploying the full intended portfolio at once.
- Run `python3 engine/signals.py` after updating cash.

Priority 2: add action thresholds.

- Action thresholds already exist in `strategy.yaml`.
- Next step is to add user-facing trade-ticket detail and better explanation of blocked actions.

Priority 3: add a weekly journal.

- `journal/executions/` now exists for manual execution records.
- Next step is to connect execution records to portfolio update reminders and later performance review.

Priority 4: improve dashboard.

- Add a first-funding trade-ticket assistant for zero-position accounts.
- Add richer risk flag display and source links.
- Add clearer warnings when valuation is rich or missing.
- Add a simple daily data-health view if needed.
- Consider vendoring `echarts.min.js` under `engine/web/vendor/` for offline use.

## Open Decisions

- Whether `engine/data/` should be committed as seed data in the eventual git repo. Current recommendation: yes, for offline reproducibility.
- Whether the handoff file should remain `HANDOFF.md` or be renamed if the user prefers a different spelling.
- What small initial funding amount the user wants to use in the Shenwan Hongyuan account.
