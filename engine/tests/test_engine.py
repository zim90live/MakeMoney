#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────
# 【回归测试 / REGRESSION TESTS】 保护 engine/ 里"碰钱"的核心逻辑不被改坏。
#
#   本项目由 Claude + Codex 双人维护，signals.py 含大量风险闸门 / 建仓数学。
#   这些测试只覆盖**纯函数**（不触网、不读写真实持仓），可在任何机器秒级运行：
#       python3 engine/tests/test_engine.py        # 直接跑
#       python3 -m unittest discover engine/tests   # 也可
#
#   重点守护的不变量（任何一条被破坏都可能给用户错误的交易信号）：
#     1. 配置非法时必须报错（权重不为 1 / 负现金 / 代码不在池内 …）
#     2. 数据缺失或过旧时 rebalance_allowed 必须为 False（历史上"缺数据→假买入"的根因）
#     3. 首次建仓只用首批比例、受单周上限约束，且只有第 1 周可执行
#     4. 份额按一手(100份)向下取整，买不起一手时为 0
#     5. 估值缺失永远不能被当成"中性"
# ─────────────────────────────────────────────────────────────────────────
import os
import sys
import tempfile
import unittest

# 让测试能 import 到 engine/ 下的模块
ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

import signals  # noqa: E402
import reports  # noqa: E402
import learning  # noqa: E402
import app as webapp  # noqa: E402  (Flask 应用模块；仅用其纯函数)


# ---------- 测试夹具：一份合法的策略 / 组合 ----------

def valid_strategy():
    return {
        "risk_profile": "平衡",
        "risk_controls": {
            "min_trade_amount": 500,
            "max_weekly_trade_amount": 10000,
            "first_tranche_pct": 0.25,
            "allow_trade_with_cache": False,
        },
        "universe": [
            {"code": "511010", "asset": "bond", "index": None, "proxy_index": "sh000012"},
            {"code": "510300", "asset": "equity", "index": "沪深300", "proxy_index": "sh000300"},
            {"code": "518880", "asset": "gold", "index": None, "proxy_index": None},
        ],
        "watchlist": [
            {"code": "511880", "name": "银华日利", "role": "cash_management", "asset": "cash", "index": None},
        ],
        "factors": {
            "trend_filter": {"enabled": True, "ma_days": 200},
            "momentum": {"enabled": True, "lookback_days": 60},
            "valuation": {"enabled": True, "lookback_years": 5, "cheap_pct": 0.30, "rich_pct": 0.70},
            "rebalance": {"enabled": True, "abs_threshold_pp": 5, "rel_threshold": 0.25},
        },
    }


def valid_portfolio():
    return {
        "cash": 30000,
        "holdings": [
            {"code": "511010", "name": "国债ETF", "shares": 0, "target_weight": 0.5},
            {"code": "510300", "name": "沪深300ETF", "shares": 0, "target_weight": 0.3},
            {"code": "518880", "name": "黄金ETF", "shares": 0, "target_weight": 0.2},
        ],
    }


def joined(errs):
    return " | ".join(errs)


# ---------- validate_config：组合配置闸门 ----------

class TestValidateConfig(unittest.TestCase):
    def test_valid_passes(self):
        self.assertEqual(signals.validate_config(valid_portfolio(), valid_strategy()), [])

    def test_weights_must_sum_to_one(self):
        p = valid_portfolio()
        p["holdings"][0]["target_weight"] = 0.9  # 合计变 1.4
        errs = signals.validate_config(p, valid_strategy())
        self.assertTrue(any("合计" in e for e in errs), joined(errs))

    def test_negative_cash_rejected(self):
        p = valid_portfolio()
        p["cash"] = -1
        errs = signals.validate_config(p, valid_strategy())
        self.assertTrue(any("cash" in e for e in errs), joined(errs))

    def test_duplicate_code_rejected(self):
        p = valid_portfolio()
        p["holdings"].append({"code": "511010", "name": "重复", "shares": 0, "target_weight": 0.0})
        errs = signals.validate_config(p, valid_strategy())
        self.assertTrue(any("重复" in e for e in errs), joined(errs))

    def test_code_outside_universe_rejected(self):
        p = valid_portfolio()
        p["holdings"][0]["code"] = "999999"
        errs = signals.validate_config(p, valid_strategy())
        self.assertTrue(any("不在" in e for e in errs), joined(errs))

    def test_negative_shares_rejected(self):
        p = valid_portfolio()
        p["holdings"][0]["shares"] = -5
        errs = signals.validate_config(p, valid_strategy())
        self.assertTrue(any("shares" in e for e in errs), joined(errs))

    def test_weight_out_of_range_rejected(self):
        p = valid_portfolio()
        p["holdings"][0]["target_weight"] = 1.5
        errs = signals.validate_config(p, valid_strategy())
        self.assertTrue(any("target_weight" in e for e in errs), joined(errs))


