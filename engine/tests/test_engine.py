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
import backtest  # noqa: E402  (仅用其纯函数：分批建仓模拟)
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


def expanded_strategy():
    """9 只可交易池：A股宽基 + 防御 + 黄金 + 债 + 全球(标普/纳指) + A股成长(创业板/科创50)。"""
    s = valid_strategy()
    s["universe"] = [
        {"code": "511010", "asset": "bond", "index": None, "proxy_index": "sh000012"},
        {"code": "510300", "asset": "equity", "index": "沪深300", "proxy_index": "sh000300"},
        {"code": "512890", "asset": "equity_defensive", "index": None, "proxy_index": "sh000300"},
        {"code": "510500", "asset": "equity", "index": "中证500", "proxy_index": "sh000905"},
        {"code": "518880", "asset": "gold", "index": None, "proxy_index": None},
        {"code": "513500", "name": "标普500ETF", "asset": "global_equity", "index": None, "proxy_index": None},
        {"code": "513100", "name": "纳指ETF", "asset": "global_growth", "index": None, "proxy_index": None},
        {"code": "159915", "name": "创业板ETF", "asset": "china_growth", "index": None, "proxy_index": "sz399006"},
        {"code": "588000", "name": "科创50ETF", "asset": "china_growth", "index": None, "proxy_index": "sh000688"},
    ]
    return s


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

    def test_cushion_allows_more_equity_for_growth_goal(self):
        """P0-3：场外稳健垫让 ETF 桶可为 8% 目标加更多股，但全组合压力回撤仍 ≤ 预算。"""
        strat, port = expanded_strategy(), {"cash": 30000, "holdings": []}
        base = {"target_annual_return": 0.08, "horizon_years": 5,
                "max_acceptable_drawdown": 0.20, "experience_level": "intermediate"}
        with_c = webapp._suggest_target_weights(port, strat, {**base, "stable_assets_outside": 700000, "planned_etf_capital": 1000000})
        without_c = webapp._suggest_target_weights(port, strat, {**base, "stable_assets_outside": 0, "planned_etf_capital": 0})
        # 安全垫 → 建议权益更高
        self.assertGreater(with_c["suggested_equity_total"], without_c["suggested_equity_total"])
        # 全组合压力回撤仍在 20% 预算内
        self.assertLessEqual(with_c["whole_portfolio_stress_drawdown"], 0.20 + 1e-9)
        # 新增全球/成长品种拿到正权重，且合计 ≈ 1
        wmap = {i["code"]: i["suggested_weight"] for i in with_c["items"]}
        for code in ("513500", "513100", "159915", "588000"):
            self.assertGreater(wmap[code], 0, f"{code} 应有正权重")
        self.assertAlmostEqual(sum(wmap.values()), 1.0, places=2)

    def test_unmet_high_target_is_flagged_not_promised(self):
        """8% 在该菜单下达不到时，必须如实说明、不承诺。"""
        strat, port = expanded_strategy(), {"cash": 30000, "holdings": []}
        sugg = webapp._suggest_target_weights(port, strat, {
            "target_annual_return": 0.08, "max_acceptable_drawdown": 0.20,
            "experience_level": "intermediate", "horizon_years": 5,
            "stable_assets_outside": 700000, "planned_etf_capital": 1000000})
        self.assertLess(sugg["expected_etf_return"], 0.08)
        self.assertTrue(any("非承诺" in r for r in sugg["reasons"]))

    def test_weights_sum_to_one_even_when_bond_zeroed(self):
        """高目标 + advanced 把权益顶到上限、债券归零时，残差不得让合计变成 1.01。"""
        sugg = webapp._suggest_target_weights({"cash": 30000, "holdings": []}, expanded_strategy(), {
            "target_annual_return": 0.10, "max_acceptable_drawdown": 0.20,
            "experience_level": "advanced", "horizon_years": 5,
            "stable_assets_outside": 700000, "planned_etf_capital": 1000000})
        total = sum(i["suggested_weight"] for i in sugg["items"])
        self.assertAlmostEqual(total, 1.0, places=6)


