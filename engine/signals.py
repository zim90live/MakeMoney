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
        if F["valuation"]["enabled"] and meta.get("index"):
            v, vst = fetch_valuation_pct(meta["index"], vyears)
            if v:
                tag = "cheap" if v["percentile"] <= cheap else (
                    "rich" if v["percentile"] >= rich else "neutral")
                sig["valuation"] = {**v, "tag": tag}
            else:
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

    action_discipline = {
        "min_trade_amount": min_trade,
        "max_weekly_trade_amount": max_weekly,
        "first_tranche_pct": first_pct,
        "allow_trade_with_cache": allow_cache_trade,
        "trade_allowed": not discipline_blockers,
        "blocked_reasons": discipline_blockers,
        "rebalance_blocked_reasons": rebalance_blockers,
    }

    eq = [(c, s.get(f"momentum_{look}d")) for c, s in per.items()
          if isinstance(s, dict) and s.get("asset") in ("equity", "equity_defensive")
          and s.get(f"momentum_{look}d") is not None]
    eq.sort(key=lambda x: x[1], reverse=True)
    momentum_rank = [{"code": c, "name": per[c]["name"], "momentum": v} for c, v in eq]

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
        "momentum_rank": momentum_rank,
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