# ---------- validate_strategy：策略配置闸门 ----------

class TestValidateStrategy(unittest.TestCase):
    def test_valid_passes(self):
        self.assertEqual(signals.validate_strategy(valid_strategy()), [])

    def test_must_have_exactly_one_bond(self):
        s = valid_strategy()
        s["universe"] = [u for u in s["universe"] if u["asset"] != "bond"]
        errs = signals.validate_strategy(s)
        self.assertTrue(any("bond" in e for e in errs), joined(errs))

    def test_two_bonds_rejected(self):
        s = valid_strategy()
        s["universe"].append({"code": "511020", "asset": "bond", "index": None, "proxy_index": None})
        errs = signals.validate_strategy(s)
        self.assertTrue(any("bond" in e for e in errs), joined(errs))

    def test_bad_ma_days_rejected(self):
        s = valid_strategy()
        s["factors"]["trend_filter"]["ma_days"] = 0
        errs = signals.validate_strategy(s)
        self.assertTrue(any("ma_days" in e for e in errs), joined(errs))

    def test_cheap_must_be_below_rich(self):
        s = valid_strategy()
        s["factors"]["valuation"]["cheap_pct"] = 0.8  # > rich 0.7
        errs = signals.validate_strategy(s)
        self.assertTrue(any("cheap_pct" in e for e in errs), joined(errs))

    def test_bad_risk_profile_rejected(self):
        s = valid_strategy()
        s["risk_profile"] = "激进"
        errs = signals.validate_strategy(s)
        self.assertTrue(any("risk_profile" in e for e in errs), joined(errs))

    def test_watchlist_duplicate_rejected(self):
        s = valid_strategy()
        s["watchlist"].append({"code": "511880", "name": "x", "role": "r", "asset": "cash"})
        errs = signals.validate_strategy(s)
        self.assertTrue(any("watchlist" in e and "重复" in e for e in errs), joined(errs))

    def test_watchlist_overlapping_universe_rejected(self):
        s = valid_strategy()
        s["watchlist"].append({"code": "510300", "name": "x", "role": "r", "asset": "equity"})
        errs = signals.validate_strategy(s)
        self.assertTrue(any("watchlist" in e and "universe" in e for e in errs), joined(errs))


# ---------- grade_data：数据质量闸门（历史 bug 的根因） ----------

class TestGradeData(unittest.TestCase):
    def test_missing_blocks_rebalance(self):
        grade, ok, _ = signals.grade_data(["510300"], {})
        self.assertEqual(grade, "部分缺失")
        self.assertFalse(ok, "缺失行情时绝不能允许再平衡")

    def test_stale_blocks_rebalance(self):
        prov = {"510300": {"source": "live", "as_of": "2026-05-01", "stale_days": 30}}
        grade, ok, _ = signals.grade_data([], prov)
        self.assertEqual(grade, "过旧")
        self.assertFalse(ok, "数据过旧时绝不能允许再平衡")

    def test_cache_is_usable_but_flagged(self):
        prov = {"510300": {"source": "cache", "as_of": "2026-06-03", "stale_days": 1}}
        grade, ok, _ = signals.grade_data([], prov)
        self.assertEqual(grade, "缓存可用")
        self.assertTrue(ok)

    def test_clean_is_complete(self):
        prov = {"510300": {"source": "live", "as_of": "2026-06-03", "stale_days": 1}}
        grade, ok, _ = signals.grade_data([], prov)
        self.assertEqual(grade, "完整")
        self.assertTrue(ok)


# ---------- floor_to_lot：份额按一手取整 ----------

