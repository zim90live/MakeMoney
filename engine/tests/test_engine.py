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
from unittest import mock

# 让测试能 import 到 engine/ 下的模块
ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

import signals  # noqa: E402
import reports  # noqa: E402
import learning  # noqa: E402
import backtest  # noqa: E402  (仅用其纯函数：分批建仓模拟)
import tactical  # noqa: E402  (双向战术配置纯函数；Phase A 影子)
import strategic  # noqa: E402  (Track C 战略层纯函数：费率解析 + §8.2 硬准入)
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

    def test_return_haircut_out_of_range_rejected(self):
        # 批3：return_haircut 须在 [0,0.15]
        s = valid_strategy()
        s["assumptions"] = {"defaults": {"return_haircut": 0.30}}
        errs = signals.validate_strategy(s)
        self.assertTrue(any("return_haircut" in e for e in errs), joined(errs))

    def test_conservative_above_optimistic_rejected(self):
        # 批3：断言 conservative ≤ central ≤ optimistic
        s = valid_strategy()
        s["assumptions"] = {"sleeves": {"equity": {"expected_return": 0.07,
                            "return_conservative": 0.12, "return_optimistic": 0.02}}}
        errs = signals.validate_strategy(s)
        self.assertTrue(any("conservative" in e for e in errs), joined(errs))

    def test_valid_assumption_intervals_pass(self):
        s = valid_strategy()
        s["assumptions"] = {"defaults": {"return_haircut": 0.03},
                            "sleeves": {"equity": {"expected_return": 0.07,
                                        "return_conservative": 0.04, "return_optimistic": 0.10}}}
        self.assertEqual(signals.validate_strategy(s), [])


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


# ---------- §0C #1 多情景历史压力（纯函数 + 标定）----------
class TestStressScenarios(unittest.TestCase):
    UNI = {
        "511010": {"asset": "bond"},
        "510300": {"asset": "equity"},
        "518880": {"asset": "gold"},
    }

    def _holdings(self):
        return valid_portfolio()["holdings"]  # bond .5 / equity .3 / gold .2

    def test_worst_scenario_is_selected(self):
        scen = [
            {"name": "重", "shocks": {"equity": -0.5, "bond": 0.05, "gold": 0.1}},
            {"name": "轻", "shocks": {"equity": -0.1, "bond": 0.0, "gold": 0.0}},
        ]
        results, worst = signals.estimate_stress_scenarios(self._holdings(), self.UNI, scen)
        self.assertEqual(worst["name"], "重")
        # 列表按回撤降序
        self.assertEqual([r["name"] for r in results], ["重", "轻"])
        # 重: 0.5*0.05 + 0.3*-0.5 + 0.2*0.1 = -0.105 → 回撤 0.105
        self.assertAlmostEqual(worst["etf_drawdown"], 0.105, places=3)

    def test_hedge_assets_net_against_loss(self):
        # 同情景内 债/金 的正冲击应抵掉一部分权益损失 → 净回撤 < 权益单腿损失 0.15
        scen = [{"name": "危机", "shocks": {"equity": -0.5, "bond": 0.05, "gold": 0.1}}]
        _, worst = signals.estimate_stress_scenarios(self._holdings(), self.UNI, scen)
        self.assertLess(worst["etf_drawdown"], 0.15)

    def test_positive_net_floors_at_zero(self):
        scen = [{"name": "全涨", "shocks": {"equity": 0.1, "bond": 0.05, "gold": 0.1}}]
        _, worst = signals.estimate_stress_scenarios(self._holdings(), self.UNI, scen)
        self.assertEqual(worst["etf_drawdown"], 0.0)

    def test_unknown_asset_uses_default_shock(self):
        scen = [{"name": "空", "shocks": {}}]
        _, worst = signals.estimate_stress_scenarios(self._holdings(), self.UNI, scen, default_shock=-0.2)
        self.assertAlmostEqual(worst["etf_drawdown"], 0.2, places=3)  # 全部退默认 → Σw×-0.2 = -0.2

    def test_builtin_historical_scenarios_are_severe(self):
        # 内置标定档必须比"示意档"更接近真实尾部：2008 权益冲击应 < -0.5（历史约 -71%）
        by_name = {s["name"]: s for s in signals.HISTORICAL_CRISIS_SCENARIOS}
        self.assertIn("2008金融危机", by_name)
        self.assertLess(by_name["2008金融危机"]["shocks"]["equity"], -0.5)
        # 债券在 A 股危机里应为正（避险），体现真实分散
        self.assertGreater(by_name["2008金融危机"]["shocks"]["bond"], 0)

    def test_load_historical_scenarios_override(self):
        strat = {"historical_stress_scenarios": [{"name": "自定义", "shocks": {"equity": -0.9}}]}
        out = signals.load_historical_scenarios(strat)
        self.assertEqual(out[0]["name"], "自定义")
        # 缺省回退到内置档
        self.assertTrue(len(signals.load_historical_scenarios({})) >= 5)

    def test_calibration_from_seed_panel(self):
        # 据 committed 种子离线标定（不触网）；缺种子则跳过
        scs = backtest.compute_crisis_scenarios(refresh=False)
        if not scs:
            self.skipTest("无价格代理种子")
        by_name = {s["name"]: s for s in scs}
        self.assertIn("2008金融危机", by_name)
        self.assertLess(by_name["2008金融危机"]["shocks"]["equity"], -0.5)


# ---------- §0C #2 真 walk-forward + 证据台账 ----------
class TestWalkForwardEvidence(unittest.TestCase):
    def _real(self):
        import yaml
        root = os.path.dirname(ENGINE_DIR)
        with open(os.path.join(root, "strategy.yaml"), encoding="utf-8") as f:
            strat = yaml.safe_load(f)
        with open(os.path.join(root, "portfolio.yaml"), encoding="utf-8") as f:
            port = yaml.safe_load(f)
        return strat, port, root

    def test_evidence_ledger_structure_and_tiers(self):
        strat, port, root = self._real()
        led = backtest.build_evidence_ledger(strat, port, root, with_walk_forward=False)
        self.assertTrue(led["claims"])
        for c in led["claims"]:
            self.assertIn(c["tier"], backtest.EVIDENCE_TIER_ORDER)
            # 维度2 护栏：每条主张都必须带依据 + 局限，不许裸结论
            self.assertTrue(c["claim"] and c["basis"] and c["caveat"])
        # 现阶段不该有任何主张被标成 live（实盘档须 §0C #6 记账积累）
        self.assertFalse(any(c["tier"] == "live" for c in led["claims"]))

    def test_walk_forward_has_no_lookahead(self):
        strat, port, root = self._real()
        wf = backtest.walk_forward_strategic(strat, port, root)
        if not wf:
            self.skipTest("无长面板 / 无 strategic_policy")
        for f in wf["folds"]:
            # 训练截止必须严格早于测试起点 → 无前视
            self.assertLess(f["train_end"], f["test"][0])
            self.assertTrue(f["rows"])
        self.assertIn(wf["summary"]["verdict"],
                      ("样本外仍倾向简化", "样本外不支持简化（构建更优）"))


# ---------- §0C #3 协方差进 construct 接受判定 ----------
class TestCovarianceAcceptGate(unittest.TestCase):
    def _construct(self, policy_overrides=None):
        import yaml
        root = os.path.dirname(ENGINE_DIR)
        with open(os.path.join(root, "strategy.yaml"), encoding="utf-8") as f:
            strat = yaml.safe_load(f)
        with open(os.path.join(root, "portfolio.yaml"), encoding="utf-8") as f:
            port = yaml.safe_load(f)
        sp = strat.get("strategic_policy") or {}
        if not sp.get("roles"):
            return None
        if policy_overrides:
            import copy
            sp = copy.deepcopy(sp)
            sp.setdefault("caps", {}).update(policy_overrides)
        prof = signals.load_investor_profile(root)
        asm = signals.load_assumptions(strat)
        scen = signals.load_stress_scenarios(strat)
        asset_of = {str(u["code"]): u.get("asset") for u in strat.get("universe", [])}
        exposure_of = {str(u["code"]): u.get("exposure_id") or u.get("index") or u.get("proxy_index") or str(u["code"])
                       for u in strat.get("universe", [])}
        stable = float(strategic.employment_resilience(prof)["risk_buffer_available"])
        planned = float(prof.get("planned_etf_capital", 0) or 0)
        etf_share = planned / (planned + stable) if planned + stable > 0 else 1.0
        current = {str(h["code"]): float(h.get("target_weight") or 0) for h in port.get("holdings", [])}
        full = backtest.build_full_panel(strat, current)
        if full is None:
            return None
        pxL = full[0]
        wk = pxL.resample("W").last().pct_change().dropna()
        cr = {c: wk[backtest.FULL_PROXY[c]].tolist() for c in asset_of if backtest.FULL_PROXY.get(c) in wk.columns}
        cov = strategic.shrinkage_covariance(cr)
        return strategic.construct_strategic_portfolio(
            sp, returns=asm["returns"], shocks=asm["shocks"], target_return=float(prof.get("target_annual_return", 0.05)),
            default_return=asm["default_return"], default_shock=asm["default_shock"], asset_of=asset_of,
            etf_share=etf_share, max_whole_stress=float(prof.get("max_acceptable_drawdown", 0.15)),
            returns_conservative=asm["returns_conservative"], scenarios=scen, exposure_of=exposure_of,
            covariance=cov, incumbent_codes=current)

    def test_covariance_metrics_exposed(self):
        snap = self._construct()
        if snap is None:
            self.skipTest("无 policy / 面板不可得")
        m = snap["metrics"]
        # 协方差隐含压力、覆盖率、有效风险源都必须被披露（此前 vol 不出 snapshot）
        self.assertIsNotNone(m.get("covariance_vol"))
        self.assertIsNotNone(m.get("covariance_stress"))
        self.assertGreater(m.get("covariance_covered_weight"), 0.5)   # 多数权重有协方差覆盖
        self.assertLessEqual(m.get("covariance_covered_weight"), 1.0)

    def test_default_gates_off_does_not_disrupt(self):
        snap = self._construct()
        if snap is None:
            self.skipTest("无 policy / 面板不可得")
        self.assertEqual(snap["validation_status"], "passed")        # 缺省不闸 → 现状不被打断

    def test_min_effective_bets_floor_binds(self):
        snap = self._construct({"min_effective_bets": 10.0})         # 不可能的分散度 → 必须无解
        if snap is None:
            self.skipTest("无 policy / 面板不可得")
        self.assertEqual(snap["validation_status"], "no_feasible_portfolio")

    def test_enforce_cov_stress_not_a_dead_end(self):
        snap = self._construct({"enforce_cov_stress": True})         # 真实相关压力 < 预算 → 仍可行（不制造死胡同）
        if snap is None:
            self.skipTest("无 policy / 面板不可得")
        self.assertEqual(snap["validation_status"], "passed")


# ---------- §0C #5 真夏普（减无风险利率）----------
class TestSharpeRatio(unittest.TestCase):
    def test_subtracts_risk_free(self):
        # (0.10 − 0.02) / 0.20 = 0.40
        self.assertAlmostEqual(backtest.sharpe_ratio(0.10, 0.20), 0.40, places=4)

    def test_old_naked_ratio_was_higher(self):
        # 旧口径(裸 cagr/vol，rf=0)=0.50 > 真夏普 0.40：证明此前系统性高估
        naked = backtest.sharpe_ratio(0.10, 0.20, rf=0.0)
        true_sharpe = backtest.sharpe_ratio(0.10, 0.20)
        self.assertGreater(naked, true_sharpe)
        self.assertAlmostEqual(naked - true_sharpe, 0.10, places=4)   # 高估量 = rf/vol = 0.02/0.20

    def test_zero_vol_is_nan(self):
        self.assertNotEqual(backtest.sharpe_ratio(0.10, 0.0), backtest.sharpe_ratio(0.10, 0.0))  # nan != nan

    def test_default_rf_is_two_percent(self):
        self.assertAlmostEqual(backtest.RISK_FREE_RATE, 0.02, places=6)


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


class TestDecisionCycle(unittest.TestCase):
    def setUp(self):
        self._orig_decisions_dir = reports.DECISIONS_DIR
        self._tmp = tempfile.TemporaryDirectory()
        reports.DECISIONS_DIR = self._tmp.name

    def tearDown(self):
        reports.DECISIONS_DIR = self._orig_decisions_dir
        self._tmp.cleanup()

    def _report(self):
        return {
            "id": "2026-06-05_120000",
            "cycle_status": "active",
            "signals": {
                "actionable_rebalance": [
                    {"actionable": True, "code": "510300", "name": "沪深300ETF",
                     "suggest": "add", "approx_amount": 2000},
                    {"actionable": True, "code": "511010", "name": "国债ETF",
                     "suggest": "trim", "approx_amount": 1000},
                ],
                "first_funding_plan": {"orders": []},
            },
        }

    def test_cycle_suggestions_excludes_completed_actions(self):
        executions = [{"report_id": "2026-06-05_120000", "items": [
            {"status": "已执行", "code": "510300", "side": "buy"},
        ]}]
        rows = reports.cycle_suggestions(self._report(), executions)
        self.assertEqual([r["code"] for r in rows], ["511010"])
        self.assertEqual(rows[0]["cycle_id"], "2026-06-05_120000")
        self.assertEqual(rows[0]["action_status"], "pending")

    def test_cycle_suggestions_does_not_match_other_cycle(self):
        executions = [{"report_id": "older", "items": [
            {"status": "已执行", "code": "510300", "side": "buy"},
        ]}]
        rows = reports.cycle_suggestions(self._report(), executions)
        self.assertEqual(len(rows), 2)

    def test_skipped_suggestion_is_persisted_and_excluded(self):
        reports.save_cycle_decision("2026-06-05_120000", "rebalance", "510300", "buy",
                                    "skipped", "等待下周现金到账")
        rows = reports.cycle_suggestions(self._report(), [])
        self.assertEqual([r["code"] for r in rows], ["511010"])
        all_rows = reports.cycle_suggestions(self._report(), [], include_completed=True)
        skipped = next(r for r in all_rows if r["code"] == "510300")
        self.assertEqual(skipped["action_status"], "skipped")
        self.assertEqual(skipped["decision_reason"], "等待下周现金到账")

    def test_pending_restores_skipped_suggestion(self):
        reports.save_cycle_decision("2026-06-05_120000", "rebalance", "510300", "buy", "rejected", "不认同")
        reports.save_cycle_decision("2026-06-05_120000", "rebalance", "510300", "buy", "pending")
        self.assertEqual(len(reports.cycle_suggestions(self._report(), [])), 2)

    def test_config_version_status_detects_change(self):
        orig = reports.CONFIG_PATHS
        try:
            paths = {}
            for name in orig:
                path = os.path.join(self._tmp.name, name + ".yaml")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("value: 1\n")
                paths[name] = path
            reports.CONFIG_PATHS = paths
            expected = reports.config_versions()
            self.assertEqual(reports.cycle_version_status({"config_versions": expected})["status"], "current")
            with open(paths["portfolio_version"], "w", encoding="utf-8") as f:
                f.write("value: 2\n")
            status = reports.cycle_version_status({"config_versions": expected})
            self.assertEqual(status["status"], "stale")
            self.assertEqual(status["changed"], ["portfolio_version"])
        finally:
            reports.CONFIG_PATHS = orig

    def test_formal_review_reports_keep_latest_per_day(self):
        rows = reports._formal_reports_for_review([
            {"id": "a", "created_at": "2026-06-05T09:00:00", "summary": {"generated_for": "2026-06-05"}},
            {"id": "b", "created_at": "2026-06-05T12:00:00", "summary": {"generated_for": "2026-06-05"}},
            {"id": "c", "created_at": "2026-06-06T09:00:00", "summary": {"generated_for": "2026-06-06"}},
        ])
        self.assertEqual([r["id"] for r in rows], ["b", "c"])


# ---------- reports.archive_report：周报按自然日归档（同日覆盖，不堆秒级目录） ----------

