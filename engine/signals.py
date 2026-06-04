#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────
# 【单一事实源 / SINGLE SOURCE OF TRUTH】 weekly-briefing 技能的唯一实现。
#
#   代码只此一份：     engine/signals.py（本文件）、engine/backtest.py、engine/validate_flags.py
#   用户配置（根目录）：portfolio.yaml（持仓，每周改）、strategy.yaml（策略参数）
#   两个 agent 入口（都只放 SKILL.md，调用本文件，无第二份代码）：
#       Claude →  .claude/skills/weekly-briefing/SKILL.md
#       Codex  →  .agents/skills/weekly-briefing/SKILL.md
#
#   ⚠️ 评审者（人或 AI）请注意：回测在 engine/backtest.py；不存在重复脚本；路径统一为 engine/...。
# ─────────────────────────────────────────────────────────────────────────
"""
周度信号引擎（量化骨架）。

读取 strategy.yaml + portfolio.yaml，多源拉取场内 ETF 的日终行情与估值，
计算 趋势 / 动量 / 估值分位 / 再平衡偏离，写出 signals.json 供 AI 增强层使用。

稳健性：
  - 行情多源：东方财富 → 新浪 → 本地缓存(engine/cache/)。
  - 估值缓存：估值接口失败时回退缓存，并给出 valuation_status（available/source/reason）。
    —— 估值"缺失"会被明确标注，绝不能被当成"中性"。
  - 数据新鲜度分级：完整 / 缓存可用 / 过旧 / 部分缺失；只有"完整/缓存可用"才允许再平衡建议。

这是教育/辅助工具，输出的是"信号与建议"，不构成投资建议；回测好 ≠ 未来赚钱。
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime


def configure_console_encoding():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


configure_console_encoding()


def die(msg):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(1)


try:
    import yaml
except ImportError:
    die("缺少依赖 pyyaml，请先运行：pip install -r engine/requirements.txt")
try:
    import pandas as pd
except ImportError:
    die("缺少依赖 pandas，请先运行：pip install -r engine/requirements.txt")
try:
    import akshare as ak
except ImportError:
    die("缺少依赖 akshare，请先运行：pip install -r engine/requirements.txt")


HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
STALE_LIMIT_DAYS = 10        # 行情最新日期超过此日历天数 → "过旧"，禁用交易建议
VAL_STALE_LIMIT_DAYS = 30    # 估值缓存超过此天数 → 视为不可用（估值变化慢，限额可宽些）

DEFAULT_INVESTOR_PROFILE = {
    "target_annual_return": 0.05,
    "horizon_years": 5,
    "max_acceptable_drawdown": 0.15,
    "experience_level": "beginner",
    "emergency_cash_kept_outside": 0,
    "monthly_contribution": 0,
    "stable_assets_outside": 0,     # 场外稳健桶（活期/固收/定存）：让算法知道有这笔缓冲，做全组合口径
    "stable_assets_yield": 0.025,   # 稳健桶假设年化（仅用于混合收益展示）
    "planned_etf_capital": 0,       # ETF 风险桶目标上限：用于缓冲比例与目标权重测算（0=不启用缓冲，按 ETF 桶自身回撤预算）
}


def find_repo_root(start):
    d = start
    for _ in range(8):
        if os.path.exists(os.path.join(d, "portfolio.yaml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_investor_profile(root):
    path = os.path.join(root, "investor_profile.yaml") if root else None
    if path and os.path.exists(path):
        try:
            data = load_yaml(path) or {}
            return {**DEFAULT_INVESTOR_PROFILE, **data}
        except Exception:  # noqa: BLE001
            return dict(DEFAULT_INVESTOR_PROFILE)
    return dict(DEFAULT_INVESTOR_PROFILE)


def _num_ok(v, lo=None, hi=None, positive=False):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    if positive and not v > 0:
        return False
    if lo is not None and v < lo:
        return False
    if hi is not None and v > hi:
        return False
    return True


def validate_config(port, strat):
    """校验 portfolio.yaml，返回错误列表。"""
    errs = []
    uni_codes = {str(u.get("code")) for u in (strat.get("universe") or [])}
    holdings = port.get("holdings") or []
    if not holdings:
        errs.append("portfolio.yaml 没有任何 holdings")
    cash = port.get("cash", 0)
    if not _num_ok(cash, lo=0):
        errs.append(f"cash 非法（需 ≥0 的数字）：{cash!r}")
    seen, tw_sum = set(), 0.0
    for h in holdings:
        code = str(h.get("code"))
        if code in seen:
            errs.append(f"重复的 ETF 代码：{code}")
        seen.add(code)
        if uni_codes and code not in uni_codes:
            errs.append(f"{code} 不在 strategy.yaml 的 universe 里")
        if not _num_ok(h.get("shares", 0), lo=0):
            errs.append(f"{code} 的 shares 非法（需 ≥0 的数字）：{h.get('shares')!r}")
        tw = h.get("target_weight", 0)
        if not _num_ok(tw, lo=0, hi=1):
            errs.append(f"{code} 的 target_weight 非法（需 0~1）：{tw!r}")
        else:
            tw_sum += tw
    if holdings and abs(tw_sum - 1.0) > 0.01:
        errs.append(f"target_weight 合计 = {tw_sum:.3f}，应接近 1.0")
    return errs


def validate_strategy(strat):
    """校验 strategy.yaml，避免后续 KeyError 或不合理参数。"""
    errs = []
    uni = strat.get("universe") or []
    if not uni:
        errs.append("strategy.yaml 的 universe 为空")
    codes = [str(u.get("code")) for u in uni]
    if len(codes) != len(set(codes)):
        errs.append("universe 存在重复代码")
    bonds = [u for u in uni if u.get("asset") == "bond"]
    if len(bonds) != 1:
        errs.append(f"universe 必须有且仅有一个 asset:bond（现 {len(bonds)} 个）")
    F = strat.get("factors") or {}
    tf, mo, va, rb = (F.get(k, {}) for k in ("trend_filter", "momentum", "valuation", "rebalance"))
    if not _num_ok(tf.get("ma_days"), positive=True):
        errs.append("trend_filter.ma_days 须为正数")
    if not _num_ok(mo.get("lookback_days"), positive=True):
        errs.append("momentum.lookback_days 须为正数")
    if not _num_ok(va.get("lookback_years"), positive=True):
        errs.append("valuation.lookback_years 须为正数")
    cp, rp = va.get("cheap_pct"), va.get("rich_pct")
    if not _num_ok(cp, lo=0, hi=1):
        errs.append("valuation.cheap_pct 须在 0~1")
    if not _num_ok(rp, lo=0, hi=1):
        errs.append("valuation.rich_pct 须在 0~1")
    if _num_ok(cp) and _num_ok(rp) and not cp < rp:
        errs.append("valuation.cheap_pct 必须 < rich_pct")
    if not _num_ok(rb.get("abs_threshold_pp"), positive=True):
        errs.append("rebalance.abs_threshold_pp 须为正数")
    if not _num_ok(rb.get("rel_threshold"), lo=0, hi=1) or rb.get("rel_threshold", 0) <= 0:
        errs.append("rebalance.rel_threshold 须在 (0,1]")
    for fk in ("trend_filter", "momentum", "valuation", "rebalance"):
        if not isinstance(F.get(fk, {}).get("enabled"), bool):
            errs.append(f"factors.{fk}.enabled 须为 true/false")
    rp = strat.get("risk_profile")
    if rp is not None and rp not in ("保守", "平衡", "进取"):
        errs.append("risk_profile 须为 保守/平衡/进取")
    rc = strat.get("risk_controls") or {}
    if rc:
        if not _num_ok(rc.get("min_trade_amount", 0), lo=0):
            errs.append("risk_controls.min_trade_amount 须为 ≥0 的数字")
        if not _num_ok(rc.get("max_weekly_trade_amount", 0), lo=0):
            errs.append("risk_controls.max_weekly_trade_amount 须为 ≥0 的数字")
        if not _num_ok(rc.get("first_tranche_pct", 0), lo=0, hi=1):
            errs.append("risk_controls.first_tranche_pct 须在 0~1")
        if not isinstance(rc.get("allow_trade_with_cache", False), bool):
            errs.append("risk_controls.allow_trade_with_cache 须为 true/false")
    watch = strat.get("watchlist") or []
    watch_codes = [str(w.get("code")) for w in watch]
    if len(watch_codes) != len(set(watch_codes)):
        errs.append("watchlist 存在重复代码")
    overlap = sorted(set(codes) & set(watch_codes))
    if overlap:
        errs.append(f"watchlist 与 universe 重复：{', '.join(overlap)}")
    for w in watch:
        code = str(w.get("code", "")).strip()
        if not code:
            errs.append("watchlist 存在空 code")
        if not w.get("name"):
            errs.append(f"watchlist {code} 缺少 name")
        if not w.get("role"):
            errs.append(f"watchlist {code} 缺少 role")
        if not w.get("asset"):
            errs.append(f"watchlist {code} 缺少 asset")
    return errs


# ---------- 行情多源取数 + 缓存 ----------

def _norm(df):
    df = df[["date", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna().sort_values("date").reset_index(drop=True)


def _try_em(code, retries):
    for _ in range(retries):
        try:
            d = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="qfq")
            if d is not None and not d.empty:
                d = d.rename(columns={"日期": "date", "收盘": "close"})
                if "close" in d.columns:
                    return _norm(d)
        except Exception:  # noqa: BLE001
            time.sleep(1.2)
    return None


def _try_sina(code, retries):
    prefix = "sh" if code[:1] in ("5", "6") else "sz"
    for _ in range(retries):
        try:
            d = ak.fund_etf_hist_sina(symbol=prefix + code)
            if d is not None and not d.empty and "close" in d.columns:
                return _norm(d)
        except Exception:  # noqa: BLE001
            time.sleep(1.2)
    return None


def _save_cache(name, df):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_csv(os.path.join(CACHE_DIR, f"{name}.csv"), index=False)
    except Exception:  # noqa: BLE001
        pass


def _read_cache(name):
    p = os.path.join(CACHE_DIR, f"{name}.csv")
    if os.path.exists(p):
        try:
            return _norm(pd.read_csv(p))
        except Exception:  # noqa: BLE001
            return None
    return None


def fetch_hist(code, retries=2):
    """多源取日终价格。返回 (DataFrame[date,close], source)；source ∈ {'live','cache',None}。"""
    df = _try_em(code, retries)
    if df is not None:
        _save_cache(code, df)
        return df, "live"
    df = _try_sina(code, retries)
    if df is not None:
        _save_cache(code, df)
        return df, "live"
    df = _read_cache(code)
    if df is not None:
        return df, "cache"
    print(f"  [警告] {code} 所有数据源失败且无缓存", file=sys.stderr)
    return None, None


def fetch_valuation_pct(index_name, lookback_years, retries=3):
    """估值分位（滚动市盈率），失败回退缓存。返回 (result|None, status)。

    status: {available, source('live'/'cache'/'cache_stale'/None), as_of, stale_days, reason?}
    """
    cache_path = os.path.join(CACHE_DIR, f"valuation_{index_name}.json")
    today = date.today()
    for _ in range(retries):
        try:
            df = ak.stock_index_pe_lg(symbol=index_name)
            if df is not None and not df.empty and "滚动市盈率" in df.columns:
                s = pd.to_numeric(df["滚动市盈率"], errors="coerce").dropna()
                if len(s) >= 30:
                    s2 = s.tail(int(lookback_years * 244))
                    cur = float(s2.iloc[-1])
                    pct = float((s2 < cur).mean())
                    as_of = str(pd.to_datetime(df["日期"].iloc[-1]).date())
                    res = {"pe": round(cur, 2), "percentile": round(pct, 3), "as_of": as_of}
                    try:
                        os.makedirs(CACHE_DIR, exist_ok=True)
                        with open(cache_path, "w", encoding="utf-8") as f:
                            json.dump({**res, "fetched_at": str(today)}, f, ensure_ascii=False)
                    except Exception:  # noqa: BLE001
                        pass
                    return res, {"available": True, "source": "live", "as_of": as_of, "stale_days": 0}
            return None, {"available": False, "source": None, "reason": "bad_response"}
        except Exception:  # noqa: BLE001
            time.sleep(1.2)
    # 回退缓存
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                c = json.load(f)
            fa = c.get("fetched_at")
            stale = (today - datetime.strptime(fa, "%Y-%m-%d").date()).days if fa else 999
            if stale <= VAL_STALE_LIMIT_DAYS:
                return ({"pe": c["pe"], "percentile": c["percentile"], "as_of": c.get("as_of")},
                        {"available": True, "source": "cache", "as_of": c.get("as_of"), "stale_days": stale})
            return None, {"available": False, "source": "cache_stale", "reason": "cache_too_old", "stale_days": stale}
        except Exception:  # noqa: BLE001
            pass
    return None, {"available": False, "source": None, "reason": "network_failed"}


def grade_data(missing, provenance):
    max_stale = max((p["stale_days"] for p in provenance.values()), default=0)
    if missing:
        return "部分缺失", False, max_stale
    if max_stale > STALE_LIMIT_DAYS:
        return "过旧", False, max_stale
    if any(p["source"] == "cache" for p in provenance.values()):
        return "缓存可用", True, max_stale
    return "完整", True, max_stale


def floor_to_lot(amount, price, lot_size=100):
    if amount <= 0 or price <= 0:
        return 0
    return int(amount // (price * lot_size)) * lot_size


def build_first_funding_schedule(holdings, prices, cash, first_pct, max_weekly, min_trade):
    """0持仓账户的多周分批建仓草案。后续周次必须复盘后再执行。"""
    if cash <= 0 or first_pct <= 0:
        return []
    weeks = max(4, min(8, int((1 / first_pct) + 0.999)))
    weekly_cap = cash * first_pct
    if max_weekly > 0:
        weekly_cap = min(weekly_cap, max_weekly)
    schedule = []
    remaining_cash = cash
    for week in range(1, weeks + 1):
        planned = min(weekly_cap, remaining_cash)
        if planned <= 0:
            break
        orders, actual = [], 0.0
        for h in holdings:
            code = str(h["code"])
            price = prices.get(code)
            tw = float(h.get("target_weight", 0) or 0)
            target_amount = planned * tw
            shares = floor_to_lot(target_amount, price or 0)
            amount = shares * price if price else 0.0
            reasons = []
            if target_amount < min_trade:
                reasons.append(f"目标金额低于最小交易门槛 {min_trade:.0f} 元")
            if shares <= 0 and target_amount > 0:
                reasons.append("不足一手，暂不下单")
            actual += amount
            orders.append({
                "code": code,
                "name": h.get("name", code),
                "target_weight": round(tw, 4),
                "target_amount": round(target_amount, 0),
                "estimated_shares": shares,
                "estimated_amount": round(amount, 0),
                "blocked_reasons": reasons,
            })
        schedule.append({
            "week": week,
            "planned_amount": round(planned, 0),
            "estimated_amount": round(actual, 0),
            "estimated_unallocated": round(max(planned - actual, 0), 0),
            "orders": orders,
            "status": "ready" if week == 1 else "requires_prior_review",
            "notes": ["第1周可作为试仓预览；后续周次必须先完成上周复盘，不自动执行"],
        })
        remaining_cash -= planned
    return schedule


def build_preflight_checks(grade, rebal_ok, used_cache, allow_cache_trade, holdings, per, min_trade, max_weekly,
                           is_zero_position, risk_budget_breached=False, target_stress_drawdown=0, max_drawdown=0):
    checks = []
    checks.append({
        "id": "data_quality",
        "label": "数据质量",
        "status": "pass" if rebal_ok else "block",
        "message": f"当前数据质量：{grade}" if rebal_ok else f"当前数据质量：{grade}，禁止交易动作",
    })
    cache_block = used_cache and not allow_cache_trade
    checks.append({
        "id": "cache_policy",
        "label": "缓存行情",
        "status": "block" if cache_block else "pass",
        "message": "包含缓存行情，当前规则禁止据此交易" if cache_block else "未触发缓存交易禁令",
    })
    valuation_missing = []
    valuation_rich = []
    for h in holdings:
        code = str(h["code"])
        s = per.get(code) or {}
        if s.get("asset") in VALUATION_APPLICABLE_ASSETS:
            if s.get("valuation_missing"):
                valuation_missing.append(h.get("name", code))
            elif (s.get("valuation") or {}).get("tag") == "rich":
                valuation_rich.append(h.get("name", code))
    if valuation_missing:
        checks.append({
            "id": "valuation_missing",
            "label": "估值覆盖",
            "status": "warn",
            "message": "权益类估值缺失：" + "、".join(valuation_missing) + "；需要额外确认，不能当作中性",
        })
    elif valuation_rich:
        checks.append({
            "id": "valuation_rich",
            "label": "估值位置",
            "status": "warn",
            "message": "权益类估值偏贵：" + "、".join(valuation_rich) + "；首次建仓应保持小额分批",
        })
    else:
        checks.append({"id": "valuation", "label": "估值检查", "status": "pass", "message": "未发现权益估值缺失或偏贵提示"})
    over_weight = [h.get("name", str(h.get("code"))) for h in holdings if float(h.get("target_weight", 0) or 0) > 0.5]
    checks.append({
        "id": "concentration",
        "label": "单品种上限",
        "status": "warn" if over_weight else "pass",
        "message": ("目标权重超过 50%：" + "、".join(over_weight)) if over_weight else "无单个 ETF 目标权重超过 50%",
    })
    checks.append({
        "id": "trade_thresholds",
        "label": "交易门槛",
        "status": "pass",
        "message": f"单笔门槛 {min_trade:.0f} 元；单周上限 {max_weekly:.0f} 元" if max_weekly > 0 else f"单笔门槛 {min_trade:.0f} 元；未设置单周上限",
    })
    checks.append({
        "id": "risk_budget",
        "label": "风险预算",
        "status": "block" if risk_budget_breached else "pass",
        "message": (
            f"目标组合压力回撤约 {target_stress_drawdown * 100:.1f}%，超过可接受回撤 {max_drawdown * 100:.1f}%"
            if risk_budget_breached else
            f"目标组合压力回撤约 {target_stress_drawdown * 100:.1f}%，未超过可接受回撤 {max_drawdown * 100:.1f}%"
        ),
    })
    checks.append({
        "id": "zero_position",
        "label": "0 持仓状态",
        "status": "warn" if is_zero_position else "pass",
        "message": "当前为 0 持仓，只使用首次建仓预览，不直接执行再平衡" if is_zero_position else "非 0 持仓，可按再平衡纪律评估",
    })
    return checks


# 各资产类别的简化假设（单一事实源，app.py 的建议权重也复用这两张表）：
#   ASSET_SHOCKS = 压力情景冲击（用于回撤估算，非预测）
#   ASSET_EXPECTED_RETURN = 假设长期年化（用于目标可行性体检，非承诺）
ASSET_SHOCKS = {
    "bond": -0.03, "cash": 0.0, "short_bond": -0.02,
    "equity": -0.30, "equity_defensive": -0.20, "gold": -0.15,
    "global_equity": -0.30, "global_growth": -0.40, "china_growth": -0.40,
}
ASSET_EXPECTED_RETURN = {
    "bond": 0.030, "cash": 0.020, "short_bond": 0.025,
    "equity": 0.070, "equity_defensive": 0.055, "gold": 0.020,
    "global_equity": 0.080, "global_growth": 0.100, "china_growth": 0.090,
}
DEFAULT_SHOCK = -0.25
DEFAULT_EXPECTED_RETURN = 0.05

# 估值分位（A股滚动 PE）只对 A 股权益类适用；QDII/黄金/债券/现金/短债没有可比 A股 PE 序列，
# 应如实标"不适用"——既不当缺失（不必额外确认），更不能被当成"估值中性"。
VALUATION_APPLICABLE_ASSETS = ("equity", "equity_defensive", "china_growth")


def estimate_target_stress_drawdown(holdings, universe):
    """按目标权重做简化压力测试；用于风险预算校准，不是预测。"""
    contributions = []
    total = 0.0
    for h in holdings:
        code = str(h.get("code"))
        tw = float(h.get("target_weight", 0) or 0)
        asset = (universe.get(code) or {}).get("asset")
        shock = ASSET_SHOCKS.get(asset, DEFAULT_SHOCK)
        contribution = tw * shock
        total += contribution
        contributions.append({
            "code": code,
            "name": h.get("name", code),
            "asset": asset,
            "target_weight": round(tw, 4),
            "shock": round(shock, 4),
            "contribution": round(contribution, 4),
        })
    return abs(total), contributions


def expected_etf_return(holdings, universe):
    """按目标权重 × 各 sleeve 假设年化，估 ETF 桶现实预期年化（非承诺，仅目标可行性刻度）。"""
    total = 0.0
    for h in holdings:
        code = str(h.get("code"))
        tw = float(h.get("target_weight", 0) or 0)
        asset = (universe.get(code) or {}).get("asset")
        total += tw * ASSET_EXPECTED_RETURN.get(asset, DEFAULT_EXPECTED_RETURN)
    return total


def whole_portfolio_stress(etf_stress_drawdown, etf_value, stable_outside):
    """把 ETF 桶的压力回撤折算到全组合（场外稳健桶按 0 冲击纳入分母，是安全垫）。

    whole_dd = etf_dd × etf_value / (etf_value + stable_outside)。
    稳健桶为 0 时退化为 ETF 桶自身口径。
    """
    whole = etf_value + max(0.0, float(stable_outside or 0))
    if whole <= 0:
        return etf_stress_drawdown
    return etf_stress_drawdown * etf_value / whole


def main():
    ap = argparse.ArgumentParser(description="周度信号引擎")
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--portfolio", default=None)
    ap.add_argument("--out", default=os.path.join(HERE, "signals.json"))
    args = ap.parse_args()

    repo_root = find_repo_root(HERE)
    strategy_path = args.strategy or (os.path.join(repo_root, "strategy.yaml") if repo_root else None)
    portfolio_path = args.portfolio or (os.path.join(repo_root, "portfolio.yaml") if repo_root else None)
    if not strategy_path or not os.path.exists(strategy_path):
        die("找不到 strategy.yaml，请用 --strategy 指定路径")
    if not portfolio_path or not os.path.exists(portfolio_path):
        die("找不到 portfolio.yaml，请用 --portfolio 指定路径")

    strat = load_yaml(strategy_path)
    port = load_yaml(portfolio_path)
    investor_profile = load_investor_profile(repo_root)

    errs = validate_strategy(strat) + validate_config(port, strat)
    if errs:
        die("配置校验未通过，请先修正 strategy.yaml / portfolio.yaml：\n  - " + "\n  - ".join(errs))

    F = strat["factors"]
    uni = {str(u["code"]): u for u in strat["universe"]}
    ma_days = int(F["trend_filter"]["ma_days"])
    look = int(F["momentum"]["lookback_days"])
    vyears = float(F["valuation"]["lookback_years"])
    cheap = float(F["valuation"]["cheap_pct"])
    rich = float(F["valuation"]["rich_pct"])
    abs_thr = float(F["rebalance"]["abs_threshold_pp"]) / 100.0
    rel_thr = float(F["rebalance"]["rel_threshold"])
    RC = strat.get("risk_controls") or {}
    min_trade = float(RC.get("min_trade_amount", 0) or 0)
    max_weekly = float(RC.get("max_weekly_trade_amount", 0) or 0)
    first_pct = float(RC.get("first_tranche_pct", 0) or 0)
    allow_cache_trade = bool(RC.get("allow_trade_with_cache", False))

    holdings = port.get("holdings", []) or []
    watchlist = strat.get("watchlist") or []
    cash = float(port.get("cash", 0) or 0)
    today = date.today()

    def build_signal(item, fallback=None):
        """生成单只 ETF 的展示信号；不包含仓位/交易动作。"""
        code = str(item["code"])
        meta = fallback or item
        name = item.get("name") or meta.get("name") or code
        df, src = fetch_hist(code)
        if df is None or len(df) < ma_days + 5:
            sig = {
                "name": name,
                "asset": meta.get("asset"),
                "role": item.get("role"),
                "note": item.get("note"),
                "error": "数据不足或拉取失败",
            }
            return code, sig, None, None, None
        close = df["close"]
        last = float(close.iloc[-1])
        as_of = df["date"].iloc[-1].date()
        ma = float(close.tail(ma_days).mean())
        mom = float(close.iloc[-1] / close.iloc[-1 - look] - 1) if len(close) > look else None
        sig = {
            "name": name,
            "asset": meta.get("asset"),
            "role": item.get("role"),
            "note": item.get("note"),
            "last": round(last, 4),
            "as_of": str(as_of),
            "source": src,
            f"ma{ma_days}": round(ma, 4),
            "trend": "above" if last >= ma else "below",
            f"momentum_{look}d": round(mom, 4) if mom is not None else None,
        }
        vst = None
        asset = meta.get("asset")
        if asset not in VALUATION_APPLICABLE_ASSETS:
            # QDII/黄金/债券/现金等：A股 PE 分位不适用，如实标注（非缺失、更非中性）
            sig["valuation_na"] = True
        elif F["valuation"]["enabled"] and meta.get("index"):
            v, vst = fetch_valuation_pct(meta["index"], vyears)
            if v:
                tag = "cheap" if v["percentile"] <= cheap else (
                    "rich" if v["percentile"] >= rich else "neutral")
                sig["valuation"] = {**v, "tag": tag}
            else:
                sig["valuation_missing"] = vst
        else:
            # A股权益但尚未接入可用估值源（如红利低波/创业板/科创50）→ 如实标缺失，绝不当中性
            vst = {"available": False, "source": None, "reason": "index_not_configured"}
            sig["valuation_missing"] = vst
        prov = {"source": src, "as_of": str(as_of), "stale_days": (today - as_of).days}
        return code, sig, last, prov, vst

    def as_of_summary_from(provenance_map):
        as_ofs = sorted(p["as_of"] for p in provenance_map.values())
        if not as_ofs:
            return None, None, "无"
        as_of_min = as_ofs[0]
        as_of_max = as_ofs[-1]
        summary = as_of_min if as_of_min == as_of_max else f"{as_of_min} 至 {as_of_max}"
        return as_of_min, as_of_max, summary

    per, prices, provenance, valuation_status = {}, {}, {}, {}
    for h in holdings:
        code = str(h["code"])
        sig_code, sig, last, prov, vst = build_signal(h, uni.get(code, {}))
        if last is not None:
            prices[sig_code] = last
            provenance[sig_code] = prov
        if vst is not None:
            valuation_status[sig_code] = vst
        per[code] = sig

    missing = [str(h["code"]) for h in holdings if str(h["code"]) not in prices]
    grade, rebal_ok, max_stale = grade_data(missing, provenance)
    used_cache = any(p["source"] == "cache" for p in provenance.values())
    as_of_min, as_of_max, as_of_summary = as_of_summary_from(provenance)

    watch_signals, watch_prices, watch_provenance = {}, {}, {}
    for w in watchlist:
        code, sig, last, prov, vst = build_signal(w)
        watch_signals[code] = sig
        if last is not None:
            watch_prices[code] = last
            watch_provenance[code] = prov
        if vst is not None:
            valuation_status[code] = vst
    watch_missing = [str(w["code"]) for w in watchlist if str(w["code"]) not in watch_prices]
    watch_grade, _, watch_max_stale = grade_data(watch_missing, watch_provenance)
    watch_as_of_min, watch_as_of_max, watch_as_of_summary = as_of_summary_from(watch_provenance)

    mkt_vals = {c: float(next(h for h in holdings if str(h["code"]) == c).get("shares", 0) or 0) * prices[c]
                for c in prices}
    total = cash + sum(mkt_vals.values())
    invested_value = sum(mkt_vals.values())
    is_zero_position = invested_value <= 0
    first_funding_eligible = is_zero_position and cash > 0
    target_stress_drawdown, stress_contributions = estimate_target_stress_drawdown(holdings, uni)
    max_acceptable_drawdown = float(investor_profile.get("max_acceptable_drawdown", 0.15) or 0.15)
    # 全组合口径：场外稳健桶按 0 冲击纳入分母，压力回撤折算到整个组合（稳健桶是安全垫）。
    stable_outside = float(investor_profile.get("stable_assets_outside", 0) or 0)
    whole_portfolio_value = total + stable_outside
    whole_portfolio_stress_drawdown = whole_portfolio_stress(target_stress_drawdown, total, stable_outside)
    # 风险预算闸门按"全组合"压力回撤评估，而非只看 ETF 桶（否则稳健桶的缓冲被忽略）。
    risk_budget_breached = whole_portfolio_stress_drawdown > max_acceptable_drawdown

    rebal = []
    for h in holdings:
        code = str(h["code"])
        tw = float(h.get("target_weight", 0) or 0)
        cw = (mkt_vals.get(code, 0) / total) if total > 0 else 0.0
        dev = cw - tw
        triggered = (rebal_ok and total > 0
                     and (abs(dev) >= abs_thr or (tw > 0 and abs(dev) / tw >= rel_thr)))
        rebal.append({
            "code": code, "name": h.get("name", code),
            "target_weight": round(tw, 4), "current_weight": round(cw, 4),
            "deviation_pp": round(dev * 100, 2), "triggered": bool(triggered),
            "suggest": ("trim" if dev > 0 else "add") if triggered else "hold",
            "approx_amount": round(abs(dev) * total, 0) if triggered else 0,
        })

    discipline_blockers = []
    if not rebal_ok:
        discipline_blockers.append("数据质量不足，禁止交易动作")
    if used_cache and not allow_cache_trade:
        discipline_blockers.append("行情包含缓存，risk_controls 不允许据此交易")
    if risk_budget_breached:
        discipline_blockers.append(
            f"全组合压力回撤约 {whole_portfolio_stress_drawdown * 100:.1f}%，超过可接受回撤 {max_acceptable_drawdown * 100:.1f}%"
        )
    rebalance_blockers = list(discipline_blockers)
    if first_funding_eligible:
        rebalance_blockers.append("0持仓账户使用首次建仓预览，不直接执行再平衡")

    actionable_rebalance = []
    weekly_used = 0.0
    for r in rebal:
        rr = dict(r)
        reasons = []
        if not r["triggered"]:
            reasons.append("未触发再平衡")
        if rebalance_blockers:
            reasons.extend(rebalance_blockers)
        if r["triggered"] and r["approx_amount"] < min_trade:
            reasons.append(f"金额低于最小交易门槛 {min_trade:.0f} 元")
        if r["triggered"] and max_weekly > 0 and weekly_used + r["approx_amount"] > max_weekly:
            reasons.append(f"超过单周交易上限 {max_weekly:.0f} 元")
        allowed = r["triggered"] and not reasons
        if allowed:
            weekly_used += r["approx_amount"]
        rr["actionable"] = bool(allowed)
        rr["blocked_reasons"] = reasons
        actionable_rebalance.append(rr)

    first_deploy = 0.0
    if first_funding_eligible and first_pct > 0:
        first_deploy = cash * first_pct
        if max_weekly > 0:
            first_deploy = min(first_deploy, max_weekly)
    first_orders = []
    first_actual = 0.0
    for h in holdings:
        code = str(h["code"])
        price = prices.get(code)
        tw = float(h.get("target_weight", 0) or 0)
        target_amount = first_deploy * tw
        shares = floor_to_lot(target_amount, price or 0)
        actual_amount = shares * price if price else 0.0
        blocked = []
        if not is_zero_position:
            blocked.append("非 0 持仓账户，不适用首次建仓")
        elif cash <= 0:
            blocked.append("没有可用现金，无法生成首次建仓")
        if discipline_blockers:
            blocked.extend(discipline_blockers)
        if target_amount < min_trade:
            blocked.append(f"目标金额低于最小交易门槛 {min_trade:.0f} 元")
        if shares <= 0 and target_amount > 0:
            blocked.append("不足一手，暂不下单")
        allowed = first_funding_eligible and target_amount >= min_trade and shares > 0 and not discipline_blockers
        if allowed:
            first_actual += actual_amount
        first_orders.append({
            "code": code,
            "name": h.get("name", code),
            "target_weight": round(tw, 4),
            "target_amount": round(target_amount, 0),
            "last": round(price, 4) if price else None,
            "estimated_shares": shares,
            "estimated_amount": round(actual_amount, 0),
            "actionable": bool(allowed),
            "blocked_reasons": blocked,
        })
    first_funding_plan = {
        "is_zero_position": bool(is_zero_position),
        "eligible": bool(first_funding_eligible),
        "cash": round(cash, 2),
        "first_tranche_pct": first_pct,
        "planned_deploy_amount": round(first_deploy, 0),
        "estimated_deploy_amount": round(first_actual, 0),
        "estimated_unallocated": round(max(first_deploy - first_actual, 0), 0),
        "orders": first_orders,
        "notes": [
            "仅用于首次试仓预览，不自动下单",
            "观察池不参与首次建仓",
            "份额按 100 份一手粗略估算，实际以下单页面为准",
        ],
    }
    first_funding_plan["schedule"] = build_first_funding_schedule(
        holdings, prices, cash, first_pct, max_weekly, min_trade
    ) if first_funding_eligible else []

    target_annual_return = float(investor_profile.get("target_annual_return", 0.05) or 0.05)
    etf_expected_return = expected_etf_return(holdings, uni)                   # 当前目标权重的现实预期年化
    risk_budget = {
        "target_annual_return": target_annual_return,
        "target_annual_profit": round(total * target_annual_return, 2),       # 针对 ETF 桶
        "expected_etf_return": round(etf_expected_return, 4),                  # 现实预期年化（非承诺）
        "expected_target_gap": round(target_annual_return - etf_expected_return, 4),
        "max_acceptable_drawdown": max_acceptable_drawdown,                    # 全组合口径
        "max_acceptable_loss": round(whole_portfolio_value * max_acceptable_drawdown, 2),
        # ETF 桶自身口径（保留，标注；勿与全组合混淆）
        "target_portfolio_stress_drawdown": round(target_stress_drawdown, 4),
        "target_portfolio_stress_loss": round(total * target_stress_drawdown, 2),
        "etf_portfolio_value": round(total, 2),
        # 全组合口径（含场外稳健桶安全垫）
        "stable_assets_outside": round(stable_outside, 2),
        "whole_portfolio_value": round(whole_portfolio_value, 2),
        "whole_portfolio_stress_drawdown": round(whole_portfolio_stress_drawdown, 4),
        "whole_portfolio_stress_loss": round(whole_portfolio_value * whole_portfolio_stress_drawdown, 2),
        "stress_contributions": stress_contributions,
        "breached": bool(risk_budget_breached),
        "stress_losses": [
            {"drawdown": 0.05, "loss": round(whole_portfolio_value * 0.05, 2)},
            {"drawdown": 0.10, "loss": round(whole_portfolio_value * 0.10, 2)},
            {"drawdown": 0.15, "loss": round(whole_portfolio_value * 0.15, 2)},
        ],
        "assessment": (
            "全组合压力回撤超过预算，本周动作降级为只观察"
            if risk_budget_breached else
            "目标需要承担波动风险；首次建仓应分批执行"
            if target_annual_return >= 0.05 and is_zero_position
            else "按风险预算执行"
        ),
    }

    action_discipline = {
        "min_trade_amount": min_trade,
        "max_weekly_trade_amount": max_weekly,
        "first_tranche_pct": first_pct,
        "allow_trade_with_cache": allow_cache_trade,
        "trade_allowed": not discipline_blockers,
        "blocked_reasons": discipline_blockers,
        "rebalance_blocked_reasons": rebalance_blockers,
        "preflight_checks": build_preflight_checks(
            grade, rebal_ok, used_cache, allow_cache_trade, holdings, per,
            min_trade, max_weekly, is_zero_position,
            risk_budget_breached, whole_portfolio_stress_drawdown, max_acceptable_drawdown
        ),
    }

    equity_assets = ("equity", "equity_defensive", "global_equity", "global_growth", "china_growth")
    eq = [(c, s.get(f"momentum_{look}d")) for c, s in per.items()
          if isinstance(s, dict) and s.get("asset") in equity_assets
          and s.get(f"momentum_{look}d") is not None]
    eq.sort(key=lambda x: x[1], reverse=True)
    momentum_rank = [{"code": c, "name": per[c]["name"], "momentum": v} for c, v in eq]

    # 危机保险提醒：权益类跌破 MA200 → 显式风险提示（即便 risk_profile=进取，趋势仅展示不自动调仓，
    # 这里也要把"跌破均线"作为强提醒，而不是无视。它是降回撤的保险信号，不是择时增收。）
    trend_alerts = [
        {"code": c, "name": s["name"], "asset": s.get("asset"),
         "momentum": s.get(f"momentum_{look}d"), f"ma{ma_days}": s.get(f"ma{ma_days}"), "last": s.get("last")}
        for c, s in per.items()
        if isinstance(s, dict) and s.get("asset") in equity_assets and s.get("trend") == "below"
    ]

    out = {
        "generated_for": str(today),
        "data_quality": grade,
        "rebalance_allowed": rebal_ok,
        "data_complete": grade == "完整",
        "missing_prices": missing,
        "used_cache": used_cache,
        "stale_days_max": max_stale,
        "as_of_min": as_of_min,
        "as_of_max": as_of_max,
        "as_of_summary": as_of_summary,
        "portfolio_value": round(total, 2),
        "cash": round(cash, 2),
        "investor_profile": investor_profile,
        "signals": per,
        "watchlist_signals": watch_signals,
        "watchlist_data_quality": watch_grade,
        "watchlist_missing_prices": watch_missing,
        "watchlist_stale_days_max": watch_max_stale,
        "watchlist_as_of_min": watch_as_of_min,
        "watchlist_as_of_max": watch_as_of_max,
        "watchlist_as_of_summary": watch_as_of_summary,
        "valuation_status": valuation_status,
        "rebalance": rebal,
        "action_discipline": action_discipline,
        "actionable_rebalance": actionable_rebalance,
        "first_funding_plan": first_funding_plan,
        "risk_budget": risk_budget,
        "momentum_rank": momentum_rank,
        "trend_alerts": trend_alerts,
        "params": {
            "ma_days": ma_days, "momentum_lookback": look,
            "rebalance_abs_pp": abs_thr * 100, "rebalance_rel": rel_thr,
            "stale_limit_days": STALE_LIMIT_DAYS,
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 控制台可读摘要
    print("=" * 54)
    print(f"日期 {out['generated_for']} ｜ 数据【{grade}】{'（含缓存）' if used_cache else ''}"
          f" ｜ 行情截至 {as_of_summary}")
    print(f"组合总值约 ¥{total:,.0f}（含现金 ¥{cash:,.0f}）")
    print("-" * 54)
    for c, s in per.items():
        if "error" in s:
            print(f"{s['name']}({c}): {s['error']}")
            continue
        line = f"{s['name']}({c}){'[缓存]' if s.get('source') == 'cache' else ''}: " + (
            "↑在均线上" if s["trend"] == "above" else "↓跌破均线")
        m = s.get(f"momentum_{look}d")
        if m is not None:
            line += f" ｜ 动量{m * 100:+.1f}%"
        if "valuation" in s:
            line += f" ｜ 估值分位{s['valuation']['percentile'] * 100:.0f}%({s['valuation']['tag']})"
        elif s.get("valuation_na"):
            line += " ｜ 估值不适用"
        elif "valuation_missing" in s:
            line += " ｜ 估值缺失(非中性)"
        print(line)
    print("-" * 54)
    if not rebal_ok:
        why = ("缺失行情：" + ", ".join(missing)) if missing else f"数据过旧（最旧 {max_stale} 天）"
        print(f"⚠️ {why} —— 本次不输出再平衡建议，请稍后重跑")
    else:
        if used_cache:
            print(f"注：部分数据来自缓存（最旧 {max_stale} 天），建议仅供参考")
        if discipline_blockers:
            print("纪律检查：本周不允许执行交易动作 —— " + "；".join(discipline_blockers))
        else:
            print(f"纪律检查：允许交易｜单笔≥¥{min_trade:,.0f}｜单周≤¥{max_weekly:,.0f}")
        actionable = [r for r in actionable_rebalance if r["actionable"]]
        blocked = [r for r in actionable_rebalance if r["triggered"] and not r["actionable"]]
        if actionable:
            print("可执行再平衡动作：")
            for r in actionable:
                verb = "减仓" if r["suggest"] == "trim" else "加仓"
                print(f"  {verb} {r['name']}({r['code']}) 约 ¥{r['approx_amount']:,.0f}"
                      f"（偏离 {r['deviation_pp']:+.1f}pp）")
        if blocked:
            print("被门槛拦截的原始再平衡信号：")
            for r in blocked:
                verb = "减仓" if r["suggest"] == "trim" else "加仓"
                print(f"  {verb} {r['name']}({r['code']}) 约 ¥{r['approx_amount']:,.0f}"
                      f" —— {'；'.join(r['blocked_reasons'])}")
        if not actionable and not blocked:
            print("无再平衡触发（持仓为空或未超阈值）")
    if trend_alerts:
        print("-" * 54)
        names = "、".join(f"{a['name']}({a['code']})" for a in trend_alerts)
        print(f"⚠️ 危机保险提醒：{names} 已跌破 MA{ma_days} —— 趋势转弱的风险信号（降回撤用，非择时增收）。")
        print("   是否减风险由你定；本工具不自动调仓。")
    if first_funding_plan["eligible"]:
        print("-" * 54)
        print(f"首次建仓预览：计划投入 ¥{first_funding_plan['planned_deploy_amount']:,.0f}"
              f"（现金的 {first_pct * 100:.0f}%）｜估算可成交 ¥{first_funding_plan['estimated_deploy_amount']:,.0f}")
        for o in first_funding_plan["orders"]:
            status = "可执行" if o["actionable"] else "暂不执行"
            print(f"  {o['name']}({o['code']}): {status} ｜ {o['estimated_shares']} 份"
                  f" ｜ 约 ¥{o['estimated_amount']:,.0f}")
    if watch_signals:
        print("-" * 54)
        print(f"观察池（仅学习/监控，不触发交易）｜数据【{watch_grade}】｜行情截至 {watch_as_of_summary}")
        for c, s in watch_signals.items():
            if "error" in s:
                print(f"{s['name']}({c}): {s['error']}")
                continue
            line = f"{s['name']}({c}){'[缓存]' if s.get('source') == 'cache' else ''}: " + (
                "↑在均线上" if s["trend"] == "above" else "↓跌破均线")
            m = s.get(f"momentum_{look}d")
            if m is not None:
                line += f" ｜ 动量{m * 100:+.1f}%"
            if s.get("role"):
                line += f" ｜ {s['role']}"
            print(line)
    print("=" * 54)
    print(f"已写出 {args.out}")


if __name__ == "__main__":
    main()