class TestFloorToLot(unittest.TestCase):
    def test_rounds_down_to_lot(self):
        # 1万元 / (5元*100份) = 20 手 → 2000 份
        self.assertEqual(signals.floor_to_lot(10000, 5), 2000)

    def test_partial_lot_rounds_down(self):
        # 10500 元只够 21 手 → 2100 份（剩 0 元）；10499 元也是 20 手
        self.assertEqual(signals.floor_to_lot(10499, 5), 2000)

    def test_cannot_afford_one_lot_is_zero(self):
        # 400 元买不起一手(500元) → 0
        self.assertEqual(signals.floor_to_lot(400, 5), 0)

    def test_zero_or_bad_inputs(self):
        self.assertEqual(signals.floor_to_lot(0, 5), 0)
        self.assertEqual(signals.floor_to_lot(1000, 0), 0)
        self.assertEqual(signals.floor_to_lot(-100, 5), 0)


# ---------- build_first_funding_schedule：多周分批建仓 ----------

class TestFirstFundingSchedule(unittest.TestCase):
    def setUp(self):
        self.holdings = valid_portfolio()["holdings"]
        self.prices = {"511010": 100.0, "510300": 4.0, "518880": 6.0}

    def test_schedule_respects_first_pct_and_cap(self):
        sched = signals.build_first_funding_schedule(
            self.holdings, self.prices, cash=30000, first_pct=0.25,
            max_weekly=10000, min_trade=500)
        self.assertTrue(len(sched) >= 1)
        # 首批比例 25% → 单周 7500，受 max_weekly 10000 不再压低
        self.assertEqual(sched[0]["planned_amount"], 7500)

    def test_only_first_week_is_ready(self):
        sched = signals.build_first_funding_schedule(
            self.holdings, self.prices, cash=30000, first_pct=0.25,
            max_weekly=10000, min_trade=500)
        self.assertEqual(sched[0]["status"], "ready")
        for wk in sched[1:]:
            self.assertEqual(wk["status"], "requires_prior_review",
                             "后续周次必须先复盘，不能默认可执行")

    def test_no_cash_no_schedule(self):
        self.assertEqual(
            signals.build_first_funding_schedule(self.holdings, self.prices, 0, 0.25, 10000, 500), [])

    def test_weekly_cap_limits_planned(self):
        # max_weekly=3000 应把单周计划压到 3000
        sched = signals.build_first_funding_schedule(
            self.holdings, self.prices, cash=30000, first_pct=0.25,
            max_weekly=3000, min_trade=500)
        self.assertEqual(sched[0]["planned_amount"], 3000)


# ---------- estimate_target_stress_drawdown：风险预算压力测试 ----------

class TestStressDrawdown(unittest.TestCase):
    def test_known_weights(self):
        holdings = valid_portfolio()["holdings"]  # bond .5 / equity .3 / gold .2
        universe = {
            "511010": {"asset": "bond"},
            "510300": {"asset": "equity"},
            "518880": {"asset": "gold"},
        }
        total, contribs = signals.estimate_target_stress_drawdown(holdings, universe)
        # 0.5*-0.03 + 0.3*-0.30 + 0.2*-0.15 = -0.135 → abs 0.135
        self.assertAlmostEqual(total, 0.135, places=3)
        self.assertEqual(len(contribs), 3)

    def test_equity_dominates_the_drawdown(self):
        holdings = valid_portfolio()["holdings"]
        universe = {
            "511010": {"asset": "bond"},
            "510300": {"asset": "equity"},
            "518880": {"asset": "gold"},
        }
        _, contribs = signals.estimate_target_stress_drawdown(holdings, universe)
        by_code = {c["code"]: c["contribution"] for c in contribs}
        # 权益对回撤的贡献(绝对值)应最大
        self.assertEqual(min(by_code, key=by_code.get), "510300")


# ---------- reports.report_summary：周报摘要提取（纯函数） ----------

class TestReportSummary(unittest.TestCase):
    def test_summary_extracts_core_fields(self):
        signals_blob = {
            "generated_for": "2026-06-04",
            "data_quality": "完整",
            "as_of_summary": "2026-06-03",
            "portfolio_value": 30000.0,
            "rebalance_allowed": True,
        }
        summary = reports.report_summary(signals_blob)
        self.assertEqual(summary.get("generated_for"), "2026-06-04")
        self.assertEqual(summary.get("data_quality"), "完整")
        self.assertEqual(summary.get("as_of_summary"), "2026-06-03")


# ---------- reports.monthly_review：月度复盘聚合（看是否守规则，不看赚亏） ----------

