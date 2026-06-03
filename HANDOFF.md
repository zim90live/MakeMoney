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
python3 -m py_compile engine/signals.py engine/backtest.py engine/validate_flags.py engine/reports.py engine/app.py
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

The dashboard is now a modular visual cockpit:

- It edits cash, ETF shares, target weights, and `risk_profile`.
- It calls `engine/signals.py` and `engine/backtest.py`.
- It does not implement independent investment logic.
- It archives weekly reports via `engine/reports.py`.
- It renders historical weekly reports with a visual detail view.
- It shows ETF return curves and KPI cards using ECharts when available.
- It marks manual execution records on ETF charts.
- It can save manual execution records to `journal/executions/`.

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