class TestWholePortfolioStress(unittest.TestCase):
    """P0-2：把 ETF 桶压力回撤折算到全组合（稳健桶是 0 冲击的安全垫）。"""

    def test_cushion_scales_down_drawdown(self):
        # ETF 桶 30%，ETF 100 万 + 稳健 70 万 → 全组合 ≈ 17.6%
        self.assertAlmostEqual(signals.whole_portfolio_stress(0.30, 1_000_000, 700_000),
                               0.30 * 1_000_000 / 1_700_000, places=6)

    def test_no_cushion_is_etf_basis(self):
        self.assertAlmostEqual(signals.whole_portfolio_stress(0.30, 1_000_000, 0), 0.30, places=6)

    def test_zero_portfolio_falls_back(self):
        self.assertEqual(signals.whole_portfolio_stress(0.30, 0, 0), 0.30)


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


class TestExpectedEtfReturn(unittest.TestCase):
    """P1-2：目标可行性体检——ETF 桶现实预期年化（按 sleeve 假设加权，非承诺）。"""

    def test_weighted_sum_uses_asset_assumptions(self):
        uni = {"511010": {"asset": "bond"}, "510300": {"asset": "equity"}, "513100": {"asset": "global_growth"}}
        holdings = [{"code": "511010", "target_weight": 0.5},
                    {"code": "510300", "target_weight": 0.3},
                    {"code": "513100", "target_weight": 0.2}]
        want = (0.5 * signals.ASSET_EXPECTED_RETURN["bond"]
                + 0.3 * signals.ASSET_EXPECTED_RETURN["equity"]
                + 0.2 * signals.ASSET_EXPECTED_RETURN["global_growth"])
        self.assertAlmostEqual(signals.expected_etf_return(holdings, uni), want, places=6)

    def test_unknown_asset_uses_default(self):
        exp = signals.expected_etf_return([{"code": "x", "target_weight": 1.0}], {"x": {"asset": "weird"}})
        self.assertAlmostEqual(exp, signals.DEFAULT_EXPECTED_RETURN, places=6)


class TestDcaBacktest(unittest.TestCase):
    """P1-1：分批/定投建仓模拟（纯函数，无网络）。"""

    def test_lumpsum_beats_dca_in_rising_market(self):
        arr = [1.0 * (1.001 ** i) for i in range(300)]   # 单调上行
        dates = [str(i) for i in range(300)]
        lump, lump_dd, _ = backtest._dca_sim(arr, dates, 0, 200, 1, 21, 1.0)
        dca, dca_dd, _ = backtest._dca_sim(arr, dates, 0, 200, 6, 21, 1.0)
        self.assertGreater(lump, dca)                    # 上行市一次性更优（更早满仓）
        self.assertAlmostEqual(lump, arr[200] / arr[0], places=6)
        self.assertEqual(lump_dd, 0.0)                   # 单调上行无回撤
        self.assertEqual(dca_dd, 0.0)

    def test_value_path_emitted_when_requested(self):
        arr = [1.0 + 0.001 * i for i in range(120)]
        dates = [str(i) for i in range(120)]
        _, _, path = backtest._dca_sim(arr, dates, 0, 100, 12, 21, 1.0, want_path=True)
        self.assertTrue(path and all("date" in p and "value" in p for p in path))

    def test_median(self):
        self.assertEqual(backtest._median([3, 1, 2]), 2)
        self.assertEqual(backtest._median([1, 2, 3, 4]), 2.5)
        self.assertEqual(backtest._median([]), 0.0)


class TestWestockFallback(unittest.TestCase):
    """westock(腾讯自选股) ETF 质量兜底的纯函数（无网络）。"""

    def test_symbol_market_prefix(self):
        self.assertEqual(webapp._westock_symbol("510300"), "sh510300")
        self.assertEqual(webapp._westock_symbol("588000"), "sh588000")
        self.assertEqual(webapp._westock_symbol("159915"), "sz159915")

    def test_parse_etf_picks_detail_table(self):
        md = (
            "#### sh513500\n"
            "| code | name | closePrice | nav | totalMV | turnoverValue | purchaseStatus | establishDate |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| sh513500 | 标普500ETF | 2.57 | 2.60 | 9500000000 | 366198544 | 不可申购 | 2013-12-05 00:00:00 |\n"
            "\n**持仓明细**\n| code | name | ratio |\n| --- | --- | --- |\n| usAAPL | 苹果 | 7.0 |\n"
        )
        row = webapp._parse_westock_etf(md)
        self.assertIsNotNone(row)
        self.assertEqual(row["code"], "sh513500")          # 取首个明细表，而非持仓表
        self.assertEqual(row["purchaseStatus"], "不可申购")
        self.assertEqual(row["totalMV"], "9500000000")

    def test_parse_etf_none_on_garbage(self):
        self.assertIsNone(webapp._parse_westock_etf(""))
        self.assertIsNone(webapp._parse_westock_etf("没有表格"))

    def test_purchase_status_note(self):
        self.assertEqual(webapp._purchase_status_note("不可申购", sensitive=True)[0], "issue")
        self.assertEqual(webapp._purchase_status_note("暂停申购", sensitive=False)[0], "warn")
        self.assertEqual(webapp._purchase_status_note("可申购", True), (None, None))
        self.assertEqual(webapp._purchase_status_note(None, True), (None, None))