class TestMonthlyReview(unittest.TestCase):
    def _patch(self, reports_list, executions_list):
        self._orig_reports = reports._all_reports
        self._orig_execs = reports.load_executions
        reports._all_reports = lambda: reports_list
        reports.load_executions = lambda: executions_list

    def tearDown(self):
        if hasattr(self, "_orig_reports"):
            reports._all_reports = self._orig_reports
            reports.load_executions = self._orig_execs

    def test_no_executions_is_neutral(self):
        self._patch(
            [{"summary": {"generated_for": "2026-06-03", "data_quality": "完整",
                          "portfolio_value": 30000, "actionable_count": 1, "first_funding_count": 2}}],
            [])
        rows = reports.monthly_review()
        self.assertEqual(len(rows), 1)
        m = rows[0]
        self.assertEqual(m["month"], "2026-06")
        self.assertEqual(m["suggested_actions"], 3)
        self.assertEqual(m["verdict_level"], "none")

    def test_on_plan_execution_is_good(self):
        self._patch(
            [{"summary": {"generated_for": "2026-06-03", "data_quality": "完整", "portfolio_value": 30000}}],
            [{"created_at": "2026-06-04T10:00:00", "items": [
                {"status": "已执行", "code": "510300", "amount": 2000, "fee": 1.5,
                 "suggestion_source": "first_funding"}]}])
        m = reports.monthly_review()[0]
        self.assertEqual(m["executed_total"], 1)
        self.assertEqual(m["off_plan_items"], 0)
        self.assertEqual(m["verdict_level"], "good")
        self.assertAlmostEqual(m["invested_amount"], 2000)
        self.assertAlmostEqual(m["fees_total"], 1.5)

    def test_off_plan_execution_is_flagged(self):
        # 手动补录、无建议来源 = 计划外操作，必须被标记为需注意
        self._patch(
            [{"summary": {"generated_for": "2026-06-03", "data_quality": "完整", "portfolio_value": 30000}}],
            [{"created_at": "2026-06-04T10:00:00", "items": [
                {"status": "已执行", "code": "159915", "amount": 5000, "suggestion_source": ""}]}])
        m = reports.monthly_review()[0]
        self.assertEqual(m["off_plan_items"], 1)
        self.assertEqual(m["verdict_level"], "warn")
        self.assertTrue(any("计划外" in f for f in m["findings"]))

    def test_skip_reasons_aggregated(self):
        self._patch(
            [{"summary": {"generated_for": "2026-06-03", "data_quality": "完整", "portfolio_value": 30000}}],
            [{"created_at": "2026-06-04T10:00:00", "items": [
                {"status": "未执行", "code": "510300", "reason": "等回调"},
                {"status": "未执行", "code": "510500", "reason": "等回调"}]}])
        m = reports.monthly_review()[0]
        self.assertEqual(m["skipped_items"], 2)
        self.assertEqual(m["skip_reasons"][0], {"reason": "等回调", "count": 2})

    def test_deviation_planned_vs_executed(self):
        self._patch(
            [{"summary": {"generated_for": "2026-06-03", "data_quality": "完整", "portfolio_value": 30000},
              "signals": {"actionable_rebalance": [],
                          "first_funding_plan": {"orders": [
                              {"actionable": True, "estimated_amount": 2000},
                              {"actionable": False, "estimated_amount": 500}]}}}],
            [{"created_at": "2026-06-04T10:00:00", "items": [
                {"status": "已执行", "code": "510300", "amount": 1800, "suggestion_source": "first_funding"}]}])
        m = reports.monthly_review()[0]
        self.assertAlmostEqual(m["suggested_amount"], 2000)   # 只算 actionable 的建议
        self.assertAlmostEqual(m["invested_amount"], 1800)
        self.assertAlmostEqual(m["deviation_amount"], -200)

    def test_traded_without_report_flagged(self):
        # 当月有执行但没有任何周报 = 未先看周报就交易
        self._patch(
            [],
            [{"created_at": "2026-06-04T10:00:00", "items": [
                {"status": "已执行", "code": "510300", "amount": 1000, "suggestion_source": "rebalance"}]}])
        m = reports.monthly_review()[0]
        self.assertEqual(m["traded_without_report"], 1)
        self.assertEqual(m["verdict_level"], "warn")


# ---------- learning：观察池解锁逻辑（观察池永不可买入） ----------