class TestReportArchival(unittest.TestCase):
    def setUp(self):
        self._orig = (reports.REPORTS_DIR, reports.DECISIONS_DIR, reports.NAV_DIR)
        self._tmp = tempfile.TemporaryDirectory()
        reports.REPORTS_DIR = os.path.join(self._tmp.name, "reports")
        reports.DECISIONS_DIR = os.path.join(self._tmp.name, "decisions")
        reports.NAV_DIR = os.path.join(self._tmp.name, "nav")

    def tearDown(self):
        reports.REPORTS_DIR, reports.DECISIONS_DIR, reports.NAV_DIR = self._orig
        self._tmp.cleanup()

    def _sig(self):
        return {"generated_for": "2026-06-08", "data_quality": "完整",
                "actionable_rebalance": [], "first_funding_plan": {"orders": []},
                "portfolio_value": 1000, "cash": 0, "holdings": []}

    def test_report_id_is_natural_day(self):
        # 周报 id 精确到自然日（YYYY-MM-DD），不含秒；执行记录另用 _now_id() 到秒，互不影响
        rid = reports._report_day_id()
        self.assertRegex(rid, r"^\d{4}-\d{2}-\d{2}$")
        self.assertNotIn("_", rid)
        self.assertRegex(reports._now_id(), r"^\d{4}-\d{2}-\d{2}_\d{6}$")  # 成交仍到秒

    def test_same_day_refresh_overwrites_single_report(self):
        r1 = reports.archive_report(signals=self._sig())
        r2 = reports.archive_report(signals=self._sig())   # 同一天第二次刷新
        self.assertEqual(r1["id"], r2["id"])               # 复用同一自然日 id
        self.assertEqual(len(os.listdir(reports.REPORTS_DIR)), 1)  # 磁盘只剩一份
        self.assertEqual(r2["cycle_status"], "active")     # 仍是活动周期（未自我 supersede）
        self.assertGreaterEqual(r2["created_at"], r1["created_at"])

    def test_cross_day_supersedes_prior_active(self):
        # 预置一份更早的活动周期 → 新日归档应把它标 superseded、指向新 id
        prior = {"id": "2020-01-01", "cycle_status": "active",
                 "superseded_at": None, "superseded_by": None, "signals": {}}
        reports._write_report(prior)
        new = reports.archive_report(signals=self._sig())
        reloaded = reports.load_json(reports._report_path("2020-01-01"))
        self.assertEqual(reloaded["cycle_status"], "superseded")
        self.assertEqual(reloaded["superseded_by"], new["id"])


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

    def test_repeated_same_day_reports_count_once(self):
        self._patch(
            [
                {"id": "a", "created_at": "2026-06-03T09:00:00",
                 "summary": {"generated_for": "2026-06-03", "data_quality": "完整",
                             "portfolio_value": 30000, "actionable_count": 9}},
                {"id": "b", "created_at": "2026-06-03T12:00:00",
                 "summary": {"generated_for": "2026-06-03", "data_quality": "完整",
                             "portfolio_value": 31000, "actionable_count": 2}},
            ],
            [])
        m = reports.monthly_review()[0]
        self.assertEqual(m["reports"], 1)
        self.assertEqual(m["suggested_actions"], 2)


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
    def test_model_allocation_preserves_shares_and_adds_new_instrument(self):
        port = {"cash": 123, "holdings": [
            {"code": "OLD", "name": "Old", "shares": 7, "target_weight": 1.0},
        ]}
        strat = {"universe": [{"code": "OLD", "name": "Old"}, {"code": "NEW", "name": "New"}]}
        out = webapp._portfolio_with_target_allocation(port, strat, {"OLD": 0.4, "NEW": 0.6})
        by_code = {h["code"]: h for h in out["holdings"]}
        self.assertEqual(out["cash"], 123)
        self.assertEqual(by_code["OLD"]["shares"], 7)
        self.assertEqual(by_code["NEW"]["shares"], 0)
        self.assertEqual(by_code["NEW"]["name"], "New")
        self.assertAlmostEqual(sum(h["target_weight"] for h in out["holdings"]), 1.0)

    def test_total_assets_derives_reserve_and_etf_cap(self):
        out = webapp._profile_with_derived_funding({
            "total_assets": 1_700_000,
            "unemployment_monthly_expense": 6000,
            "unemployment_minimum_monthly_income": 0,
            "unemployment_runway_years": 5,
            "post_stress_reserve_months": 12,
            "stable_assets_outside": 123,
            "planned_etf_capital": 456,
        })
        self.assertEqual(out["stable_assets_outside"], 432000)
        self.assertEqual(out["planned_etf_capital"], 1268000)

    def test_config_save_never_auto_applies_target_weights(self):
        # 批 2（§0B 阻断项 #3）：保存设置只持久化 profile/risk/portfolio，绝不自动改 target_weight，也不跑 construct
        body = {
            "cash": 100, "risk_profile": "平衡",
            "holdings": [{"code": "OLD", "name": "Old", "shares": 7, "target_weight": 1.0}],
            "investor_profile": dict(webapp.DEFAULT_INVESTOR_PROFILE),
        }
        strat = {"risk_profile": "平衡", "universe": [{"code": "OLD", "name": "Old"}]}
        with mock.patch.object(webapp, "load_investor_profile", return_value=dict(webapp.DEFAULT_INVESTOR_PROFILE)), \
                mock.patch.object(webapp, "load_yaml", return_value=strat), \
                mock.patch.object(webapp, "validate_strategy", return_value=[]), \
                mock.patch.object(webapp, "validate_config", return_value=[]), \
                mock.patch.object(webapp, "validate_investor_profile", return_value=[]), \
                mock.patch.object(webapp, "_write_investor_profile"), \
                mock.patch.object(webapp, "_set_risk_profile"), \
                mock.patch.object(webapp, "_write_portfolio") as write_port, \
                mock.patch.object(webapp, "_run_construct") as run_construct, \
                mock.patch.object(webapp, "_apply_constructed_allocation") as apply_alloc:
            response = webapp.app.test_client().post("/api/config", json=body)
        self.assertEqual(response.status_code, 200)
        upd = response.get_json()["strategic_update"]
        self.assertFalse(upd["applied"])
        self.assertTrue(upd["manual_apply_required"])
        write_port.assert_called_once()       # 只写一次持仓（保留提交的 target_weight）
        run_construct.assert_not_called()      # 不再在保存路径跑 construct
        apply_alloc.assert_not_called()        # 也绝不自动应用目标权重

    def test_config_save_writes_submitted_target_weights_unchanged(self):
        # 写出的持仓 target_weight 即用户提交值（保存不改权重）
        body = {
            "cash": 0, "risk_profile": "平衡",
            "holdings": [{"code": "A", "name": "A", "shares": 1, "target_weight": 0.4},
                         {"code": "B", "name": "B", "shares": 1, "target_weight": 0.6}],
            "investor_profile": dict(webapp.DEFAULT_INVESTOR_PROFILE),
        }
        strat = {"risk_profile": "平衡", "universe": [{"code": "A", "name": "A"}, {"code": "B", "name": "B"}]}
        with mock.patch.object(webapp, "load_investor_profile", return_value=dict(webapp.DEFAULT_INVESTOR_PROFILE)), \
                mock.patch.object(webapp, "load_yaml", return_value=strat), \
                mock.patch.object(webapp, "validate_strategy", return_value=[]), \
                mock.patch.object(webapp, "validate_config", return_value=[]), \
                mock.patch.object(webapp, "validate_investor_profile", return_value=[]), \
                mock.patch.object(webapp, "_write_investor_profile"), \
                mock.patch.object(webapp, "_set_risk_profile"), \
                mock.patch.object(webapp, "_write_portfolio") as write_port:
            response = webapp.app.test_client().post("/api/config", json=body)
        self.assertEqual(response.status_code, 200)
        written = write_port.call_args[0][0]   # _write_portfolio(port) 的位置参数
        tw = {h["code"]: h["target_weight"] for h in written["holdings"]}
        self.assertEqual(tw, {"A": 0.4, "B": 0.6})

    def test_adjust_cash_add(self):
        with mock.patch.object(webapp, "load_yaml", return_value={"cash": 1000.0, "holdings": []}), \
                mock.patch.object(webapp, "_write_portfolio") as wp, \
                mock.patch.object(webapp, "save_cash_flow", return_value={"id": "x"}):
            r = webapp.app.test_client().post("/api/portfolio/cash", json={"action": "add", "amount": 500})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["cash"], 1500.0)
        self.assertEqual(wp.call_args[0][0]["cash"], 1500.0)   # 现金写入、持仓不动

    def test_adjust_cash_withdraw(self):
        with mock.patch.object(webapp, "load_yaml", return_value={"cash": 1000.0, "holdings": []}), \
                mock.patch.object(webapp, "_write_portfolio"), \
                mock.patch.object(webapp, "save_cash_flow", return_value={"id": "x"}):
            r = webapp.app.test_client().post("/api/portfolio/cash", json={"action": "withdraw", "amount": 300})
        self.assertEqual(r.get_json()["cash"], 700.0)

    def test_adjust_cash_over_withdraw_rejected(self):
        with mock.patch.object(webapp, "load_yaml", return_value={"cash": 100.0, "holdings": []}), \
                mock.patch.object(webapp, "_write_portfolio") as wp:
            r = webapp.app.test_client().post("/api/portfolio/cash", json={"action": "withdraw", "amount": 500})
        self.assertEqual(r.status_code, 400)
        wp.assert_not_called()                                 # 超额提取被拒、不写盘

    def test_adjust_cash_invalid_amount_rejected(self):
        with mock.patch.object(webapp, "_write_portfolio") as wp:
            r = webapp.app.test_client().post("/api/portfolio/cash", json={"action": "add", "amount": -5})
        self.assertEqual(r.status_code, 400)
        wp.assert_not_called()

    def test_save_and_load_cash_flow(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(reports, "CASHFLOWS_DIR", d):
            rec = reports.save_cash_flow("add", 500, 1000, 1500, "测试注入")
            rows = reports.load_cash_flows()
        self.assertEqual(rec["action"], "add")
        self.assertEqual(rows[0]["cash_after"], 1500.0)
        self.assertEqual(rows[0]["note"], "测试注入")

    def test_strategic_apply_endpoint_writes_passed_construct(self):
        strat = {"strategic_policy": {"roles": {"x": {}}}, "universe": [{"code": "OLD", "name": "Old"}]}
        port = {"cash": 100, "holdings": [{"code": "OLD", "name": "Old", "shares": 7, "target_weight": 1.0}]}
        with mock.patch.object(webapp, "load_investor_profile", return_value=dict(webapp.DEFAULT_INVESTOR_PROFILE)), \
                mock.patch.object(webapp, "load_yaml", side_effect=[strat, port]), \
                mock.patch.object(webapp, "validate_config", return_value=[]), \
                mock.patch.object(webapp, "_write_portfolio") as write_port, \
                mock.patch.object(webapp, "_run_construct", return_value=(
                    {"validation_status": "passed", "instrument_allocation": {"OLD": 1.0}}, "fp")):
            response = webapp.app.test_client().post("/api/strategic/apply", json={"input_fingerprint": "fp"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        write_port.assert_called_once()

    def test_strategic_apply_endpoint_rejects_fingerprint_mismatch(self):
        # §8.2 阻断项 #4：客户端回显的指纹与服务端重算不一致 → 409，不写盘
        strat = {"strategic_policy": {"roles": {"x": {}}}, "universe": [{"code": "OLD", "name": "Old"}]}
        port = {"cash": 100, "holdings": [{"code": "OLD", "name": "Old", "shares": 7, "target_weight": 1.0}]}
        with mock.patch.object(webapp, "load_investor_profile", return_value=dict(webapp.DEFAULT_INVESTOR_PROFILE)), \
                mock.patch.object(webapp, "load_yaml", side_effect=[strat, port]), \
                mock.patch.object(webapp, "_write_portfolio") as write_port, \
                mock.patch.object(webapp, "_run_construct", return_value=(
                    {"validation_status": "passed", "instrument_allocation": {"OLD": 1.0}}, "fp-new")):
            response = webapp.app.test_client().post("/api/strategic/apply", json={"input_fingerprint": "fp-old"})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json().get("stale"))
        write_port.assert_not_called()

    def test_strategic_apply_endpoint_requires_large_move_confirmation(self):
        # §small_capital_guardrails #2：单产品跳变 >15pp 未确认 → 409 needs_confirmation，不写盘
        strat = {"strategic_policy": {"roles": {"x": {}}}, "universe": [{"code": "A", "name": "A"}, {"code": "B", "name": "B"}]}
        port = {"cash": 0, "holdings": [{"code": "A", "name": "A", "shares": 1, "target_weight": 0.07},
                                        {"code": "B", "name": "B", "shares": 1, "target_weight": 0.93}]}
        snap = {"validation_status": "passed", "instrument_allocation": {"A": 0.30, "B": 0.70}}
        with mock.patch.object(webapp, "load_investor_profile", return_value=dict(webapp.DEFAULT_INVESTOR_PROFILE)), \
                mock.patch.object(webapp, "load_yaml", side_effect=[strat, port]), \
                mock.patch.object(webapp, "_write_portfolio") as write_port, \
                mock.patch.object(webapp, "_run_construct", return_value=(snap, "fp")):
            response = webapp.app.test_client().post("/api/strategic/apply", json={"input_fingerprint": "fp"})
        body = response.get_json()
        self.assertEqual(response.status_code, 409)
        self.assertTrue(body.get("needs_confirmation"))
        self.assertEqual(body["large_moves"][0]["code"], "A")
        write_port.assert_not_called()

    def test_strategic_apply_endpoint_applies_after_confirmation(self):
        strat = {"strategic_policy": {"roles": {"x": {}}}, "universe": [{"code": "A", "name": "A"}, {"code": "B", "name": "B"}]}
        port = {"cash": 0, "holdings": [{"code": "A", "name": "A", "shares": 1, "target_weight": 0.07},
                                        {"code": "B", "name": "B", "shares": 1, "target_weight": 0.93}]}
        snap = {"validation_status": "passed", "instrument_allocation": {"A": 0.30, "B": 0.70}}
        with mock.patch.object(webapp, "load_investor_profile", return_value=dict(webapp.DEFAULT_INVESTOR_PROFILE)), \
                mock.patch.object(webapp, "load_yaml", side_effect=[strat, port]), \
                mock.patch.object(webapp, "validate_config", return_value=[]), \
                mock.patch.object(webapp, "_write_portfolio") as write_port, \
                mock.patch.object(webapp, "_run_construct", return_value=(snap, "fp")):
            response = webapp.app.test_client().post(
                "/api/strategic/apply", json={"input_fingerprint": "fp", "confirm_large_moves": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        write_port.assert_called_once()

    def test_strategic_apply_endpoint_missing_fingerprint(self):
        strat = {"strategic_policy": {"roles": {"x": {}}}, "universe": [{"code": "OLD", "name": "Old"}]}
        port = {"cash": 100, "holdings": [{"code": "OLD", "name": "Old", "shares": 7, "target_weight": 1.0}]}
        with mock.patch.object(webapp, "load_investor_profile", return_value=dict(webapp.DEFAULT_INVESTOR_PROFILE)), \
                mock.patch.object(webapp, "load_yaml", side_effect=[strat, port]), \
                mock.patch.object(webapp, "_write_portfolio") as write_port, \
                mock.patch.object(webapp, "_run_construct", return_value=(
                    {"validation_status": "passed", "instrument_allocation": {"OLD": 1.0}}, "fp")):
            response = webapp.app.test_client().post("/api/strategic/apply", json={})
        self.assertEqual(response.status_code, 400)
        write_port.assert_not_called()

    def test_run_construct_blocks_when_quality_missing(self):
        # §8.2 阻断项 #1：质量缓存 missing → quality_gate.blocked，validation 不为 passed
        strat = {"strategic_policy": {"roles": {
                    "core": {"tier": "core", "members": ["A1"], "range": [0.40, 0.60]},
                    "bond": {"tier": "core_defensive", "members": ["B1"], "range": [0.40, 0.60]}}},
                 "universe": [{"code": "A1", "asset": "equity"}, {"code": "B1", "asset": "bond"}]}
        prof = dict(webapp.DEFAULT_INVESTOR_PROFILE)
        port = {"holdings": [{"code": "A1", "target_weight": 0.5}, {"code": "B1", "target_weight": 0.5}]}
        with mock.patch.object(webapp, "_load_strategic_quality_cache", return_value=({}, "missing")), \
                mock.patch.object(webapp, "load_yaml", return_value=port):
            snap, _fp = webapp._run_construct(strat, prof)
        self.assertTrue(snap["quality_gate"]["blocked"])
        self.assertEqual(snap["quality_gate"]["status"], "missing")
        self.assertEqual(snap["product_quality_status"], "missing")
        self.assertNotEqual(snap["validation_status"], "passed")

    def test_construct_stress_budget_decoupled(self):
        # 批3：construct_stress_budget 显式设值 → 构建用它（而非展示 max_dd）作硬约束；null → 默认=展示回撤
        roles = {"core": {"tier": "core", "members": ["A1"], "range": [0.40, 0.60]},
                 "bond": {"tier": "core_defensive", "members": ["B1"], "range": [0.40, 0.60]}}
        prof = dict(webapp.DEFAULT_INVESTOR_PROFILE)
        port = {"holdings": [{"code": "A1", "target_weight": 0.5}, {"code": "B1", "target_weight": 0.5}]}
        fresh = ({"A1": {"admission": {"admitted": True}, "score": {"total": 0.9, "coverage": 1.0}},
                  "B1": {"admission": {"admitted": True}, "score": {"total": 0.9, "coverage": 1.0}}}, "cached")

        def run(budget):
            strat = {"strategic_policy": {"roles": roles, "construct_stress_budget": budget},
                     "universe": [{"code": "A1", "asset": "equity", "exposure_id": "a"},
                                  {"code": "B1", "asset": "bond", "exposure_id": "b"}]}
            with mock.patch.object(webapp, "_load_strategic_quality_cache", return_value=fresh), \
                    mock.patch.object(webapp, "load_yaml", return_value=port):
                return webapp._run_construct(strat, prof)[0]

        tight = run(0.01)
        self.assertEqual(tight["construct_stress_budget"], 0.01)
        self.assertNotEqual(tight["construct_stress_budget"], tight["display_max_drawdown"])   # 解耦
        self.assertEqual(tight["validation_status"], "no_feasible_portfolio")                  # 小预算真作硬约束
        loose = run(None)
        self.assertEqual(loose["construct_stress_budget"], loose["display_max_drawdown"])      # null → 默认=展示回撤

    def test_quality_status_endpoint(self):
        # 战略流程步骤条第②步状态探针：缺失→未新鲜；有真实准入判定→新鲜；数据取不到→不新鲜
        strat = {"strategic_policy": {"roles": {"core": {"members": ["A1", "A2"]}}}}

        def q(cache):
            with mock.patch.object(webapp, "load_yaml", return_value=strat), \
                    mock.patch.object(webapp, "_load_strategic_quality_cache", return_value=cache):
                return webapp.app.test_client().get("/api/strategic/quality-status").json

        r = q(({}, "missing"))
        self.assertEqual(r["status"], "missing")
        self.assertFalse(r["fresh"])
        self.assertEqual(sorted(r["missing"]), ["A1", "A2"])
        # 缓存新鲜 + 有真实准入判定 → fresh
        ok = ({"A1": {"admission": {"admitted": True}}, "A2": {"admission": {"admitted": True}}}, "cached")
        r = q(ok)
        self.assertTrue(r["fresh"])
        self.assertTrue(r["data_ok"])
        self.assertEqual(r["covered_count"], 2)
        # 缓存新鲜但数据取不到（admitted None、无真实判定）→ 不 fresh
        gap = ({"A1": {"admission": {"admitted": None}}, "A2": {"admission": {"admitted": None}}}, "cached")
        r = q(gap)
        self.assertFalse(r["fresh"])
        self.assertFalse(r["data_ok"])

    def test_is_trading_session(self):
        import datetime as _dt
        # 工作日盘中 → True；午休/盘后/周末 → False
        self.assertTrue(webapp._is_trading_session(_dt.datetime(2026, 6, 8 - 3, 10, 0)))   # 周四 10:00（6/5）
        self.assertTrue(webapp._is_trading_session(_dt.datetime(2026, 6, 5, 14, 30)))      # 周五 14:30
        self.assertFalse(webapp._is_trading_session(_dt.datetime(2026, 6, 5, 12, 0)))      # 周五 午休
        self.assertFalse(webapp._is_trading_session(_dt.datetime(2026, 6, 5, 16, 0)))      # 周五 盘后
        self.assertFalse(webapp._is_trading_session(_dt.datetime(2026, 6, 7, 10, 0)))      # 周日

    def test_large_target_moves_threshold(self):
        moves = webapp._large_target_moves({"A": 0.10, "B": 0.50, "C": 0.40, "D": 0.20},
                                           {"A": 0.30, "B": 0.55, "C": 0.15, "D": 0.35})
        codes = {m["code"] for m in moves}
        self.assertIn("A", codes)      # +20pp
        self.assertIn("C", codes)      # -25pp
        self.assertIn("D", codes)      # 恰好 +15pp 也触发（边界含，真金从严）
        self.assertNotIn("B", codes)   # +5pp 不超阈值

    def test_strategic_apply_endpoint_rejects_unpassed_construct(self):
        strat = {"strategic_policy": {"roles": {"x": {}}}, "universe": [{"code": "OLD", "name": "Old"}]}
        port = {"cash": 100, "holdings": [{"code": "OLD", "name": "Old", "shares": 7, "target_weight": 1.0}]}
        snap = {"validation_status": "no_feasible_portfolio", "instrument_allocation": {},
                "constraint_diagnostics": ["no feasible"]}
        with mock.patch.object(webapp, "load_investor_profile", return_value=dict(webapp.DEFAULT_INVESTOR_PROFILE)), \
                mock.patch.object(webapp, "load_yaml", side_effect=[strat, port]), \
                mock.patch.object(webapp, "_write_portfolio") as write_port, \
                mock.patch.object(webapp, "_run_construct", return_value=(snap, "fp")):
            response = webapp.app.test_client().post("/api/strategic/apply", json={"input_fingerprint": "fp"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        write_port.assert_not_called()

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

    def test_employment_reserve_is_isolated_from_risk_buffer(self):
        r = strategic.employment_resilience({
            "stable_assets_outside": 700000,
            "unemployment_monthly_expense": 6000,
            "unemployment_minimum_monthly_income": 0,
            "unemployment_runway_years": 5,
            "post_stress_reserve_months": 12,
        })
        self.assertTrue(r["passes"])
        self.assertEqual(r["required_reserve"], 432000)
        self.assertEqual(r["risk_buffer_available"], 268000)

    def test_employment_reserve_shortfall_fails(self):
        r = strategic.employment_resilience({
            "stable_assets_outside": 300000,
            "unemployment_monthly_expense": 6000,
            "unemployment_runway_years": 5,
            "post_stress_reserve_months": 12,
        })
        self.assertFalse(r["passes"])
        self.assertEqual(r["shortfall"], 132000)


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
        "| code | name | closePrice | nav | totalMV | turnoverValue | purchaseStatus | establishDate"
        " | ytdReturn | return1M | return3M | return6M | return1Y | return3Y | maxDrawdown1Y | maxDrawdown3Y |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| sh510300 | 沪深300ETF | 4.95 | 4.94 | 120000000000 | 1080000000 | 可申购 | 2012-05-28 00:00:00"
        " | 3.5 | 1.2 | -0.5 | 4.1 | 12.3 | 35.0 | 1.1 | 8.5 |\n"
        "| sh513500 | 标普500ETF | 2.57 | 2.45 | 9500000000 | 366000000 | 不可申购 | 2013-12-05 00:00:00"
        " | -6.82 | -4.99 | -6.21 | -6.82 | -12.0 | -33.72 | 0.82 | 6.93 |\n"
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

    def test_row_to_metrics_returns(self):
        """多周期收益/回撤字段应被解析进 returns dict（%，已为浮点）。"""
        row = webapp._parse_westock_etf_batch(self.BATCH_MD)["513500"]
        m = webapp._etf_row_to_metrics(row)
        ret = m.get("returns")
        self.assertIsNotNone(ret)
        self.assertAlmostEqual(ret["r1y"], -12.0)
        self.assertAlmostEqual(ret["r3y"], -33.72)
        self.assertAlmostEqual(ret["ytd"], -6.82)
        self.assertAlmostEqual(ret["mdd3y"], 6.93)
        # 正收益一侧
        row2 = webapp._parse_westock_etf_batch(self.BATCH_MD)["510300"]
        m2 = webapp._etf_row_to_metrics(row2)
        self.assertAlmostEqual(m2["returns"]["r1y"], 12.3)
        self.assertAlmostEqual(m2["returns"]["ytd"], 3.5)

    def test_row_to_metrics_missing_returns_gives_none(self):
        """没有收益字段时 returns 应为 None，其它字段不受影响。"""
        row_no_returns = {"code": "sh510300", "closePrice": "4.95", "nav": "4.94",
                          "totalMV": "1e11", "turnoverValue": "1e9",
                          "purchaseStatus": "可申购", "establishDate": "2012-05-28"}
        m = webapp._etf_row_to_metrics(row_no_returns)
        self.assertIsNone(m["returns"])
        self.assertIsNotNone(m["premium"])   # 其它字段照常

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


class TestExecQualityGate(unittest.TestCase):
    """执行质量闸纯函数：买入动作按折溢价 + 申购状态裁决。"""

    def test_high_premium_qdii_blocks(self):
        v, m = webapp._exec_quality_decision(0.0574, None, True)  # 5.74% 溢价、敏感
        self.assertEqual(v, "block")
        self.assertTrue(any("溢价" in x for x in m))

    def test_mild_premium_qdii_warns(self):
        v, _ = webapp._exec_quality_decision(0.008, None, True)   # 0.8% ∈ [0.5,1.5)
        self.assertEqual(v, "warn")

    def test_mild_premium_nonsensitive_ok(self):
        v, _ = webapp._exec_quality_decision(0.008, None, False)  # 0.8% < 1.0% 普通阈值
        self.assertEqual(v, "ok")

    def test_blocked_purchase_qdii_blocks(self):
        v, m = webapp._exec_quality_decision(0.0, "不可申购", True)
        self.assertEqual(v, "block")
        self.assertTrue(any("申购" in x for x in m))

    def test_blocked_purchase_nonsensitive_warns(self):
        v, _ = webapp._exec_quality_decision(0.0, "暂停申购", False)
        self.assertEqual(v, "warn")

    def test_missing_premium_qdii_warns_not_block(self):
        v, m = webapp._exec_quality_decision(None, None, True)    # 缺失≠中性：提示自查，不硬拦
        self.assertEqual(v, "warn")
        self.assertTrue(any("自查" in x for x in m))

    def test_missing_premium_nonsensitive_ok(self):
        v, _ = webapp._exec_quality_decision(None, None, False)
        self.assertEqual(v, "ok")

    def test_near_nav_ok(self):
        v, _ = webapp._exec_quality_decision(0.002, "可申购", True)  # 0.2% < 0.5%
        self.assertEqual(v, "ok")

    def test_gate_blocks_add_keeps_trim(self):
        """加仓(add) 命中高溢价→降级；减仓(trim) 不参与折溢价闸。"""
        orig = (webapp._quality_metrics, webapp.prefetch_westock,
                webapp._prefetch_westock_etf, webapp._etf_spot_snapshot)
        webapp._quality_metrics = lambda code, snap, sensitive: ({"premium": 0.06}, {"purchase_status": None})
        webapp.prefetch_westock = lambda codes: None           # 不打网络
        webapp._prefetch_westock_etf = lambda codes: None
        webapp._etf_spot_snapshot = lambda *a, **k: None
        try:
            sig = {
                "signals": {"513100": {"asset": "global_growth"}, "510300": {"asset": "domestic_equity"}},
                "actionable_rebalance": [
                    {"code": "513100", "suggest": "add", "actionable": True, "triggered": True, "approx_amount": 5000},
                    {"code": "510300", "suggest": "trim", "actionable": True, "triggered": True, "approx_amount": 5000},
                ],
                "first_funding_plan": {"orders": []},
            }
            out = webapp._apply_execution_quality_gate(sig)
            add = next(r for r in out["actionable_rebalance"] if r["code"] == "513100")
            trim = next(r for r in out["actionable_rebalance"] if r["code"] == "510300")
            self.assertFalse(add["actionable"])                 # 高溢价加仓被降级
            self.assertTrue(any("溢价" in x for x in add["blocked_reasons"]))
            self.assertTrue(trim["actionable"])                 # 减仓不受影响
            self.assertTrue(out.get("exec_quality_gated"))
        finally:
            (webapp._quality_metrics, webapp.prefetch_westock,
             webapp._prefetch_westock_etf, webapp._etf_spot_snapshot) = orig

    def test_recheck_cycle_suggestion_marks_current_block(self):
        orig = (webapp._quality_metrics, webapp.prefetch_westock,
                webapp._prefetch_westock_etf, webapp._etf_spot_snapshot)
        webapp._quality_metrics = lambda code, snap, sensitive: (
            {"premium": 0.06}, {"purchase_status": "不可申购"})
        webapp.prefetch_westock = lambda codes: None
        webapp._prefetch_westock_etf = lambda codes: None
        webapp._etf_spot_snapshot = lambda *a, **k: None
        try:
            rows = webapp._recheck_cycle_suggestions(
                [{"code": "513500", "side": "buy", "action_status": "pending"}],
                {"signals": {"513500": {"asset": "global_equity"}}})
            self.assertEqual(rows[0]["action_status"], "blocked_now")
            self.assertEqual(rows[0]["execution_quality"], "block")
        finally:
            (webapp._quality_metrics, webapp.prefetch_westock,
             webapp._prefetch_westock_etf, webapp._etf_spot_snapshot) = orig


# ---------- WS4：收益/冲击假设单一来源 + 可配置 + 有出处 ----------

class TestAssumptions(unittest.TestCase):
    def _uni(self):
        return {"511010": {"asset": "bond"}, "510300": {"asset": "equity"}, "518880": {"asset": "gold"}}

    def test_defaults_match_module_tables(self):
        a = signals.load_assumptions({})
        self.assertEqual(a["shocks"], signals.ASSET_SHOCKS)
        self.assertEqual(a["returns"], signals.ASSET_EXPECTED_RETURN)
        self.assertEqual(a["default_shock"], signals.DEFAULT_SHOCK)
        self.assertEqual(a["default_return"], signals.DEFAULT_EXPECTED_RETURN)
        self.assertEqual(a["meta"], {})

    def test_per_key_override_keeps_others(self):
        strat = {"assumptions": {"defaults": {"shock": -0.20, "expected_return": 0.04},
                                 "sleeves": {"equity": {"expected_return": 0.09, "shock": -0.25,
                                                        "source": "自定义", "note": "测试"}}}}
        a = signals.load_assumptions(strat)
        self.assertEqual(a["returns"]["equity"], 0.09)
        self.assertEqual(a["shocks"]["equity"], -0.25)
        self.assertEqual(a["default_shock"], -0.20)
        self.assertEqual(a["default_return"], 0.04)
        self.assertEqual(a["returns"]["bond"], signals.ASSET_EXPECTED_RETURN["bond"])  # 未覆盖项不变
        self.assertEqual(a["meta"]["equity"], {"source": "自定义", "note": "测试"})

    def test_malformed_sleeve_does_not_crash(self):
        a = signals.load_assumptions({"assumptions": {"sleeves": {"equity": "oops"}}})
        self.assertEqual(a["returns"]["equity"], signals.ASSET_EXPECTED_RETURN["equity"])

    def test_stress_uses_injected_shocks(self):
        holdings = valid_portfolio()["holdings"]
        base, _ = signals.estimate_target_stress_drawdown(holdings, self._uni())
        shk = dict(signals.ASSET_SHOCKS); shk["equity"] = -0.50
        bumped, _ = signals.estimate_target_stress_drawdown(holdings, self._uni(), shk, signals.DEFAULT_SHOCK)
        self.assertGreater(bumped, base)

    def test_expected_return_uses_injected_returns(self):
        holdings = valid_portfolio()["holdings"]
        base = signals.expected_etf_return(holdings, self._uni())
        ret = dict(signals.ASSET_EXPECTED_RETURN); ret["equity"] = 0.20
        bumped = signals.expected_etf_return(holdings, self._uni(), ret, signals.DEFAULT_EXPECTED_RETURN)
        self.assertGreater(bumped, base)

    def test_validate_rejects_out_of_range(self):
        strat = valid_strategy()
        strat["assumptions"] = {"sleeves": {"equity": {"shock": 0.5}}}  # shock 须 ≤0
        self.assertTrue(any("shock" in e for e in signals.validate_strategy(strat)))
        strat["assumptions"] = {"sleeves": {"equity": {"expected_return": 2.0}}}  # >1
        self.assertTrue(any("expected_return" in e for e in signals.validate_strategy(strat)))


# ---------- WS1：本周每只持仓 ETF 的 加仓/减仓/不动 理由 ----------

class TestRebalanceReason(unittest.TestCase):
    def _call(self, row, signal):
        return signals.explain_rebalance_action(
            row, signal, abs_thr_pp=5, rel_thr=0.25, min_trade=500, max_weekly=50000)

    def test_hold_no_trade_wording(self):
        row = {"suggest": "hold", "triggered": False, "actionable": False, "deviation_pp": 1.2,
               "blocked_reasons": ["未触发再平衡"]}
        txt, f = self._call(row, {"trend": "above", "momentum_60d": 0.02})
        self.assertIn("维持当前仓位", txt)
        self.assertNotIn("加仓", txt)
        self.assertNotIn("减仓", txt)
        self.assertEqual(f["suggest"], "hold")

    def test_add_triggered_has_qualifiers(self):
        row = {"suggest": "add", "triggered": True, "actionable": True, "deviation_pp": -6.0,
               "approx_amount": 3000, "blocked_reasons": []}
        sig = {"trend": "above", "momentum_60d": 0.08, "valuation": {"percentile": 0.2, "tag": "cheap"}}
        txt, f = self._call(row, sig)
        self.assertIn("建议加仓", txt)
        self.assertIn("动量偏强", txt)
        self.assertFalse(f["valuation_decel"])

    def test_trim_triggered(self):
        row = {"suggest": "trim", "triggered": True, "actionable": True, "deviation_pp": 7.0,
               "approx_amount": 4000, "blocked_reasons": []}
        txt, _ = self._call(row, {"trend": "below"})
        self.assertIn("建议减仓", txt)
        self.assertIn("跌破 MA200", txt)

    def test_blocked_shows_reason(self):
        row = {"suggest": "add", "triggered": True, "actionable": False, "deviation_pp": -6.0,
               "approx_amount": 300, "blocked_reasons": ["金额低于最小交易门槛 500 元"]}
        txt, _ = self._call(row, {"trend": "above"})
        self.assertIn("被拦截", txt)
        self.assertIn("最小交易门槛", txt)

    def test_valuation_missing_not_neutral(self):
        row = {"suggest": "hold", "triggered": False, "actionable": False, "deviation_pp": 0.5, "blocked_reasons": []}
        txt, _ = self._call(row, {"trend": "above", "valuation_missing": {"available": False}})
        self.assertIn("缺失", txt)
        self.assertIn("非中性", txt)          # 如实标"缺失(非中性)"
        self.assertNotIn("估值中性", txt)     # 绝不把缺失当成中性

    def test_valuation_na_not_applicable(self):
        row = {"suggest": "hold", "triggered": False, "actionable": False, "deviation_pp": 0.5, "blocked_reasons": []}
        txt, _ = self._call(row, {"trend": "above", "valuation_na": True})
        self.assertIn("不适用", txt)

    def test_momentum_none_omitted(self):
        row = {"suggest": "hold", "triggered": False, "actionable": False, "deviation_pp": 0.5, "blocked_reasons": []}
        txt, f = self._call(row, {"trend": "above", "momentum_60d": None})
        self.assertNotIn("动量", txt)
        self.assertIsNone(f["momentum_bucket"])

    def test_rich_add_decel_hint(self):
        row = {"suggest": "add", "triggered": True, "actionable": True, "deviation_pp": -6.0,
               "approx_amount": 3000, "blocked_reasons": []}
        txt, f = self._call(row, {"trend": "above", "valuation": {"percentile": 0.94, "tag": "rich"}})
        self.assertIn("缓建", txt)
        self.assertTrue(f["valuation_decel"])

    def test_error_row_no_trade_wording(self):
        row = {"suggest": "hold", "triggered": False, "actionable": False, "deviation_pp": 0, "blocked_reasons": []}
        txt, f = self._call(row, {"error": "数据不足或拉取失败"})
        self.assertIn("不评估", txt)
        self.assertNotIn("加仓", txt)
        self.assertEqual(f.get("state"), "no_data")

    def test_deterministic(self):
        row = {"suggest": "add", "triggered": True, "actionable": True, "deviation_pp": -6.0,
               "approx_amount": 3000, "blocked_reasons": []}
        sig = {"trend": "above", "momentum_60d": 0.08, "valuation": {"percentile": 0.2, "tag": "cheap"}}
        self.assertEqual(self._call(row, sig)[0], self._call(row, sig)[0])

    def test_exec_quality_gate_appends_to_reason(self):
        orig = (webapp._quality_metrics, webapp.prefetch_westock,
                webapp._prefetch_westock_etf, webapp._etf_spot_snapshot)
        webapp._quality_metrics = lambda code, snap, sensitive: ({"premium": 0.008}, {"purchase_status": None})
        webapp.prefetch_westock = lambda codes: None
        webapp._prefetch_westock_etf = lambda codes: None
        webapp._etf_spot_snapshot = lambda *a, **k: None
        try:
            sig = {
                "signals": {"513100": {"asset": "global_growth"}},
                "actionable_rebalance": [
                    {"code": "513100", "suggest": "add", "actionable": True, "triggered": True,
                     "approx_amount": 5000, "action_reason": "建议加仓约 ¥5,000",
                     "reason_factors": {"exec_quality": "none"}},
                ],
                "first_funding_plan": {"orders": []},
            }
            out = webapp._apply_execution_quality_gate(sig)
            a = out["actionable_rebalance"][0]
            self.assertEqual(a["exec_quality"], "warn")
            self.assertIn("执行质量提示", a["action_reason"])
            self.assertEqual(a["reason_factors"]["exec_quality"], "warn")
        finally:
            (webapp._quality_metrics, webapp.prefetch_westock,
             webapp._prefetch_westock_etf, webapp._etf_spot_snapshot) = orig


class TestRebalanceFrequencyGate(unittest.TestCase):
    import datetime as _dt
    T = _dt.date(2026, 6, 30)

    def test_weekly_never_gates(self):
        # 默认每周：min_gap=0，无论距上次成交多近都不闸（保持现状行为）
        mg, ds, gated = signals.frequency_gate_state("weekly", self._dt.date(2026, 6, 29), self.T)
        self.assertEqual(mg, 0)
        self.assertFalse(gated)

    def test_monthly_blocks_within_window(self):
        mg, ds, gated = signals.frequency_gate_state("monthly", self._dt.date(2026, 6, 20), self.T)
        self.assertEqual(mg, 28)
        self.assertEqual(ds, 10)
        self.assertTrue(gated)                 # 距上次 10 天 < 28 → 闸住

    def test_monthly_allows_after_window(self):
        mg, ds, gated = signals.frequency_gate_state("monthly", self._dt.date(2026, 5, 1), self.T)
        self.assertGreaterEqual(ds, 28)
        self.assertFalse(gated)                # 已满 28 天 → 放行

    def test_no_history_never_gates(self):
        mg, ds, gated = signals.frequency_gate_state("quarterly", None, self.T)
        self.assertIsNone(ds)
        self.assertFalse(gated)                # 无成交记录 → 不闸

    def test_unknown_freq_falls_back_to_no_gap(self):
        mg, ds, gated = signals.frequency_gate_state("yearly", self._dt.date(2026, 6, 29), self.T)
        self.assertEqual(mg, 0)
        self.assertFalse(gated)

    def test_latest_execution_date_parses_filenames(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            ex = os.path.join(root, "journal", "executions")
            os.makedirs(ex)
            for fn in ("2026-05-01_090001.json", "2026-06-08_144015.json", "bad.json", "notdate_1.json"):
                with open(os.path.join(ex, fn), "w") as f:
                    f.write("{}")
            self.assertEqual(signals.latest_execution_date(root), self._dt.date(2026, 6, 8))
        self.assertIsNone(signals.latest_execution_date(None))

    def test_validate_strategy_checks_frequency_and_breaker(self):
        s = valid_strategy()
        s["factors"]["rebalance"]["check_frequency"] = "monthly"
        s["factors"]["rebalance"]["circuit_breaker_pp"] = 15
        self.assertEqual(signals.validate_strategy(s), [])
        s["factors"]["rebalance"]["check_frequency"] = "yearly"
        self.assertTrue(any("check_frequency" in e for e in signals.validate_strategy(s)))
        s["factors"]["rebalance"]["check_frequency"] = "monthly"
        s["factors"]["rebalance"]["circuit_breaker_pp"] = 3   # ≤ abs_threshold_pp
        self.assertTrue(any("circuit_breaker_pp" in e for e in signals.validate_strategy(s)))


# ---------- Track C Phase A：战略基础工具正确性（零值/确定性投影）----------

class TestStrategicPhaseA(unittest.TestCase):
    def _profile(self, **over):
        p = {"max_acceptable_drawdown": 0.2, "target_annual_return": 0.08,
             "experience_level": "intermediate", "planned_etf_capital": 1000000,
             "stable_assets_outside": 700000}
        p.update(over)
        return p
# ---------- Track C Phase B：ETF 费率解析 + §8.2 硬准入 ----------

class TestEtfFeeParse(unittest.TestCase):
    def test_standard_row(self):
        f = strategic.parse_etf_fee([["管理费率", "0.15%（每年）", "托管费率", "0.05%（每年）"]])
        self.assertAlmostEqual(f["management_fee"], 0.0015)
        self.assertAlmostEqual(f["custody_fee"], 0.0005)
        self.assertAlmostEqual(f["expense_ratio"], 0.002)

    def test_qdii_higher_fee(self):
        f = strategic.parse_etf_fee([["管理费率", "0.60%（每年）", "托管费率", "0.20%（每年）"]])
        self.assertAlmostEqual(f["expense_ratio"], 0.008)

    def test_missing_custody(self):
        f = strategic.parse_etf_fee([["管理费率", "0.15%（每年）"]])
        self.assertAlmostEqual(f["management_fee"], 0.0015)
        self.assertIsNone(f["custody_fee"])
        self.assertAlmostEqual(f["expense_ratio"], 0.0015)   # 单边也给合计

    def test_same_cell_layout(self):
        f = strategic.parse_etf_fee([["管理费率：0.15%"]])
        self.assertAlmostEqual(f["management_fee"], 0.0015)

    def test_empty_or_garbage_all_none(self):
        for rows in ([], [["无关", "数据"]], None):
            f = strategic.parse_etf_fee(rows)
            self.assertIsNone(f["management_fee"])
            self.assertIsNone(f["expense_ratio"])           # 缺失绝不编造


class TestHardAdmission(unittest.TestCase):
    def _good(self, **over):
        c = {"market_cap": 50e8, "avg_turnover_20d": 5e8, "premium": 0.01,
             "purchase_status": "可申购", "listed_years": 5.0,
             "fee": {"expense_ratio": 0.002}}
        c.update(over)
        return c

    def test_all_pass_admitted(self):
        r = strategic.hard_admission(self._good(), planned_single_trade=50000, planned_position=200000)
        self.assertTrue(r["admitted"])
        self.assertFalse(r["blockers"])

    def test_liquidity_fail(self):
        # 计划单笔 100万 > 5% × 1000万 = 50万
        r = strategic.hard_admission(self._good(avg_turnover_20d=1e7), planned_single_trade=1_000_000)
        self.assertFalse(r["admitted"])
        self.assertTrue(any("成交额" in b for b in r["blockers"]))

    def test_capacity_fail(self):
        # 计划持仓 300万 > 1% × 1亿 = 100万
        r = strategic.hard_admission(self._good(market_cap=1e8), planned_position=3_000_000)
        self.assertFalse(r["admitted"])
        self.assertTrue(any("持仓" in b for b in r["blockers"]))

    def test_premium_does_not_block_admission(self):
        # 折溢价是执行时点问题，不进长期准入：高溢价/缺失都不影响 admitted（仍由执行质量闸下单时把关）
        high = strategic.hard_admission(self._good(premium=0.05), planned_single_trade=50000, planned_position=200000)
        self.assertTrue(high["admitted"])
        self.assertFalse(any("折溢价" in b for b in high["blockers"]))
        self.assertFalse(any("折溢价" in g for g in high["data_gaps"]))
        none = strategic.hard_admission(self._good(premium=None), planned_single_trade=50000, planned_position=200000)
        self.assertTrue(none["admitted"])      # 折溢价缺失也不再阻断长期准入

    def test_purchase_block_fail(self):
        r = strategic.hard_admission(self._good(purchase_status="暂停申购"), planned_position=1000)
        self.assertFalse(r["admitted"])

    def test_small_scale_fail(self):
        r = strategic.hard_admission(self._good(market_cap=1e8), planned_position=1000)
        self.assertFalse(r["admitted"])

    def test_missing_critical_is_fail_closed(self):
        # 关键字段（规模等）缺失=关键 gap → 不准入（绝不 fail-open），且归入 data_gaps 而非 blockers
        r = strategic.hard_admission(self._good(market_cap=None), planned_position=1000)
        self.assertFalse(r["admitted"])
        self.assertTrue(any("规模" in g for g in r["data_gaps"]))
        self.assertFalse(any("规模" in b for b in r["blockers"]))

    def test_missing_fee_is_soft_gap(self):
        # 费率缺失=软 gap，其它齐全仍可准入
        r = strategic.hard_admission(self._good(fee=None), planned_single_trade=50000, planned_position=200000)
        self.assertTrue(r["admitted"])
        self.assertTrue(any("费" in g for g in r["data_gaps"]))

    def test_listed_years_known_short_fails_unknown_is_soft(self):
        short = strategic.hard_admission(self._good(listed_years=0.5), planned_position=1000)
        self.assertFalse(short["admitted"])                  # 已知 <1 年 → fail
        unknown = strategic.hard_admission(self._good(listed_years=None),
                                           planned_single_trade=50000, planned_position=200000)
        self.assertTrue(unknown["admitted"])                 # 未知 → 软 gap，不阻断

    def test_deterministic(self):
        c = self._good()
        self.assertEqual(strategic.hard_admission(c, planned_position=1000),
                         strategic.hard_admission(c, planned_position=1000))


# ---------- Track C Phase B Step 2：§8.3 产品分 + 三层目录骨架 ----------

class TestProductScore(unittest.TestCase):
    def _full(self, **over):
        c = {"market_cap": 50e8, "avg_turnover_20d": 5e8, "premium": 0.005,
             "purchase_status": "可申购", "listed_years": 5.0,
             "fee": {"expense_ratio": 0.002}, "tracking_dispersion": None}
        c.update(over)
        return c

    def test_tracking_missing_is_degraded_not_neutral(self):
        r = strategic.product_score(self._full())          # tracking 缺
        self.assertIsNone(r["subscores"]["tracking_quality"]["score"])   # 缺=None，绝不 0.5
        self.assertAlmostEqual(r["coverage"], 0.75)        # 1 − 0.25
        self.assertEqual(r["status"], "degraded")
        self.assertIsNotNone(r["total"])                   # 仅可得子分归一

    def test_full_coverage_scored(self):
        r = strategic.product_score(self._full(tracking_dispersion=0.01))
        self.assertAlmostEqual(r["coverage"], 1.0)
        self.assertEqual(r["status"], "scored")
        self.assertEqual(r["confidence"], "high")

    def test_missing_critical_flags(self):
        r = strategic.product_score(self._full(fee=None))  # total_cost 关键缺
        self.assertTrue(any("total_cost_quality" in f for f in r["flags"]))
        self.assertIn(r["status"], ("degraded", "insufficient"))

    def test_all_critical_missing_insufficient(self):
        r = strategic.product_score(self._full(market_cap=None, avg_turnover_20d=None, fee=None))
        self.assertEqual(r["status"], "insufficient")
        self.assertLess(r["coverage"], 0.5)

    def test_cheaper_fee_scores_higher(self):
        lo = strategic.product_score(self._full(fee={"expense_ratio": 0.002}))
        hi = strategic.product_score(self._full(fee={"expense_ratio": 0.009}))
        self.assertGreater(lo["subscores"]["total_cost_quality"]["score"],
                           hi["subscores"]["total_cost_quality"]["score"])

    def test_not_inflated_by_returns(self):
        base = strategic.product_score(self._full())
        withret = strategic.product_score({**self._full(), "returns": {"r1y": 0.5}})
        self.assertEqual(base["total"], withret["total"])   # 评分不含收益项

    def test_deterministic(self):
        c = self._full()
        self.assertEqual(strategic.product_score(c), strategic.product_score(c))


class TestBuildCatalog(unittest.TestCase):
    def _strat(self):
        return {
            "universe": [{"code": "510300", "name": "沪深300"}, {"code": "513100", "name": "纳指"},
                         {"code": "159915", "name": "创业板"}, {"code": "588000", "name": "科创50"}],
            "strategic_policy": {"roles": {
                "china_core_equity": {"tier": "core", "members": ["510300"], "range": [0.10, 0.35]},
                "growth_satellite": {"tier": "satellite", "members": ["513100", "159915", "588000"], "range": [0.00, 0.20]},
            }},
        }

    def _port(self):
        return {"holdings": [{"code": "510300", "target_weight": 0.15}, {"code": "513100", "target_weight": 0.13},
                             {"code": "159915", "target_weight": 0.06}, {"code": "588000", "target_weight": 0.06}]}

    def test_roles_and_members(self):
        cat = strategic.build_catalog(self._strat(), self._port())
        by = {r["role"]: r for r in cat["roles"]}
        self.assertEqual(len(by["growth_satellite"]["members"]), 3)
        self.assertEqual(by["growth_satellite"]["members"][0]["name"], "纳指")

    def test_range_status_above_and_within(self):
        cat = strategic.build_catalog(self._strat(), self._port())
        by = {r["role"]: r for r in cat["roles"]}
        self.assertAlmostEqual(by["growth_satellite"]["current_total"], 0.25)
        self.assertEqual(by["growth_satellite"]["range_status"], "above")   # 25% > 20%
        self.assertEqual(by["china_core_equity"]["range_status"], "within")

    def test_no_policy_is_empty(self):
        self.assertEqual(strategic.build_catalog({"universe": []}, None)["roles"], [])


class TestTrackingAndOverlap(unittest.TestCase):
    def test_identical_series_zero_dispersion(self):
        s = [0.01, -0.02, 0.005, 0.0, 0.012] * 6        # 30 点
        self.assertEqual(strategic.tracking_dispersion(s, s), 0.0)

    def test_constant_offset_zero_dispersion(self):
        idx = [0.01, -0.02, 0.005, 0.0, 0.012] * 6
        etf = [x + 0.001 for x in idx]                  # 恒定跟踪偏移 → 差值 std=0
        self.assertEqual(strategic.tracking_dispersion(etf, idx), 0.0)

    def test_varying_diff_positive(self):
        idx = [0.01, -0.02, 0.005, 0.0, 0.012] * 6
        etf = [x + (0.002 if i % 2 else -0.002) for i, x in enumerate(idx)]
        self.assertGreater(strategic.tracking_dispersion(etf, idx), 0)

    def test_too_few_points_none(self):
        self.assertIsNone(strategic.tracking_dispersion([0.01] * 10, [0.0] * 10))

    def test_jaccard_identical_and_disjoint(self):
        self.assertEqual(strategic.weighted_jaccard({"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5}), 1.0)
        self.assertEqual(strategic.weighted_jaccard({"a": 1.0}, {"b": 1.0}), 0.0)   # QDII↔A股 即此情形

    def test_jaccard_partial(self):
        self.assertAlmostEqual(strategic.weighted_jaccard({"x": 0.6, "y": 0.4}, {"x": 0.5, "z": 0.5}), 0.3333, places=3)

    def test_jaccard_empty_none(self):
        self.assertIsNone(strategic.weighted_jaccard({}, {"a": 1.0}))   # 无法判定，绝不默认低重合


class TestIncumbentAssess(unittest.TestCase):
    def test_disposition_rules(self):
        d = strategic.incumbent_disposition
        self.assertEqual(d(role_range_status="within", admitted=True), "keep")
        self.assertEqual(d(role_range_status="above", admitted=True), "trim")
        self.assertEqual(d(role_range_status="above", admitted=True, redundant=True), "review")
        self.assertEqual(d(role_range_status="within", single_cap_exceeded=True, admitted=True), "trim")
        self.assertEqual(d(role_range_status="within", admitted=True, redundant=True), "review")
        self.assertEqual(d(role_range_status="within", admitted=False, has_blockers=True), "replace_candidate")
        # 准入不过但只是数据缺失（无真实阻断）→ 待复核，不是考虑替换
        self.assertEqual(d(role_range_status="within", admitted=False, has_blockers=False), "review_data")

    def _strat(self):
        return {
            "universe": [{"code": "510300", "name": "沪深300", "asset": "equity"},
                         {"code": "513100", "name": "纳指", "asset": "global_growth"},
                         {"code": "159915", "name": "创业板", "asset": "china_growth"},
                         {"code": "588000", "name": "科创50", "asset": "china_growth"}],
            "strategic_policy": {"caps": {"single_satellite_max": 0.10}, "roles": {
                "china_core_equity": {"tier": "core", "members": ["510300"], "range": [0.10, 0.35]},
                "growth_satellite": {"tier": "satellite", "members": ["513100", "159915", "588000"], "range": [0.00, 0.20]},
            }},
        }

    def _port(self):
        return {"holdings": [{"code": "510300", "target_weight": 0.15}, {"code": "513100", "target_weight": 0.13},
                             {"code": "159915", "target_weight": 0.06}, {"code": "588000", "target_weight": 0.06}]}

    def _q(self, admitted=True):
        return {c: {"admission": {"admitted": admitted}, "score": {"total": 0.8, "status": "degraded"}}
                for c in ("510300", "513100", "159915", "588000")}

    def test_assess_dispositions_and_consolidation(self):
        rows = strategic.assess_incumbents(self._strat(), self._port(), self._q())
        by = {r["code"]: r for r in rows}
        self.assertEqual(by["510300"]["disposition"], "keep")            # core within
        self.assertTrue(by["513100"]["single_cap_exceeded"])             # 13% > 10%
        self.assertFalse(by["513100"]["consolidation_candidate"])        # global_growth 独此一只
        self.assertEqual(by["513100"]["disposition"], "trim")            # 超上限、非冗余
        # 创业板/科创50 同属 china_growth 卫星 → 二选一候选 → review
        self.assertTrue(by["159915"]["consolidation_candidate"])
        self.assertTrue(by["588000"]["consolidation_candidate"])
        self.assertEqual(by["159915"]["disposition"], "review")
        self.assertEqual(by["588000"]["disposition"], "review")

    def test_not_admitted_replace(self):
        # 有真实阻断（溢价/限购等）→ replace_candidate（暂不加仓）
        q = self._q()
        q["513100"]["admission"] = {"admitted": False, "blockers": ["折溢价 +5% 超出 ±3%"]}
        rows = strategic.assess_incumbents(self._strat(), self._port(), q)
        by = {r["code"]: r for r in rows}
        self.assertEqual(by["513100"]["disposition"], "replace_candidate")

    def test_data_gap_is_review_not_replace(self):
        # 仅数据缺失（无 blockers）→ review_data（待复核），不再误报考虑替换
        q = self._q()
        q["513100"]["admission"] = {"admitted": False, "blockers": [], "data_gaps": ["折溢价数据缺失"]}
        rows = strategic.assess_incumbents(self._strat(), self._port(), q)
        by = {r["code"]: r for r in rows}
        self.assertEqual(by["513100"]["disposition"], "review_data")
        self.assertFalse(by["513100"]["has_blockers"])

    def test_holdings_overlap_flags_redundant(self):
        # 同角色两只高 Jaccard → holdings_redundant；无 holdings 时 max_same_role_overlap=None
        none_h = strategic.assess_incumbents(self._strat(), self._port(), self._q())
        self.assertIsNone({r["code"]: r for r in none_h}["159915"]["max_same_role_overlap"])
        holds = {"513100": {"AAPL": 0.5, "MSFT": 0.5}, "159915": {"AAPL": 0.5, "MSFT": 0.5},
                 "588000": {"ZZZ": 1.0}, "510300": {"600000": 1.0}}
        rows = strategic.assess_incumbents(self._strat(), self._port(), self._q(), holdings_by_code=holds)
        by = {r["code"]: r for r in rows}
        self.assertTrue(by["513100"]["holdings_redundant"])             # 与 159915 Jaccard=1
        self.assertAlmostEqual(by["513100"]["max_same_role_overlap"], 1.0)

    def test_overlap_matrix(self):
        m = strategic.overlap_matrix({"a": {"x": 1.0}, "b": {"x": 1.0}, "c": {"y": 1.0}})
        self.assertEqual(m["a"]["b"], 1.0)
        self.assertEqual(m["a"]["c"], 0.0)


class TestStrategicReplacementCandidates(unittest.TestCase):
    def test_only_same_asset_non_members_are_candidates(self):
        strat = {
            "universe": [
                {"code": "A", "name": "A", "asset": "equity"},
                {"code": "B", "name": "B", "asset": "equity"},
                {"code": "G", "name": "G", "asset": "gold"},
            ],
            "watchlist": [{"code": "W", "name": "W", "asset": "equity"}],
            "strategic_policy": {"roles": {
                "equity_role": {"members": ["A"]},
                "gold_role": {"members": ["G"]},
            }},
        }
        rows = webapp._replacement_candidates(strat)
        self.assertEqual({(r["role"], r["code"], r["source"]) for r in rows},
                         {("equity_role", "B", "universe"), ("equity_role", "W", "watchlist")})

    def test_introduce_endpoint_reports_helper_failure(self):
        with mock.patch.object(webapp, "_introduce_strategic_role_member",
                               return_value=(False, "candidate must pass basic admission")):
            response = webapp.app.test_client().post(
                "/api/strategic/roles/introduce", json={"role": "x", "code": "A"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("basic admission", response.get_json()["error"])


# ---------- Track C Phase C：权威战略组合构建 §10 ----------

class TestConstructStrategic(unittest.TestCase):
    def _policy(self, priority="return_first"):
        return {
            "selection_priority": priority,
            "caps": {"non_satellite_min": 0.50, "satellite_max": 0.20, "single_satellite_max": 0.10,
                     "growth_factor_max": 0.20, "single_country_equity_max": 0.45, "single_risk_currency_exposure_max": 0.55},
            "roles": {
                "china_core_equity": {"tier": "core", "members": ["A1"], "range": [0.10, 0.35]},
                "us_core_equity": {"tier": "core", "members": ["A2"], "range": [0.10, 0.35]},
                "growth_satellite": {"tier": "satellite", "members": ["G1", "G2"], "range": [0.00, 0.20]},
                "government_bond": {"tier": "core_defensive", "members": ["B1"], "range": [0.05, 0.40]},
                "gold": {"tier": "diversifier", "members": ["GD"], "range": [0.00, 0.15]},
            },
        }

    _ASSET = {"A1": "equity", "A2": "global_equity", "G1": "global_growth", "G2": "china_growth",
              "B1": "bond", "GD": "gold"}
    _RET = {"equity": 0.07, "global_equity": 0.08, "global_growth": 0.10, "china_growth": 0.09, "bond": 0.03, "gold": 0.02}
    _SHK = {"equity": -0.30, "global_equity": -0.30, "global_growth": -0.40, "china_growth": -0.40, "bond": -0.03, "gold": -0.15}

    def _run(self, priority="return_first", max_stress=0.30):
        return strategic.construct_strategic_portfolio(
            self._policy(priority), returns=self._RET, shocks=self._SHK, target_return=0.08,
            asset_of=self._ASSET, etf_share=1.0, max_whole_stress=max_stress)

    def test_feasible_and_caps_respected(self):
        s = self._run()
        self.assertEqual(s["validation_status"], "passed")
        self.assertAlmostEqual(sum(s["instrument_allocation"].values()), 1.0, places=3)
        m = s["metrics"]
        self.assertLessEqual(m["satellite_total"], 0.20 + 1e-9)
        self.assertLessEqual(m["growth_factor_total"], 0.20 + 1e-9)
        self.assertLessEqual(max(m["country_equity"].values()), 0.45 + 1e-9)
        self.assertLessEqual(max(m["risk_currency_exposure"].values()), 0.55 + 1e-9)
        self.assertLessEqual(m["whole_portfolio_stress"], 0.30 + 5e-3)
        self.assertTrue(all(w <= 0.10 + 1e-9 for c, w in s["instrument_allocation"].items() if c in ("G1", "G2")))

    def test_deterministic(self):
        self.assertEqual(self._run(), self._run())

    def test_no_feasible_under_tight_stress(self):
        s = self._run(max_stress=0.01)        # 任何风险资产都超 → 无可行
        self.assertEqual(s["validation_status"], "no_feasible_portfolio")
        self.assertEqual(s["instrument_allocation"], {})

    def test_return_first_beats_defensive(self):
        r = self._run("return_first")["metrics"]["expected_etf_return"]
        d = self._run("defensive_first")["metrics"]["expected_etf_return"]
        self.assertGreaterEqual(r, d)

    def test_projection_alias_matches(self):
        # app.py 别名与 strategic 同源
        self.assertEqual(webapp._deterministic_projection([0.333, 0.333, 0.334]),
                         strategic._deterministic_projection([0.333, 0.333, 0.334]))

    def test_multi_scenario_picks_worst(self):
        mild = {a: -0.05 for a in self._SHK}
        severe = {a: -0.50 for a in self._SHK}
        s = strategic.construct_strategic_portfolio(
            self._policy(), returns=self._RET, shocks=self._SHK, target_return=0.08,
            asset_of=self._ASSET, etf_share=1.0, max_whole_stress=0.60,
            scenarios=[{"name": "mild", "shocks": mild}, {"name": "severe", "shocks": severe}])
        self.assertEqual(s["metrics"]["worst_scenario"], "severe")
        self.assertGreater(s["metrics"]["whole_portfolio_stress"], 0.30)   # 受最坏情景驱动

    def test_caps_hold_under_return_perturbation(self):
        # §12.4 稳健性：收益 ±30% 扰动下，构建仍守住 §18 上限（caps 主导→对假设误差稳定）
        for delta in (-0.3, 0.3):
            rp = {a: v * (1 + delta) for a, v in self._RET.items()}
            s = strategic.construct_strategic_portfolio(
                self._policy(), returns=rp, shocks=self._SHK, target_return=0.08,
                asset_of=self._ASSET, etf_share=1.0, max_whole_stress=0.30)
            self.assertEqual(s["validation_status"], "passed")
            self.assertLessEqual(s["metrics"]["satellite_total"], 0.20 + 1e-9)
            self.assertLessEqual(s["metrics"]["growth_factor_total"], 0.20 + 1e-9)

    def test_derive_comparison_portfolios(self):
        constructed = {"A1": 0.30, "A2": 0.30, "G1": 0.10, "G2": 0.10, "B1": 0.15, "GD": 0.05}
        current = {"A1": 0.2, "A2": 0.2, "G1": 0.2, "G2": 0.1, "B1": 0.2, "GD": 0.1}
        tier = {"A1": "core", "A2": "core", "G1": "satellite", "G2": "satellite",
                "B1": "core_defensive", "GD": "diversifier"}
        p = strategic.derive_comparison_portfolios(constructed, current, self._ASSET, tier)
        self.assertEqual(set(p), {"当前", "权威构建", "仅核心", "无卫星", "无黄金", "更低权益"})
        for name, d in p.items():
            self.assertAlmostEqual(sum(d.values()), 1.0, places=2, msg=name)   # 各自归一
        self.assertNotIn("GD", p["无黄金"])
        self.assertNotIn("G1", p["无卫星"])
        self.assertNotIn("G2", p["无卫星"])
        self.assertEqual(set(p["仅核心"]), {"A1", "A2", "B1"})
        eq = {"A1", "A2", "G1", "G2"}
        self.assertLess(sum(v for k, v in p["更低权益"].items() if k in eq),
                        sum(v for k, v in p["权威构建"].items() if k in eq))   # 权益占比更低

    def test_conservative_gap_reported(self):
        cons = {a: v - 0.03 for a, v in self._RET.items()}
        s = strategic.construct_strategic_portfolio(
            self._policy(), returns=self._RET, shocks=self._SHK, target_return=0.08,
            asset_of=self._ASSET, etf_share=1.0, max_whole_stress=0.30, returns_conservative=cons)
        m = s["metrics"]
        self.assertLess(m["expected_etf_return_conservative"], m["expected_etf_return"])
        self.assertGreaterEqual(m["target_gap_conservative"], m["target_gap"])

    def test_final_projection_recomputes_and_preserves_caps(self):
        policy = {"caps": {"satellite_max": 0.05}, "roles": {
            "sat": {"tier": "satellite", "members": ["A1", "A2"], "range": [0.05, 0.05]},
            "core": {"tier": "core", "members": ["B1", "B2"], "range": [0.95, 0.95]},
        }}
        assets = {"A1": "global_growth", "A2": "global_growth", "B1": "bond", "B2": "bond"}
        s = strategic.construct_strategic_portfolio(
            policy, returns=self._RET, shocks=self._SHK, target_return=0.01,
            asset_of=assets, max_whole_stress=1.0)
        self.assertEqual(s["validation_status"], "passed")
        actual_sat = sum(s["instrument_allocation"][c] for c in ("A1", "A2"))
        self.assertAlmostEqual(actual_sat, 0.05, places=9)
        self.assertAlmostEqual(s["metrics"]["satellite_total"], actual_sat, places=9)

    def test_empty_role_fails_closed(self):
        policy = {"roles": {
            "missing": {"tier": "core", "members": [], "range": [0.50, 0.50]},
            "core": {"tier": "core", "members": ["B1"], "range": [0.50, 0.50]},
        }}
        s = strategic.construct_strategic_portfolio(
            policy, returns=self._RET, shocks=self._SHK, target_return=0.01,
            asset_of={"B1": "bond"}, max_whole_stress=1.0)
        self.assertEqual(s["validation_status"], "no_feasible_portfolio")
        self.assertIn("missing", s["constraint_diagnostics"][0])

    def test_product_admission_and_score_select_primary(self):
        policy = {"roles": {
            "core": {"tier": "core", "members": ["BAD", "LOW", "HIGH"], "range": [1.0, 1.0]},
        }}
        quality = {
            "BAD": {"admission": {"admitted": False}, "score": {"total": 1.0, "coverage": 1.0}},
            "LOW": {"admission": {"admitted": True}, "score": {"total": 0.4, "coverage": 1.0}},
            "HIGH": {"admission": {"admitted": True}, "score": {"total": 0.9, "coverage": 1.0}},
        }
        s = strategic.construct_strategic_portfolio(
            policy, returns=self._RET, shocks=self._SHK, target_return=0.01,
            asset_of={"BAD": "equity", "LOW": "equity", "HIGH": "equity"},
            exposure_of={"BAD": "same", "LOW": "same", "HIGH": "same"},
            instrument_quality=quality, max_whole_stress=1.0)
        self.assertEqual(s["instrument_allocation"], {"HIGH": 1.0})
        self.assertEqual(s["selected_instruments"]["core"]["backup"]["same"], ["LOW"])

    def test_incumbent_with_only_data_gaps_is_provisional(self):
        policy = {"roles": {"core": {"tier": "core", "members": ["OLD"], "range": [1.0, 1.0]}}}
        quality = {"OLD": {"admission": {"admitted": False, "blockers": [], "data_gaps": ["missing"]},
                           "score": {"total": 0.5, "coverage": 0.5}}}
        s = strategic.construct_strategic_portfolio(
            policy, returns=self._RET, shocks=self._SHK, target_return=0.01,
            asset_of={"OLD": "equity"}, instrument_quality=quality,
            incumbent_codes={"OLD"}, incumbent_weights={"OLD": 1.0}, max_whole_stress=1.0)
        self.assertEqual(s["validation_status"], "passed")
        self.assertEqual(s["instrument_allocation"], {"OLD": 1.0})
        self.assertIn("data gaps", s["selection_diagnostics"][0])

    def test_failed_incumbent_cannot_be_increased(self):
        policy = {"roles": {
            "restricted": {"tier": "core", "members": ["OLD"], "range": [0.20, 0.50]},
            "other": {"tier": "core", "members": ["NEW"], "range": [0.50, 0.80]},
        }}
        quality = {"OLD": {"admission": {"admitted": False, "blockers": ["premium"], "data_gaps": []},
                           "score": {"total": 0.9, "coverage": 1.0}}}
        s = strategic.construct_strategic_portfolio(
            policy, returns={"equity": 0.20, "bond": 0.01},
            shocks={"equity": -0.3, "bond": -0.03}, target_return=0.01,
            asset_of={"OLD": "equity", "NEW": "bond"}, instrument_quality=quality,
            incumbent_codes={"OLD"}, incumbent_weights={"OLD": 0.20}, max_whole_stress=1.0)
        self.assertEqual(s["validation_status"], "passed")
        self.assertLessEqual(s["instrument_allocation"]["OLD"], 0.20)

    def test_missing_quality_record_is_fail_closed_when_required(self):
        # §8.2 阻断项 #1：质量缓存为空时，require_quality 把 incumbent 封顶在当前权重（不再悄悄抬高）
        policy = {"roles": {
            "us": {"tier": "core", "members": ["US"], "range": [0.20, 0.20]},
            "cn": {"tier": "core", "members": ["CN"], "range": [0.80, 0.80]},
        }, "caps": {}}
        kw = dict(returns={"equity": 0.07, "global_equity": 0.08},
                  shocks={"equity": -0.3, "global_equity": -0.3}, target_return=0.05,
                  asset_of={"US": "global_equity", "CN": "equity"},
                  incumbent_codes={"US", "CN"}, incumbent_weights={"US": 0.10, "CN": 0.30},
                  instrument_quality={}, max_whole_stress=1.0)
        lax = strategic.construct_strategic_portfolio(policy, **kw)            # 旧 fail-open
        self.assertEqual(lax["validation_status"], "passed")
        self.assertAlmostEqual(lax["instrument_allocation"]["US"], 0.20, places=6)
        strict = strategic.construct_strategic_portfolio(policy, require_quality=True, **kw)
        self.assertNotEqual(strict["validation_status"], "passed")            # US 封顶 0.10 → us 角色 0.20 无法满足
        self.assertTrue(any("quality data unavailable" in d for d in strict["selection_diagnostics"]))

    def test_unverified_non_incumbent_excluded_when_required(self):
        policy = {"roles": {"core": {"tier": "core", "members": ["KNOWN", "NEW"], "range": [1.0, 1.0]}}, "caps": {}}
        q = {"KNOWN": {"admission": {"admitted": True}, "score": {"total": 0.9, "coverage": 1.0}}}
        s = strategic.construct_strategic_portfolio(
            policy, returns={"equity": 0.07}, shocks={"equity": -0.3}, target_return=0.01,
            asset_of={"KNOWN": "equity", "NEW": "equity"}, exposure_of={"KNOWN": "k", "NEW": "n"},
            instrument_quality=q, require_quality=True, max_whole_stress=1.0)
        self.assertEqual(s["instrument_allocation"], {"KNOWN": 1.0})           # NEW 无记录 → 被剔除
        self.assertTrue(any("NEW" in d and "excluded" in d for d in s["selection_diagnostics"]))

    def test_single_satellite_cap_binds(self):
        # 批3：single_satellite_max 真的 binding——单成员卫星即便高收益、return_first 想要，也被压在 10%
        policy = {"roles": {
            "core": {"tier": "core", "members": ["C"], "range": [0.50, 0.95]},
            "sat":  {"tier": "satellite", "members": ["S"], "range": [0.00, 0.30]},
        }, "caps": {"single_satellite_max": 0.10, "satellite_max": 0.30}}
        s = strategic.construct_strategic_portfolio(
            policy, returns={"equity": 0.05, "global_growth": 0.30},
            shocks={"equity": -0.30, "global_growth": -0.40}, target_return=0.20,
            asset_of={"C": "equity", "S": "global_growth"}, max_whole_stress=1.0)
        self.assertEqual(s["validation_status"], "passed")
        sat_w = s["instrument_allocation"].get("S", 0.0)
        self.assertGreater(sat_w, 0.0)                 # 高收益卫星确实进入
        self.assertLessEqual(sat_w, 0.10 + 1e-9)       # 但被单卫星上限压住，没到 30%

    def test_role_floor_respected(self):
        # 批3：黄金/防御加下限后，即便 return_first 想全压权益，构建输出仍至少持有下限
        policy = {"roles": {
            "equity": {"tier": "core", "members": ["E"], "range": [0.50, 0.90]},
            "gold":   {"tier": "diversifier", "members": ["G"], "range": [0.05, 0.15]},
            "bond":   {"tier": "core_defensive", "members": ["B"], "range": [0.05, 0.40]},
        }, "caps": {}}
        s = strategic.construct_strategic_portfolio(
            policy, returns={"equity": 0.10, "gold": 0.02, "bond": 0.03},
            shocks={"equity": -0.30, "gold": -0.15, "bond": -0.03}, target_return=0.20,
            asset_of={"E": "equity", "G": "gold", "B": "bond"}, max_whole_stress=1.0)
        self.assertEqual(s["validation_status"], "passed")
        self.assertGreaterEqual(s["instrument_allocation"].get("G", 0.0), 0.05 - 1e-9)

    def test_final_metrics_come_from_final_allocation(self):
        s = self._run()
        actual_sat = sum(w for c, w in s["instrument_allocation"].items() if c in ("G1", "G2"))
        self.assertAlmostEqual(s["metrics"]["satellite_total"], actual_sat, places=9)

    def test_risk_currency_cap_excludes_defensive_bonds(self):
        policy = {"caps": {"single_risk_currency_exposure_max": 0.55}, "roles": {
            "bond": {"tier": "core_defensive", "members": ["B"], "range": [0.70, 0.70]},
            "equity": {"tier": "core", "members": ["E"], "range": [0.30, 0.30]},
        }}
        s = strategic.construct_strategic_portfolio(
            policy, returns={"bond": 0.03, "equity": 0.07}, shocks={"bond": -0.03, "equity": -0.30},
            target_return=0.01, asset_of={"B": "bond", "E": "equity"}, max_whole_stress=1.0)
        self.assertEqual(s["validation_status"], "passed")
        self.assertGreater(s["metrics"]["currency_exposure"]["CNY"], 0.55)
        self.assertLessEqual(s["metrics"]["risk_currency_exposure"]["CNY"], 0.55)

    def test_rationale_explains_roles_and_frozen_incumbent(self):
        # 「为什么这样配」：每个有权重的角色都有用途/区间说明；限购冻结的 incumbent 给出对应理由；卫星顶上限进 notes
        policy = {"caps": {"satellite_max": 0.10, "growth_factor_max": 0.10}, "roles": {
            "us_core_equity": {"tier": "core", "members": ["US"], "range": [0.10, 0.35]},
            "china_core_equity": {"tier": "core", "members": ["CN"], "range": [0.10, 0.50]},
            "growth_satellite": {"tier": "satellite", "members": ["S"], "range": [0.00, 0.10]},
            "government_bond": {"tier": "core_defensive", "members": ["B"], "range": [0.05, 0.60]},
        }}
        quality = {"US": {"admission": {"admitted": False, "blockers": ["purchase"]},
                          "score": {"total": 0.9, "coverage": 1.0}}}   # 标普类：限购
        snap = strategic.construct_strategic_portfolio(
            policy, returns={"global_equity": 0.08, "equity": 0.07, "global_growth": 0.30, "bond": 0.03},
            shocks={"global_equity": -0.3, "equity": -0.3, "global_growth": -0.4, "bond": -0.03},
            target_return=0.20, asset_of={"US": "global_equity", "CN": "equity", "S": "global_growth", "B": "bond"},
            instrument_quality=quality, incumbent_codes={"US"}, incumbent_weights={"US": 0.10},
            max_whole_stress=1.0)
        snap["construct_stress_budget"] = 0.30
        r = strategic.build_construct_rationale(
            policy, snap, name_of={"US": "标普500ETF", "CN": "沪深300ETF", "S": "纳指ETF", "B": "国债ETF"},
            quality=quality, incumbent_weights={"US": 0.10})
        self.assertIn("保守", r["objective"])
        self.assertIn("30%", r["objective"])                       # 压力预算写进目标说明
        by_role = {row["role"]: row for row in r["roles"]}
        self.assertEqual(set(by_role), {k for k, w in snap["policy_allocation"].items() if w > 0})
        for row in r["roles"]:
            self.assertTrue(row["purpose"])                        # 每个角色有中文用途
            self.assertTrue(row["band"])                           # 每个角色有"为什么这个比例"
        us_reason = by_role["us_core_equity"]["members"][0]["reason"]
        self.assertIn("限购", us_reason)                           # 冻结理由点名限购
        self.assertTrue(any("限购冻结" in n for n in r["notes"]))
        self.assertTrue(any("卫星" in n for n in r["notes"]))      # 卫星顶上限进 notes

    def test_rationale_empty_when_no_portfolio(self):
        r = strategic.build_construct_rationale({}, {"policy_allocation": {}}, name_of={})
        self.assertEqual(r["roles"], [])
        self.assertEqual(r["notes"], [])
        self.assertTrue(r["objective"])


class TestReturnIntervalsAndScenarios(unittest.TestCase):
    def test_intervals_from_haircut(self):
        a = signals.load_assumptions({})
        for asset, central in a["returns"].items():
            self.assertAlmostEqual(a["returns_conservative"][asset], round(central - 0.03, 6))
            self.assertAlmostEqual(a["returns_optimistic"][asset], round(central + 0.03, 6))

    def test_haircut_override_and_explicit(self):
        strat = {"assumptions": {"defaults": {"return_haircut": 0.05},
                                 "sleeves": {"equity": {"expected_return": 0.07, "return_conservative": 0.02}}}}
        a = signals.load_assumptions(strat)
        self.assertAlmostEqual(a["return_haircut"], 0.05)
        self.assertAlmostEqual(a["returns_conservative"]["equity"], 0.02)       # 显式优先于 haircut
        self.assertAlmostEqual(a["returns_optimistic"]["equity"], 0.12)         # 0.07 + 0.05

    def test_default_scenarios_seven(self):
        sc = signals.load_stress_scenarios({})
        self.assertEqual(len(sc), 7)
        self.assertTrue(all(s.get("name") and isinstance(s.get("shocks"), dict) for s in sc))
        self.assertTrue(any(s["name"] == "全球权益危机" for s in sc))

    def test_scenarios_override(self):
        sc = signals.load_stress_scenarios({"stress_scenarios": [{"name": "自定义", "shocks": {"equity": -0.2}}]})
        self.assertEqual(len(sc), 1)
        self.assertEqual(sc[0]["name"], "自定义")


class TestCovarianceRisk(unittest.TestCase):
    def test_shrinkage_basic_and_insufficient(self):
        a = [0.01, -0.02, 0.0, 0.015, -0.01] * 6      # 30 点
        b = [0.005, -0.01, 0.002, 0.008, -0.004] * 6
        cov = strategic.shrinkage_covariance({"B": b, "A": a})
        self.assertEqual(cov["labels"], ["A", "B"])    # 排序
        self.assertEqual(cov["obs"], 30)
        self.assertAlmostEqual(cov["matrix"][0][1], cov["matrix"][1][0])   # 对称
        self.assertIsNone(strategic.shrinkage_covariance({"A": a[:10], "B": b[:10]}))   # 不足 → None

    def test_portfolio_volatility_diagonal(self):
        cov = {"labels": ["A", "B"], "matrix": [[0.04, 0.0], [0.0, 0.04]]}
        v = strategic.portfolio_volatility(cov, {"A": 0.5, "B": 0.5}, annualize=52.0)
        self.assertAlmostEqual(v, (0.02 * 52) ** 0.5, places=6)

    def test_risk_contributions_equal_and_concentrated(self):
        cov = {"labels": ["A", "B"], "matrix": [[0.04, 0.0], [0.0, 0.04]]}
        eq = strategic.risk_contributions(cov, {"A": 0.5, "B": 0.5})
        self.assertAlmostEqual(eq["contributions"]["A"], 0.5)
        self.assertAlmostEqual(eq["effective_bets"], 2.0, places=2)        # 两个等额独立风险源
        conc = strategic.risk_contributions(cov, {"A": 0.9, "B": 0.1})
        self.assertLess(conc["effective_bets"], eq["effective_bets"])       # 集中 → 有效风险源更少

    def test_risk_contributions_zero_var_none(self):
        cov = {"labels": ["A"], "matrix": [[0.0]]}
        self.assertIsNone(strategic.risk_contributions(cov, {"A": 1.0}))


# ---------- WS5：rich 估值加仓的有界软化 ----------

class TestValuationDeceleration(unittest.TestCase):
    def _rich_add(self):
        return {"suggest": "add", "triggered": True, "actionable": True, "approx_amount": 6000, "deviation_pp": -6.0}

    def test_rich_add_softened_aggressive(self):
        row = self._rich_add()
        signals.decelerate_add(row, {"valuation": {"percentile": 0.80, "tag": "rich"}}, "进取")
        self.assertEqual(row["action_mode"], "缓建")
        self.assertEqual(row["soften_amount"], 3000)   # 6000*0.5
        self.assertEqual(row["approx_amount"], 6000)   # 原值不动
        self.assertTrue(row["actionable"])             # 不拦截

    def test_extreme_band_softens_more(self):
        row = self._rich_add()
        signals.decelerate_add(row, {"valuation": {"percentile": 0.95, "tag": "rich"}}, "进取")
        self.assertEqual(row["soften_amount"], round(6000 * 0.33))

    def test_profile_scaling_conservative_softens_more(self):
        a = self._rich_add(); signals.decelerate_add(a, {"valuation": {"percentile": 0.80, "tag": "rich"}}, "进取")
        b = self._rich_add(); signals.decelerate_add(b, {"valuation": {"percentile": 0.80, "tag": "rich"}}, "保守")
        self.assertLess(b["soften_amount"], a["soften_amount"])

    def test_cheap_neutral_not_softened(self):
        for tag, pct in (("cheap", 0.2), ("neutral", 0.5)):
            row = self._rich_add()
            signals.decelerate_add(row, {"valuation": {"percentile": pct, "tag": tag}}, "进取")
            self.assertNotIn("action_mode", row)

    def test_na_missing_not_softened(self):
        for sig in ({"valuation_na": True}, {"valuation_missing": {"available": False}}):
            row = self._rich_add()
            signals.decelerate_add(row, sig, "进取")
            self.assertNotIn("action_mode", row)

    def test_trim_hold_never_softened(self):
        for sg in ("trim", "hold"):
            row = {"suggest": sg, "triggered": sg == "trim", "approx_amount": 6000}
            signals.decelerate_add(row, {"valuation": {"percentile": 0.95, "tag": "rich"}}, "进取")
            self.assertNotIn("action_mode", row)


# ---------- Track B：战术配置纯函数（冻结向量 + 守恒 + 状态机）----------

class TestTacticalScore(unittest.TestCase):
    def test_frozen_nasdaq_vector(self):
        r = tactical.score_asset(s_trend=-0.60, s_mom_63=-0.50, s_mom_126=-0.30,
                                 valuation_percentile=0.85, valuation_reliability=0.80,
                                 price_cov=1.0, data_quality_multiplier=1.0)
        self.assertAlmostEqual(r["s_momentum"], -0.42, places=6)
        self.assertAlmostEqual(r["s_price"], -0.519, places=6)
        self.assertAlmostEqual(r["s_valuation"], -0.56, places=6)
        self.assertAlmostEqual(r["s_interaction"], -0.29064, places=5)
        self.assertAlmostEqual(r["s_raw"], -0.504364, places=5)
        self.assertAlmostEqual(r["coverage"], 0.94, places=6)
        self.assertEqual(round(r["effective_score"], 3), -0.380)          # §4.11 冻结向量
        rt = tactical.raw_tactical_weight(0.13, r["effective_score"], 0.35, 0.55)
        self.assertEqual(round(rt, 4), 0.1028)                            # 进取 beta_down=0.55
        self.assertIn("下跌接刀保护", r["guards"])

    def test_missing_valuation_not_amplified(self):
        r = tactical.score_asset(s_trend=0.40, s_mom_63=0.30, s_mom_126=0.20,
                                 valuation_percentile=None, valuation_reliability=0.0)
        self.assertEqual(r["s_valuation"], 0.0)
        self.assertEqual(r["s_interaction"], 0.0)
        self.assertFalse(r["valuation_available"])
        s_price = 0.55 * 0.40 + 0.45 * (0.6 * 0.30 + 0.4 * 0.20)
        self.assertAlmostEqual(r["s_raw"], 0.70 * s_price, places=6)       # 不被重归一放大
        self.assertAlmostEqual(r["coverage"], 0.70, places=6)

    def test_deterministic(self):
        kw = dict(s_trend=-0.6, s_mom_63=-0.5, s_mom_126=-0.3,
                  valuation_percentile=0.85, valuation_reliability=0.8)
        self.assertEqual(tactical.score_asset(**kw), tactical.score_asset(**kw))


class TestTacticalConstruct(unittest.TestCase):
    def test_frozen_construction_vector(self):
        out = tactical.construct_tactical_portfolio(
            strategic={"A": 0.40, "B": 0.30, "R": 0.30},
            bounded_targets={"A": 0.48, "B": 0.20},
            reserve_asset="R", reserve_bounds=(0.10, 0.35))
        self.assertTrue(out["ok"])
        self.assertFalse(out["fallback"])
        self.assertEqual(round(out["weights"]["A"], 6), 0.48)
        self.assertEqual(round(out["weights"]["B"], 6), 0.20)
        self.assertEqual(round(out["reserve"], 6), 0.32)          # B 释放 10pp，8pp 给 A、2pp 进 reserve
        self.assertEqual(round(out["cash"], 6), 0.0)
        self.assertAlmostEqual(sum(out["weights"].values()) + out["cash"], 1.0, places=9)

    def test_budget_shrinks_tilts(self):
        out = tactical.construct_tactical_portfolio(
            strategic={"A": 0.40, "B": 0.30, "R": 0.30},
            bounded_targets={"A": 0.48, "B": 0.20},
            reserve_asset="R", reserve_bounds=(0.10, 0.35), active_weight_budget=0.02)
        self.assertTrue(out["ok"])
        self.assertLessEqual(out["active_weight_budget_used"], 0.02 + 1e-6)
        self.assertAlmostEqual(sum(out["weights"].values()) + out["cash"], 1.0, places=9)

    def test_fallback_when_overallocated(self):
        out = tactical.construct_tactical_portfolio(
            strategic={"A": 0.60, "B": 0.35, "R": 0.05},
            bounded_targets={"A": 0.60, "B": 0.35},
            reserve_asset="R", reserve_bounds=(0.20, 0.35))   # reserve 下限挤不下 → 守恒失败
        self.assertFalse(out["ok"])
        self.assertTrue(out["fallback"])
        self.assertEqual(out["weights"], {"A": 0.60, "B": 0.35, "R": 0.05})  # 回退战略组合

    def test_sleeve_concentration_shrinks_to_cap(self):
        # 同 sleeve 两个权益都想加仓→超 sleeve 压力上限→收缩到 cap，且守恒
        out = tactical.construct_tactical_portfolio(
            strategic={"A": 0.20, "B": 0.20, "R": 0.60}, bounded_targets={"A": 0.30, "B": 0.30},
            reserve_asset="R", reserve_bounds=(0.10, 0.70),
            shocks={"A": -0.30, "B": -0.30, "R": -0.03},
            asset_of={"A": "equity", "B": "equity", "R": "bond"},
            max_sleeve_stress=0.45)
        self.assertTrue(out["ok"])
        self.assertFalse(out["fallback"])
        st = abs(0.20 * -0.30) + abs(0.20 * -0.30) + abs(0.60 * -0.03)
        cap = max(0.45 * st, abs(0.20 * -0.30) + abs(0.20 * -0.30))
        sleeve = abs(out["weights"]["A"] * -0.30) + abs(out["weights"]["B"] * -0.30)
        self.assertLessEqual(sleeve, cap + 1e-6)               # sleeve 压力贡献 ≤ 上限
        self.assertAlmostEqual(sum(out["weights"].values()) + out["cash"], 1.0, places=9)

    def test_no_risk_to_risk_reallocation(self):
        # B 减仓释放的资金不得自动加到 A 之外的风险资产 C（C 无需求时保持战略）
        out = tactical.construct_tactical_portfolio(
            strategic={"A": 0.30, "B": 0.30, "C": 0.10, "R": 0.30},
            bounded_targets={"A": 0.30, "B": 0.20, "C": 0.10},  # 只有 B 释放，无人加仓
            reserve_asset="R", reserve_bounds=(0.10, 0.45))
        self.assertEqual(round(out["weights"]["C"], 6), 0.10)   # C 不动
        self.assertEqual(round(out["weights"]["A"], 6), 0.30)   # A 不动
        self.assertEqual(round(out["reserve"], 6), 0.40)        # B 释放的 10pp 全进 reserve


class TestTacticalStateMachine(unittest.TestCase):
    def _run(self, scores):
        st = tactical.new_state()
        out = []
        for s in scores:
            st = tactical.next_tactical_state(st, s)
            out.append(st["state"])
        return out

    def test_enter_confirm_recover_neutral(self):
        self.assertEqual(self._run([0.30, 0.30, 0.05, 0.0, 0.0]),
                         ["positive_watch", "positive_active", "recovering", "recovering", "neutral"])

    def test_single_immediate_from_watch(self):
        self.assertEqual(self._run([0.70, 0.70]), ["positive_watch", "positive_active"])

    def test_extreme_reversal_skips_recovering(self):
        self.assertEqual(self._run([0.30, 0.30, -0.70]),
                         ["positive_watch", "positive_active", "negative_active"])

    def test_negative_path(self):
        self.assertEqual(self._run([-0.30, -0.30]), ["negative_watch", "negative_active"])

    def test_watch_fades_to_neutral(self):
        self.assertEqual(self._run([0.30, 0.0]), ["positive_watch", "neutral"])


class TestTacticalShadow(unittest.TestCase):
    def _assets(self):
        full = {"trend": 1, "mom_63": 1, "mom_126": 1}
        return [
            {"code": "511010", "asset": "bond", "strategic_weight": 0.35,
             "subsignals": {"s_trend": 0.0, "s_mom_63": 0.0, "s_mom_126": 0.0, "availability": full}, "shock": -0.03},
            {"code": "510300", "asset": "equity", "strategic_weight": 0.25,
             "subsignals": {"s_trend": 0.6, "s_mom_63": 0.5, "s_mom_126": 0.4, "availability": full},
             "valuation_percentile": 0.2, "valuation_reliability": 0.85, "data_quality_multiplier": 1.0, "shock": -0.30},
            {"code": "513500", "asset": "global_equity", "strategic_weight": 0.20,
             "subsignals": {"s_trend": 0.0, "s_mom_63": 0.0, "s_mom_126": 0.0, "availability": full},
             "valuation_percentile": None, "valuation_reliability": 0.0, "data_quality_multiplier": 1.0, "shock": -0.30},
            {"code": "513100", "asset": "global_growth", "strategic_weight": 0.20,
             "subsignals": {"s_trend": -0.7, "s_mom_63": -0.6, "s_mom_126": -0.5, "availability": full},
             "valuation_percentile": None, "valuation_reliability": 0.0, "data_quality_multiplier": 1.0, "shock": -0.40},
        ]

    def test_shadow_conserves_and_directional(self):
        out = tactical.compute_shadow(self._assets(), "进取", "511010", etf_share=0.6, max_whole_stress=0.20)
        self.assertEqual(set(out["diagnostics"]), {"511010", "510300", "513500", "513100"})
        self.assertAlmostEqual(sum(out["weights"].values()) + out["cash"], 1.0, places=6)
        self.assertGreaterEqual(out["diagnostics"]["510300"]["tactical_weight"], 0.25 - 1e-9)   # 强且便宜→加
        self.assertLessEqual(out["diagnostics"]["513100"]["tactical_weight"], 0.20 + 1e-9)        # 弱→减
        self.assertEqual(out["diagnostics"]["511010"].get("role"), "reserve")                    # reserve 不独立评分

    def test_state_persistence_advances(self):
        a = self._assets()
        out1 = tactical.compute_shadow(a, "进取", "511010", etf_share=0.6)
        self.assertEqual(out1["diagnostics"]["510300"]["state"], "positive_watch")
        prior = {c: d.get("state_after") for c, d in out1["diagnostics"].items() if d.get("state_after")}
        out2 = tactical.compute_shadow(a, "进取", "511010", etf_share=0.6, prior_states=prior)
        self.assertEqual(out2["diagnostics"]["510300"]["state"], "positive_active")   # 连续确认→激活

    def test_shadow_never_actionable(self):
        # 影子产出不含任何"可执行"字段（actionable / blocked_reasons）——它是只读
        out = tactical.compute_shadow(self._assets(), "平衡", "511010")
        for d in out["diagnostics"].values():
            self.assertNotIn("actionable", d)


class TestTacticalBacktestSkeleton(unittest.TestCase):
    def test_weekly_sim_runs_and_uses_tactical(self):
        import pandas as pd
        n = 300
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        px = pd.DataFrame({
            "511010": [100.0] * n,                                   # 债券（reserve）平
            "510300": [100.0 * (1.0 + 0.0008 * i) for i in range(n)],  # 权益上行
            "513100": [100.0 * (1.0 - 0.0005 * i) for i in range(n)],  # 成长下行
        }, index=dates)
        out = backtest.tactical_weekly_sim(
            px, {"511010": 0.34, "510300": 0.33, "513100": 0.33},
            {"511010": "bond", "510300": "equity", "513100": "global_growth"},
            "511010", {"511010": -0.03, "510300": -0.30, "513100": -0.40},
            profile="进取", warmup=250, step=5)
        self.assertGreater(out["tactical_final"], 0)
        self.assertGreater(out["static_final"], 0)
        self.assertGreaterEqual(out["rebalances"], 1)
        self.assertGreaterEqual(out["weeks"], 1)


class TestTacticalBacktestPhaseB(unittest.TestCase):
    def _panel(self):
        import pandas as pd
        n = 420
        dates = pd.date_range("2022-01-01", periods=n, freq="B")
        px = pd.DataFrame({
            "511010": [100.0] * n,                                    # 债券（reserve）平
            "510300": [100.0 * (1.0 + 0.0012 * i) for i in range(n)],   # 强上行→正向激活
            "513100": [100.0 * (1.0 - 0.0010 * i) for i in range(n)],   # 强下行→负向激活
            "513500": [100.0 * (1.0 + 0.0002 * i) for i in range(n)],   # 温和
        }, index=dates)
        return (px, {"511010": 0.34, "510300": 0.25, "513100": 0.20, "513500": 0.21},
                {"511010": "bond", "510300": "equity", "513100": "global_growth", "513500": "global_equity"},
                "511010", {"511010": -0.03, "510300": -0.30, "513100": -0.40, "513500": -0.30})

    def test_comparison_six_strategies(self):
        px, st, ao, res, sh = self._panel()
        rows = backtest.run_tactical_comparison(px, st, ao, res, sh, profile="进取", warmup=250)
        self.assertEqual({r["mode"] for r in rows}, {m for m, _ in backtest.TACTICAL_MODES})
        for r in rows:
            self.assertGreater(r["total_return"], -1)   # 净值有意义

    def test_state_ablation_differs(self):
        # 去状态机=无确认延迟，倾斜时点不同→净值不同（趋势期方向不单调，故只断"有别"）
        px, st, ao, res, sh = self._panel()
        _, mt = backtest.simulate_tactical(px, st, ao, res, sh, mode="tactical", profile="进取", warmup=250)
        _, mn = backtest.simulate_tactical(px, st, ao, res, sh, mode="no_state", profile="进取", warmup=250)
        self.assertNotEqual(round(mt["total"], 4), round(mn["total"], 4))

    def test_valuation_ablation_at_signal(self):
        # 估值消融在【信号层】必然有别（NAV 层可能被资金约束吸收）：便宜估值抬高 effective_score。
        import copy
        full = {"trend": 1, "mom_63": 1, "mom_126": 1}
        base = [{"code": "510300", "asset": "equity", "strategic_weight": 0.6, "data_quality_multiplier": 1.0,
                 "subsignals": {"s_trend": 0.6, "s_mom_63": 0.5, "s_mom_126": 0.4, "availability": full}, "shock": -0.30},
                {"code": "511010", "asset": "bond", "strategic_weight": 0.4,
                 "subsignals": {"s_trend": 0.0, "s_mom_63": 0.0, "s_mom_126": 0.0, "availability": full}, "shock": -0.03}]
        wv = copy.deepcopy(base); wv[0]["valuation_percentile"] = 0.2; wv[0]["valuation_reliability"] = 0.85
        ev_w = tactical.compute_shadow(wv, "进取", "511010", gate_by_state=False)["diagnostics"]["510300"]["effective_score"]
        ev_n = tactical.compute_shadow(base, "进取", "511010", gate_by_state=False)["diagnostics"]["510300"]["effective_score"]
        self.assertGreater(ev_w, ev_n)   # 便宜估值→更高战术分

    def test_cost_reduces_return(self):
        px, st, ao, res, sh = self._panel()
        _, lo = backtest.simulate_tactical(px, st, ao, res, sh, mode="tactical", profile="进取", warmup=250, cost_per_side=0.0)
        _, hi = backtest.simulate_tactical(px, st, ao, res, sh, mode="tactical", profile="进取", warmup=250, cost_per_side=0.01)
        self.assertGreater(lo["total"], hi["total"])

    def test_threshold_reduces_turnover(self):
        px, st, ao, res, sh = self._panel()
        _, fine = backtest.simulate_tactical(px, st, ao, res, sh, mode="tactical", profile="进取", warmup=250, min_rebal_turnover=0.0)
        _, coarse = backtest.simulate_tactical(px, st, ao, res, sh, mode="tactical", profile="进取", warmup=250, min_rebal_turnover=0.05)
        self.assertLessEqual(coarse["n_rebal"], fine["n_rebal"])

    def test_negative_only_clamps_up_tilts(self):
        upto = {"511010": [100.0] * 260, "510300": [100.0 * (1 + 0.0012 * i) for i in range(260)]}
        st, ao, sh = {"511010": 0.5, "510300": 0.5}, {"511010": "bond", "510300": "equity"}, {"511010": -0.03, "510300": -0.30}
        states, w = {}, {}
        for _ in range(3):                                    # 走到 positive_active
            w, _c, states = backtest._tactical_targets(upto, st, ao, "511010", sh, tactical.TACTICAL_DEFAULTS,
                                                       "进取", states, mode="negative_only")
        self.assertLessEqual(w["510300"], st["510300"] + 1e-9)   # 仅负向：强势资产不得超过战略

    def test_no_lookahead(self):
        import pandas as pd
        s = pd.Series([0.1, 0.2, 0.3], index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]))
        self.assertEqual(backtest._val_pct_at(s, pd.Timestamp("2024-01-02")), 0.2)   # 不取未来 0.3

    def test_val_reliability_grades_by_history(self):
        import pandas as pd
        idx = pd.date_range("2010-01-01", periods=2200, freq="B")
        s = pd.Series([0.5] * len(idx), index=idx)
        self.assertEqual(backtest._val_reliability_at(s, pd.Timestamp("2012-06-01")), 0.0)   # <3年→0
        self.assertEqual(backtest._val_reliability_at(s, pd.Timestamp("2017-06-01")), 0.85)  # ≥7年→0.85
        mid = backtest._val_reliability_at(s, pd.Timestamp("2015-01-01"))                    # 5年→(0,0.85)
        self.assertTrue(0.0 < mid < 0.85)

    def test_reproducible(self):
        px, st, ao, res, sh = self._panel()
        a, _ = backtest.simulate_tactical(px, st, ao, res, sh, mode="tactical", profile="进取", warmup=250)
        b, _ = backtest.simulate_tactical(px, st, ao, res, sh, mode="tactical", profile="进取", warmup=250)
        self.assertEqual([round(x, 8) for x in a], [round(x, 8) for x in b])

    def test_walk_forward_and_perturbation_run(self):
        px, st, ao, res, sh = self._panel()
        wf = backtest.walk_forward_tactical(px, st, ao, res, sh, folds=2, warmup=250, profile="进取")
        self.assertTrue(all("tactical_cagr" in r for r in wf))
        pert = backtest.perturb_params(px, st, ao, res, sh, warmup=250, profile="进取")
        self.assertTrue(all("tactical_maxdd" in r for r in pert))


class TestValuationReconstruction(unittest.TestCase):
    def test_point_in_time_monotone_up(self):
        import pandas as pd
        n = 400
        pe = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=n, freq="B").astype(str),
                           "pe": [10.0 + 0.05 * i for i in range(n)]})
        s = backtest.valuation_percentile_series(pe, lookback_years=1, min_obs=120)
        self.assertTrue(len(s) > 0)
        self.assertLess(len(s), n)              # 前 min_obs-1 个无窗口被跳过
        self.assertGreater(s.iloc[-1], 0.9)     # 单调升→当日 PE 是窗口最高→分位接近 1
        self.assertLess(s.iloc[-1], 1.0)        # 但 <1（不含未来）

    def test_point_in_time_monotone_down(self):
        import pandas as pd
        pe = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=300, freq="B").astype(str),
                           "pe": [100.0 - 0.1 * i for i in range(300)]})
        s = backtest.valuation_percentile_series(pe, lookback_years=1, min_obs=120)
        self.assertLess(s.iloc[-1], 0.05)       # 单调降→当日最低→分位≈0（无前视）

    def test_percentile_value(self):
        import pandas as pd
        pe = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=200, freq="B").astype(str),
                           "pe": [5, 15] * 100})
        s = backtest.valuation_percentile_series(pe, lookback_years=5, min_obs=10)
        self.assertAlmostEqual(s.iloc[-1], 0.5, delta=0.02)   # 末点=15，窗口半数(5)更小→分位≈0.5

    def test_build_proxy_panel_offline(self):
        pp = backtest.build_proxy_panel(valid_strategy(), {"511010": 0.5, "510300": 0.3, "518880": 0.2})
        if pp is None:
            self.skipTest("无指数代理种子（engine/data/idx_*.csv）")
        pxL, pt, pa, pbond = pp
        self.assertEqual(pbond, "sh000012")
        self.assertIn("sh000300", pxL.columns)
        self.assertAlmostEqual(sum(pt.values()), 1.0, places=6)

    def test_build_full_panel_total_return_offline(self):
        strat = {"universe": [{"code": "511010", "asset": "bond"}, {"code": "510300", "asset": "equity"},
                              {"code": "510500", "asset": "equity"}, {"code": "513500", "asset": "global_equity"},
                              {"code": "513100", "asset": "global_growth"}]}
        targets = {"511010": 0.1, "510300": 0.3, "510500": 0.2, "513500": 0.25, "513100": 0.15}
        fp = backtest.build_full_panel(strat, targets)
        if fp is None:
            self.skipTest("无 US/指数代理种子（idx_spx/idx_ixic 等）")
        pxF, ft, fa, fbond, drop = fp
        self.assertEqual(fbond, "sh000012")
        self.assertIn("spx", pxF.columns)         # 保留 QDII（标普）
        self.assertIn("ixic", pxF.columns)        # 保留 QDII（纳指）
        self.assertEqual(fa["spx"], "equity")
        self.assertAlmostEqual(sum(ft.values()), 1.0, places=6)

    def test_build_full_panel_bond_carry_zero_lowers_bond(self):
        # 批4(§0B #5-②)：bond_carry=0 → 债券代理累计全收益更低（证明票息开关生效，供 Calmar 敏感性）
        strat = {"universe": [{"code": "511010", "asset": "bond"}, {"code": "510300", "asset": "equity"},
                              {"code": "510500", "asset": "equity"}, {"code": "513500", "asset": "global_equity"}]}
        targets = {"511010": 0.4, "510300": 0.3, "510500": 0.2, "513500": 0.1}
        base = backtest.build_full_panel(strat, targets)
        zero = backtest.build_full_panel(strat, targets, bond_carry=0.0)
        if base is None or zero is None:
            self.skipTest("无全收益面板种子")
        bsym = base[3]
        self.assertLess(zero[0][bsym].iloc[-1], base[0][bsym].iloc[-1])

    def test_compute_return_intervals_vol_scaled_and_capped(self):
        # 批3收尾：折扣随波动缩放 + 成长桶保守值封顶在核心权益保守值（纯逻辑，mock 掉取数）
        strat = {"assumptions": {"defaults": {"return_haircut": 0.03}, "sleeves": {
            "equity": {"expected_return": 0.07}, "global_growth": {"expected_return": 0.10},
            "global_equity": {"expected_return": 0.08}, "china_growth": {"expected_return": 0.09}}}}
        volmap = {"sh000300": 0.24, "ixic": 0.20, "spx": 0.18}   # equity/纳指/标普

        def fake_vol(kind, ref, refresh=False):
            if kind == "etf_avg":
                return 0.32                                      # china_growth 高波
            return volmap.get(ref)

        with mock.patch.object(backtest, "_sleeve_vol", side_effect=fake_vol):
            ri = backtest.compute_return_intervals(strat)
        self.assertGreater(ri["china_growth"]["haircut"], ri["global_equity"]["haircut"])   # 波动越大折扣越大
        eq_cons = ri["equity"]["conservative"]
        self.assertLessEqual(ri["global_growth"]["conservative"], eq_cons + 1e-9)           # 成长保守封顶核心
        self.assertLessEqual(ri["china_growth"]["conservative"], eq_cons + 1e-9)
        self.assertGreater(ri["global_equity"]["conservative"], eq_cons)                    # 非成长(标普)不封顶、按波动
        for d in ri.values():
            self.assertLessEqual(d["conservative"], d["central"] + 1e-9)
            self.assertLessEqual(d["central"], d["optimistic"] + 1e-9)

    def test_simulate_strategic_comparison_honest_disclosures(self):
        # 批4(§0B #5)：可代理子集统一剔除并披露 + 零息 Calmar 敏感性 + 去退化重复基准
        root = backtest.find_repo_root(backtest.HERE)
        strat = backtest.load_yaml(os.path.join(root, "strategy.yaml"))
        port = backtest.load_yaml(os.path.join(root, "portfolio.yaml"))
        res = backtest.simulate_strategic_comparison(strat, port, root)
        if not res:
            self.skipTest("无全收益面板种子 / 无可行组合")
        # ① 未覆盖成长卫星(创业板/科创50)统一剔除并显式披露，不再静默再分配
        self.assertGreater(res["excluded_weight"]["权威构建"], 0)
        # ② 每行带零息 Calmar 敏感性；主表票息口径 +3%
        self.assertTrue(all("calmar_zero_coupon" in r for r in res["rows"]))
        self.assertAlmostEqual(res["bond_sensitivity"]["bond_carry"], 0.03, places=4)
        # ③ 去退化重复——被测组合两两不同（无字节相同基准）
        names = [r["name"] for r in res["rows"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertIsInstance(res["deduped"], list)
        # ④ 每个被比组合都返回实际配仓 + 名称（前端展示"各组合怎么配仓"）
        for n in names:
            self.assertIn(n, res["weights"])
            self.assertAlmostEqual(sum(res["weights"][n].values()), 1.0, places=2, msg=n)
        self.assertTrue(any(res["names"].values()))


class TestTacticalActions(unittest.TestCase):
    def _shadow(self, state, tac_w):
        return {"diagnostics": {"510300": {"state": state, "tactical_weight": tac_w},
                                "511010": {"role": "reserve", "strategic_weight": 0.3}}}

    def test_add_when_active_over_threshold(self):
        acts = tactical.tactical_actions(self._shadow("positive_active", 0.28), {"510300": 0.20}, 100000,
                                         min_trade=500, max_weekly=50000, strategic_weights={"510300": 0.25})
        a = next(x for x in acts if x["code"] == "510300")
        self.assertEqual(a["side"], "add")
        self.assertTrue(a["actionable"])
        self.assertAlmostEqual(a["approx_amount"], 8000, delta=1)

    def test_small_tilt_needs_active_but_structural_still_fires(self):
        # 小幅(2pp<5/25)倾斜：watch 态不激活、且未达结构门槛→拦截；active 态过战术门槛→可执行
        watch = next(x for x in tactical.tactical_actions(self._shadow("positive_watch", 0.27), {"510300": 0.25},
                     100000, min_trade=100, strategic_weights={"510300": 0.25}) if x["code"] == "510300")
        self.assertFalse(watch["actionable"])
        active = next(x for x in tactical.tactical_actions(self._shadow("positive_active", 0.27), {"510300": 0.25},
                      100000, min_trade=100, strategic_weights={"510300": 0.25}) if x["code"] == "510300")
        self.assertTrue(active["actionable"])
        self.assertEqual(active["trigger"], "tactical")

    def test_structural_fires_even_when_neutral(self):
        # 大幅(10pp≥5/25)偏离：即便状态中性，结构 5/25 仍触发（不被状态机吞掉）
        a = next(x for x in tactical.tactical_actions(self._shadow("neutral", 0.30), {"510300": 0.20},
                 100000, min_trade=500, strategic_weights={"510300": 0.30}) if x["code"] == "510300")
        self.assertTrue(a["actionable"])
        self.assertEqual(a["trigger"], "structural")

    def test_blocked_below_min_trade(self):
        a = next(x for x in tactical.tactical_actions(self._shadow("positive_active", 0.2509), {"510300": 0.25},
                 100000, min_trade=500, abs_thr_pp=0.01, strategic_weights={"510300": 0.25}) if x["code"] == "510300")
        self.assertFalse(a["actionable"])
        self.assertTrue(any("最小交易门槛" in r for r in a["blocked_reasons"]))

    def test_reserve_excluded(self):
        codes = [a["code"] for a in tactical.tactical_actions(self._shadow("positive_active", 0.28),
                 {"510300": 0.20}, 100000, strategic_weights={"510300": 0.25})]
        self.assertNotIn("511010", codes)

    def test_direction_current_to_tactical(self):
        a = next(x for x in tactical.tactical_actions(self._shadow("negative_active", 0.25), {"510300": 0.30},
                 100000, min_trade=500, strategic_weights={"510300": 0.25}) if x["code"] == "510300")
        self.assertEqual(a["side"], "trim")     # current 0.30 → tactical 0.25：减仓（方向只看 current→tactical）


class TestAdvisoryGate(unittest.TestCase):
    def _report(self, mode):
        return {"id": "phasec-test-cycle", "signals": {
            "signals": {"510300": {"name": "沪深300ETF"}, "513100": {"name": "纳指ETF"}},
            "actionable_rebalance": [{"code": "510300", "name": "沪深300ETF", "suggest": "add",
                                      "actionable": True, "triggered": True, "approx_amount": 1000}],
            "tactical": {"mode": mode, "actions": [{"code": "513100", "side": "add", "actionable": True,
                                                    "approx_amount": 2000, "state": "positive_active", "deviation_pp": 3}]}}}

    def test_shadow_uses_structural_only(self):
        sug = reports.cycle_suggestions(report=self._report("shadow"), executions=[])
        self.assertEqual({x["source"] for x in sug}, {"rebalance"})   # 影子绝不泄漏战术进可执行
        self.assertTrue(any(x["code"] == "510300" for x in sug))

    def test_advisory_uses_tactical(self):
        sug = reports.cycle_suggestions(report=self._report("advisory"), executions=[])
        self.assertEqual({x["source"] for x in sug}, {"tactical"})    # advisory 战术取代结构性
        self.assertTrue(any(x["code"] == "513100" for x in sug))


class TestTacticalConfig(unittest.TestCase):
    def test_load_defaults(self):
        cfg = tactical.load_tactical_config({})
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["mode"], "shadow")
        self.assertEqual(cfg["signals"]["deadband"], 0.20)

    def test_override_keeps_other_defaults(self):
        cfg = tactical.load_tactical_config(
            {"tactical_allocation": {"enabled": True, "signals": {"deadband": 0.1}}})
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["signals"]["deadband"], 0.1)
        self.assertEqual(cfg["signals"]["trend_scale"], 8.0)

    def test_validate(self):
        self.assertEqual(tactical.validate_tactical_config({}), [])
        self.assertTrue(any("mode" in e for e in
                            tactical.validate_tactical_config({"tactical_allocation": {"mode": "live"}})))


# ---------- WS3：真实业绩 TWR / MWR（剔除注入本金）----------

class TestPerformanceTracking(unittest.TestCase):
    def test_twr_excludes_injected_principal(self):
        navs = [{"as_of": "2026-01-01", "etf_value": 1000.0}, {"as_of": "2026-01-08", "etf_value": 1100.0}]
        out = reports.compute_twr(navs, [{"date": "2026-01-05", "amount": 100.0}])   # 注入 100、无市场涨跌
        self.assertTrue(out["available"])
        self.assertAlmostEqual(out["twr"], 0.0, places=6)        # 注入本金不被当收益

    def test_twr_links_subperiods(self):
        navs = [{"as_of": "2026-01-01", "etf_value": 1000.0}, {"as_of": "2026-01-08", "etf_value": 1100.0},
                {"as_of": "2026-01-15", "etf_value": 1210.0}]
        self.assertAlmostEqual(reports.compute_twr(navs, [])["twr"], 0.21, places=6)   # 1.1×1.1−1

    def test_mwr_known_irr(self):
        navs = [{"as_of": "2025-06-06", "etf_value": 1000.0}, {"as_of": "2026-06-06", "etf_value": 1100.0}]
        out = reports.compute_mwr(navs, [])
        self.assertTrue(out["available"])
        self.assertAlmostEqual(out["mwr"], 0.10, places=2)

    def test_mwr_contribution_not_counted_as_gain(self):
        navs = [{"as_of": "2025-06-06", "etf_value": 1000.0}, {"as_of": "2026-06-06", "etf_value": 2100.0}]
        out = reports.compute_mwr(navs, [{"date": "2025-12-06", "amount": 1000.0}])   # 期中加投 1000
        self.assertTrue(out["available"])
        self.assertLess(out["mwr"], 0.10)                        # 不把注入的 1000 当收益

    def test_cash_flows_signs(self):
        execs = [{"created_at": "2026-01-02", "items": [
            {"code": "510300", "status": "已执行", "side": "buy", "amount": 500, "fee": 1},
            {"code": "513100", "status": "已执行", "side": "sell", "amount": 300, "fee": 1}]}]
        flows, fee = reports.cash_flows_from_executions(execs)
        amts = {f["amount"] for f in flows}
        self.assertIn(500.0, amts)
        self.assertIn(-300.0, amts)
        self.assertEqual(fee, 2.0)

    def test_insufficient_snapshots_unavailable(self):
        self.assertFalse(reports.compute_twr([{"as_of": "2026-01-01", "etf_value": 1000}], [])["available"])
        self.assertFalse(reports.compute_mwr([], [])["available"])

    def test_zero_start_period_skipped(self):
        navs = [{"as_of": "2026-01-01", "etf_value": 0.0}, {"as_of": "2026-01-08", "etf_value": 100.0},
                {"as_of": "2026-01-15", "etf_value": 110.0}]
        out = reports.compute_twr(navs, [])
        self.assertTrue(out["available"])
        self.assertEqual(out["skipped"], 1)
        self.assertAlmostEqual(out["twr"], 0.10, places=6)

    def test_twr_subperiod_loss_over_100pct_no_crash(self):
        # 期内流入 > 期末市值 → 子区间亏损>100% → twr<-1；不得崩溃(复数幂)，年化记 -100%
        out = reports.compute_twr(
            [{"as_of": "2026-01-01", "etf_value": 1000.0}, {"as_of": "2026-01-08", "etf_value": 500.0}],
            [{"date": "2026-01-05", "amount": 1500.0}])
        self.assertTrue(out["available"])
        self.assertLess(out["twr"], -1.0)
        self.assertEqual(out["annualized"], -1.0)

    def test_nav_snapshot_roundtrip_and_same_day_overwrite(self):
        d = tempfile.mkdtemp()
        orig = reports.NAV_DIR
        reports.NAV_DIR = d
        try:
            snap = reports.save_nav_snapshot({"generated_for": "2026-06-06", "portfolio_value": 31000,
                                              "cash": 14000, "data_quality": "完整", "signals": {}})
            self.assertEqual(snap["etf_value"], 17000.0)
            self.assertEqual(len(reports.load_nav_series()), 1)
            reports.save_nav_snapshot({"generated_for": "2026-06-06", "portfolio_value": 32000,
                                       "cash": 14000, "signals": {}})
            self.assertEqual(len(reports.load_nav_series()), 1)   # 同日覆盖→仍 1 条
        finally:
            reports.NAV_DIR = orig
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