class TestWestockKline(unittest.TestCase):
    """westock(腾讯自选股) 行情兜底源的纯函数（无网络）。"""

    def test_parse_kline_uses_last_as_close(self):
        md = (
            "| date | open | last | high | low | volume | amount | exchange |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| 2026-06-04 | 4.92 | 4.93 | 4.96 | 4.91 | 6097283 | 3006706301 | 2.17 |\n"
            "| 2026-06-03 | 4.94 | 4.97 | 5.02 | 4.93 | 7604619 | 3784500000 | 2.69 |\n"
        )
        df = signals._parse_westock_kline(md)
        self.assertIsNotNone(df)
        self.assertEqual(list(df.columns), ["date", "close", "amount"])      # 现保留成交额列
        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 4.93, places=4)  # 升序末行=06-04，close=last
        self.assertAlmostEqual(float(df["amount"].iloc[-1]), 3006706301, places=0)  # amount 取成交额列

    def test_parse_kline_none_on_garbage(self):
        self.assertIsNone(signals._parse_westock_kline(""))
        self.assertIsNone(signals._parse_westock_kline("没有表格"))

    def test_symbol_prefix(self):
        self.assertEqual(signals._westock_symbol("510300"), "sh510300")
        self.assertEqual(signals._westock_symbol("159915"), "sz159915")

    def test_parse_kline_batch_groups_by_symbol(self):
        md = (
            "[Batch] 状态: success | 总数: 2\n\n"
            "| symbol | date | open | last | high | low | volume | amount | exchange |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| sh510300 | 2026-06-04 | 4.92 | 4.93 | 4.96 | 4.91 | 1 | 1 | 2.17 |\n"
            "| sh510300 | 2026-06-03 | 4.94 | 4.97 | 5.02 | 4.93 | 1 | 1 | 2.71 |\n"
            "| sz159915 | 2026-06-04 | 4.07 | 4.10 | 4.13 | 4.07 | 1 | 1 | 10.5 |\n"
        )
        out = signals._parse_westock_kline_batch(md)
        self.assertEqual(set(out), {"510300", "159915"})            # 去市场前缀后按裸代码分组
        self.assertEqual(list(out["510300"].columns), ["date", "close", "amount"])
        self.assertEqual(len(out["510300"]), 2)
        self.assertAlmostEqual(float(out["510300"]["close"].iloc[-1]), 4.93, places=4)  # 升序末行=06-04
        self.assertAlmostEqual(float(out["510300"]["amount"].iloc[-1]), 1, places=0)

    def test_parse_kline_batch_empty(self):
        self.assertEqual(signals._parse_westock_kline_batch(""), {})
        self.assertEqual(signals._parse_westock_kline_batch("没有表格"), {})

    def test_parse_kline_batch_without_amount_backcompat(self):
        md = (
            "| symbol | date | last |\n"
            "| --- | --- | --- |\n"
            "| sh510300 | 2026-06-04 | 4.93 |\n"
        )
        out = signals._parse_westock_kline_batch(md)
        self.assertEqual(list(out["510300"].columns), ["date", "close"])  # 无 amount 列时不强加