class TestLearningUnlock(unittest.TestCase):
    def test_unlock_states(self):
        self.assertEqual(learning._unlock_state(0, False)[0], "learning")
        self.assertEqual(learning._unlock_state(4, False)[0], "need_ack")
        self.assertEqual(learning._unlock_state(4, True)[0], "unlocked")
        self.assertEqual(learning._unlock_state(2, True)[0], "observing")

    def test_custom_threshold(self):
        self.assertEqual(learning._unlock_state(2, True, min_obs=2)[0], "unlocked")


class TestWatchlistLearning(unittest.TestCase):
    def setUp(self):
        self._cards, self._obs, self._acks = learning.load_cards, learning.observed_counts, learning.load_acks
        learning.load_cards = lambda: {"511360": {"tracks": "短融", "risks": ["信用风险"],
                                                    "questions": ["为什么不等于现金？"]}}

    def tearDown(self):
        learning.load_cards, learning.observed_counts, learning.load_acks = self._cards, self._obs, self._acks

    def test_unlocked_when_observed_and_acknowledged(self):
        learning.observed_counts = lambda: {"511360": 5}
        learning.load_acks = lambda: {"511360": {"code": "511360", "acknowledged": True,
                                                  "acknowledged_at": "2026-06-01T00:00:00"}}
        strat = {"watchlist": [{"code": "511360", "name": "短融ETF", "role": "short_bond", "asset": "short_bond"}]}
        items = learning.watchlist_learning(strat)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["unlock_status"], "unlocked")
        self.assertEqual(it["card"]["tracks"], "短融")
        self.assertFalse(it["buyable"], "观察池永远不可直接买入")

    def test_fresh_item_is_learning_and_not_buyable(self):
        learning.observed_counts = lambda: {}
        learning.load_acks = lambda: {}
        strat = {"watchlist": [{"code": "511360", "name": "短融ETF"}]}
        it = learning.watchlist_learning(strat)[0]
        self.assertEqual(it["unlock_status"], "learning")
        self.assertFalse(it["buyable"])


# ---------- ETF 折溢价 / 规模分级（买入前新手保护） ----------

class TestEtfPremiumScale(unittest.TestCase):
    def test_premium_sensitive_thresholds(self):
        # QDII/黄金/货币更敏感：≥1.5% 溢价即 issue（如标普500 +5.14%）
        self.assertEqual(webapp._classify_premium(0.0514, sensitive=True)[0], "issue")
        self.assertEqual(webapp._classify_premium(0.008, sensitive=True)[0], "warn")
        self.assertEqual(webapp._classify_premium(0.002, sensitive=True)[0], "ok")

    def test_premium_normal_thresholds(self):
        self.assertEqual(webapp._classify_premium(0.02, sensitive=False)[0], "warn")
        self.assertEqual(webapp._classify_premium(0.035, sensitive=False)[0], "issue")
        self.assertEqual(webapp._classify_premium(0.003, sensitive=False)[0], "ok")

    def test_premium_discount_side_label(self):
        _, msg = webapp._classify_premium(-0.04, sensitive=False)
        self.assertIn("折价", msg)
        _, msg2 = webapp._classify_premium(0.04, sensitive=False)
        self.assertIn("溢价", msg2)

    def test_premium_none(self):
        self.assertEqual(webapp._classify_premium(None), (None, None))

    def test_scale_thresholds(self):
        self.assertEqual(webapp._classify_scale(3e9)[0], "ok")     # 30 亿
        self.assertEqual(webapp._classify_scale(1.5e8)[0], "warn")  # 1.5 亿
        self.assertEqual(webapp._classify_scale(3e7)[0], "issue")   # 0.3 亿 → 清盘风险
        self.assertEqual(webapp._classify_scale(None), (None, None))

    def test_spot_row_metrics(self):
        import pandas as pd
        snap = pd.DataFrame([
            {"代码": "513500", "最新价": 2.563, "IOPV实时估值": 2.4377,
             "总市值": 25454789547, "流通市值": 25454789547, "成交额": 197217700},
            {"代码": "511880", "最新价": 100.502, "IOPV实时估值": float("nan"),
             "总市值": 85646870133, "流通市值": 85646870133, "成交额": 3777390000},
        ])
        m = webapp._spot_row_metrics(snap, "513500")
        self.assertAlmostEqual(m["premium"], 2.563 / 2.4377 - 1, places=4)
        self.assertEqual(m["market_cap"], 25454789547)
        self.assertEqual(m["turnover"], 197217700)   # 快照近一日成交额，用于历史源失败兜底
        # 货币基金 IOPV 为 NaN → premium 必须为 None（不能当 0 溢价）
        self.assertIsNone(webapp._spot_row_metrics(snap, "511880")["premium"])
        # 不存在的 code / 空快照
        self.assertIsNone(webapp._spot_row_metrics(snap, "999999"))
        self.assertIsNone(webapp._spot_row_metrics(None, "513500"))


# ---------- reports.compute_holdings_draft：成交后持仓草稿（不自动写入） ----------

class TestHoldingsDraft(unittest.TestCase):
    def _port(self):
        return {"cash": 30000, "holdings": [
            {"code": "510300", "name": "沪深300ETF", "shares": 0, "target_weight": 0.5},
            {"code": "511010", "name": "国债ETF", "shares": 100, "target_weight": 0.5}]}

    def test_buy_updates_shares_and_cash(self):
        recs = [{"items": [{"status": "已执行", "code": "510300", "shares": 1000,
                            "amount": 4900, "fee": 2, "side": "buy"}]}]
        d = reports.compute_holdings_draft(self._port(), recs)
        h = {x["code"]: x for x in d["holdings"]}
        self.assertEqual(h["510300"]["new_shares"], 1000)
        self.assertEqual(h["510300"]["delta_shares"], 1000)
        self.assertAlmostEqual(d["cash_new"], 30000 - 4902)
        self.assertTrue(d["changed"])

    def test_sell_updates_shares_and_cash(self):
        recs = [{"items": [{"status": "已执行", "code": "511010", "shares": 100,
                            "amount": 10000, "fee": 1, "side": "sell"}]}]
        d = reports.compute_holdings_draft(self._port(), recs)
        h = {x["code"]: x for x in d["holdings"]}
        self.assertEqual(h["511010"]["new_shares"], 0)
        self.assertAlmostEqual(d["cash_new"], 30000 + 9999)

    def test_skipped_item_ignored(self):
        recs = [{"items": [{"status": "未执行", "code": "510300", "shares": 1000, "amount": 4900}]}]
        d = reports.compute_holdings_draft(self._port(), recs)
        self.assertFalse(d["changed"])
        self.assertEqual(d["applied_items"], 0)

    def test_missing_side_defaults_buy_with_warning(self):
        recs = [{"items": [{"status": "已执行", "code": "510300", "shares": 100, "amount": 490, "fee": 0}]}]
        d = reports.compute_holdings_draft(self._port(), recs)
        h = {x["code"]: x for x in d["holdings"]}
        self.assertEqual(h["510300"]["delta_shares"], 100)   # 默认按买入
        self.assertTrue(any("方向" in w for w in d["warnings"]))


class TestTargetWeightSuggestion(unittest.TestCase):
    def test_suggestion_sums_to_one_and_respects_budget(self):
        sugg = webapp._suggest_target_weights(
            valid_portfolio(),
            valid_strategy(),
            {"target_annual_return": 0.05, "horizon_years": 5,
             "max_acceptable_drawdown": 0.15, "experience_level": "beginner",
             "emergency_cash_kept_outside": 0, "monthly_contribution": 0}
        )
        total = sum(x["suggested_weight"] for x in sugg["items"])
        self.assertAlmostEqual(total, 1.0, places=2)
        self.assertLessEqual(sugg["stress_drawdown"], 0.15)
        self.assertTrue(sugg["reasons"])

    def test_suggestion_keeps_manual_confirmation_warning(self):
        sugg = webapp._suggest_target_weights(valid_portfolio(), valid_strategy(), {})
        self.assertTrue(any("不会自动生效" in w for w in sugg["warnings"]))


class TestExecutionRecordValidation(unittest.TestCase):
    def setUp(self):
        self._orig_dir = reports.EXECUTIONS_DIR
        self._tmp = tempfile.TemporaryDirectory()
        reports.EXECUTIONS_DIR = self._tmp.name

    def tearDown(self):
        reports.EXECUTIONS_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_amount_autofilled_from_price_and_shares(self):
        rec = reports.save_execution_record({"items": [
            {"status": "已执行", "code": "510300", "shares": 300, "price": 4.927, "amount": 0}
        ]})
        self.assertAlmostEqual(rec["items"][0]["amount"], 1478.1)

    def test_amount_mismatch_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            reports.save_execution_record({"items": [
                {"status": "已执行", "code": "510300", "shares": 300, "price": 4.927, "amount": 1490}
            ]})
        self.assertIn("成交金额", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