class TestWestockEtfBatch(unittest.TestCase):
    """westock 批量 etf 详情解析 + 行→指标（无网络）。"""

    BATCH_MD = (
        "[Batch] 状态: success | 总数: 2 | 成功: 2 | 失败: 0\n\n"
        "| code | name | closePrice | nav | totalMV | turnoverValue | purchaseStatus | establishDate |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| sh510300 | 沪深300ETF | 4.95 | 4.94 | 120000000000 | 1080000000 | 可申购 | 2012-05-28 00:00:00 |\n"
        "| sh513500 | 标普500ETF | 2.57 | 2.45 | 9500000000 | 366000000 | 不可申购 | 2013-12-05 00:00:00 |\n"
    )

    def test_batch_groups_by_bare_code(self):
        out = webapp._parse_westock_etf_batch(self.BATCH_MD)
        self.assertEqual(set(out), {"510300", "513500"})            # 去前缀按裸代码
        self.assertEqual(out["513500"]["purchaseStatus"], "不可申购")

    def test_batch_empty_on_garbage(self):
        self.assertEqual(webapp._parse_westock_etf_batch(""), {})
        self.assertEqual(webapp._parse_westock_etf_batch("没有表格"), {})

    def test_row_to_metrics_premium(self):
        row = webapp._parse_westock_etf_batch(self.BATCH_MD)["513500"]
        m = webapp._etf_row_to_metrics(row)
        self.assertAlmostEqual(m["premium"], 2.57 / 2.45 - 1, places=4)   # 溢价 = close/nav - 1
        self.assertEqual(m["market_cap"], 9500000000.0)
        self.assertEqual(m["purchase_status"], "不可申购")

    def test_row_to_metrics_none(self):
        self.assertIsNone(webapp._etf_row_to_metrics(None))

    def test_years_since_from_establish_date(self):
        self.assertIsNone(webapp._years_since(None))
        self.assertIsNone(webapp._years_since(""))
        self.assertIsNone(webapp._years_since("乱码"))
        self.assertGreater(webapp._years_since("2012-05-28 00:00:00"), 10)   # 成立日→距今年限


class TestQualityMetricsWestockFirst(unittest.TestCase):
    """_quality_metrics 应 westock 优先、akshare 快照兜底（无网络，打桩）。"""

    def test_westock_first_then_akshare_fallback(self):
        orig_ws, orig_spot = webapp._westock_etf_metrics, webapp._spot_row_metrics
        try:
            # westock 给折溢价/成交额、但缺规模；akshare 快照补规模
            webapp._westock_etf_metrics = lambda code, max_age=300: {
                "premium": 0.01, "market_cap": None, "turnover": 5e8,
                "purchase_status": "可申购", "last_price": 4.95, "iopv": 4.90}
            webapp._spot_row_metrics = lambda snap, code: {
                "premium": -0.02, "market_cap": 1.2e11, "turnover": 9e8,
                "price": 4.96, "iopv": 4.91}
            m, extra = webapp._quality_metrics("510300", snap=object(), sensitive=False)
            self.assertAlmostEqual(m["premium"], 0.01)         # 折溢价取 westock（不取 akshare 的 -0.02）
            self.assertEqual(extra["premium_source"], "westock")
            self.assertEqual(m["market_cap"], 1.2e11)          # 规模 westock 缺→取 akshare
            self.assertEqual(extra["scale_source"], "akshare")
            self.assertTrue(extra["fallback"])
            self.assertAlmostEqual(m["turnover"], 5e8)         # 成交额 westock 优先
        finally:
            webapp._westock_etf_metrics, webapp._spot_row_metrics = orig_ws, orig_spot

    def test_akshare_only_when_westock_unavailable(self):
        orig_ws, orig_spot = webapp._westock_etf_metrics, webapp._spot_row_metrics
        try:
            webapp._westock_etf_metrics = lambda code, max_age=300: None   # westock 挂了
            webapp._spot_row_metrics = lambda snap, code: {
                "premium": -0.02, "market_cap": 1.2e11, "turnover": 9e8, "price": 4.96, "iopv": 4.91}
            m, extra = webapp._quality_metrics("510300", snap=object(), sensitive=False)
            self.assertAlmostEqual(m["premium"], -0.02)
            self.assertEqual(extra["premium_source"], "akshare")
            self.assertTrue(extra["fallback"])
        finally:
            webapp._westock_etf_metrics, webapp._spot_row_metrics = orig_ws, orig_spot


if __name__ == "__main__":
    unittest.main(verbosity=2)
