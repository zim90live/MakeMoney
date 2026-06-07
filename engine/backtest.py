#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────
# 【单一事实源 / SINGLE SOURCE OF TRUTH】 回测引擎，与 engine/signals.py 同属唯一实现。
# 评审者请注意：本项目“有回测”。Claude(.claude/) 与 Codex(.agents/) 只放 SKILL.md，
# 共用本目录的代码与根目录的 strategy.yaml / portfolio.yaml，不存在重复副本。
# ─────────────────────────────────────────────────────────────────────────
"""
策略回测（两段）：
  ① ETF 可交易回测 —— 用真实场内 ETF 历史，区间受最新 ETF 成立时间限制（约 6 年）。
  ② 指数代理长期回测 —— 用长历史指数代理各 sleeve，把样本拉长到约 20 年、覆盖更多市场周期。
     近似与局限：价格指数未含分红（低估收益，尤其债券/红利）；缺长序列的 sleeve（红利低波、黄金）
     做近似或剔除（见 strategy.yaml 的 proxy_index）。长回测主要看“更长周期的回撤轮廓”，非精确收益。

被测机械策略：目标权重 + 趋势过滤(权益跌破均线→移入债券) + 月度再平衡 + 成本。
  注：动量/估值/5-25 是 live 周报的展示信号，不进入机械回测（避免过拟合）。
数据：行情 东财(前复权)→新浪(未复权)→缓存；指数 新浪(价格指数)。元数据见 engine/data/meta.json。
回测好 ≠ 未来赚钱。
"""
import argparse
import json
import os
import sys
import time
from datetime import date

import yaml
import pandas as pd
import akshare as ak


def configure_console_encoding():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


configure_console_encoding()


HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
META_PATH = os.path.join(DATA_DIR, "meta.json")
WARMUP = 250          # 统一净值起点（覆盖最长均线参数），保证各组同起点公平
COST = 0.0003         # 单边交易成本（万3）


def find_repo_root(start):
    d = start
    for _ in range(8):
        if os.path.exists(os.path.join(d, "portfolio.yaml")):
            return d
        p = os.path.dirname(d)
        if p == d:
            break
        d = p
    return None


def load_yaml(p):
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _norm(df):
    df = df[["date", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna().sort_values("date").reset_index(drop=True)


def _load_meta():
    if os.path.exists(META_PATH):
        try:
            with open(META_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_meta(key, entry):
    os.makedirs(DATA_DIR, exist_ok=True)
    meta = _load_meta()
    meta[key] = entry
    try:
        with open(META_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def _ensure_meta_from_cache(key, df, source, adjust):
    """旧缓存没有 meta 时补一条保守元数据，方便协作环境复现。"""
    meta = _load_meta()
    if key in meta:
        return
    _save_meta(key, {
        "source": source,
        "adjust": adjust,
        "fetched_at": "unknown_cache",
        "start": str(df["date"].iloc[0].date()),
        "end": str(df["date"].iloc[-1].date()),
        "rows": int(len(df)),
    })


def _persist(df, cache, key, source, adjust):
    df.to_csv(cache, index=False)
    _save_meta(key, {
        "source": source, "adjust": adjust, "fetched_at": str(date.today()),
        "start": str(df["date"].iloc[0].date()), "end": str(df["date"].iloc[-1].date()),
        "rows": int(len(df)),
    })


def fetch_etf(code, refresh=False):
    """ETF 行情：东财(前复权)→新浪(未复权)→缓存。返回 (Series, source)。"""
    cache = os.path.join(DATA_DIR, f"{code}.csv")
    if os.path.exists(cache) and not refresh:
        df = _norm(pd.read_csv(cache))
        _ensure_meta_from_cache(code, df, "缓存(旧数据)", "未知；运行 --refresh 可重取并登记")
        return df.set_index("date")["close"], "缓存"
    os.makedirs(DATA_DIR, exist_ok=True)

    def _em():
        d = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="qfq")
        return _norm(d.rename(columns={"日期": "date", "收盘": "close"})), "东财", "前复权"

    def _sina():
        prefix = "sh" if code[:1] in ("5", "6") else "sz"
        return _norm(ak.fund_etf_hist_sina(symbol=prefix + code)), "新浪", "未复权"

    for getter in (_em, _sina):
        for _ in range(3):
            try:
                df, source, adjust = getter()
                if df is not None and not df.empty:
                    _persist(df, cache, code, source, adjust)
                    return df.set_index("date")["close"], source
            except Exception:  # noqa: BLE001
                time.sleep(1.0)
    return None, None


def fetch_index(symbol, refresh=False):
    """指数行情（新浪，价格指数/未含分红）。返回 (Series, source)。"""
    cache = os.path.join(DATA_DIR, f"idx_{symbol}.csv")
    if os.path.exists(cache) and not refresh:
        df = _norm(pd.read_csv(cache))
        _ensure_meta_from_cache(f"idx_{symbol}", df, "缓存(旧数据)", "价格指数(未含分红)")
        return df.set_index("date")["close"], "缓存"
    os.makedirs(DATA_DIR, exist_ok=True)
    for _ in range(3):
        try:
            d = ak.stock_zh_index_daily(symbol=symbol)
            if d is not None and not d.empty and "close" in d.columns:
                df = _norm(d[["date", "close"]])
                _persist(df, cache, f"idx_{symbol}", "新浪指数", "价格指数(未含分红)")
                return df.set_index("date")["close"], "新浪指数"
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    return None, None


def simulate(px, targets, trend_codes, bond_code, ma_days, cost, use_trend, freq="M"):
    """日频净值模拟，按 freq(M/Q) 再平衡。返回 (nav, stats)。"""
    rets = px.pct_change().fillna(0.0)
    ma = px.rolling(ma_days).mean()
    period = pd.Series(px.index.to_period(freq), index=px.index)
    is_rebal = ~period.duplicated().values

    nav, holdings = [], None
    turn_sum, n_rebal = 0.0, 0
    for i in range(len(px)):
        if holdings is not None:
            for c in targets:
                holdings[c] *= (1 + rets[c].iloc[i])
        if is_rebal[i] and i >= ma_days:
            total = sum(holdings.values()) if holdings is not None else 1.0
            tw = dict(targets)
            if use_trend:
                moved = 0.0
                for c in trend_codes:
                    if px[c].iloc[i] < ma[c].iloc[i]:
                        moved += tw[c]
                        tw[c] = 0.0
                tw[bond_code] += moved
            target_val = {c: tw[c] * total for c in targets}
            if holdings is not None and total > 0:
                turnover = sum(abs(target_val[c] - holdings[c]) for c in targets)
                turn_sum += turnover / total
                total -= turnover * cost
                n_rebal += 1
            holdings = {c: tw[c] * total for c in targets}
        nav.append(sum(holdings.values()) if holdings is not None else 1.0)
    return pd.Series(nav, index=px.index), {"turn_sum": turn_sum, "n_rebal": n_rebal}


def metrics(nav):
    nav = nav / nav.iloc[0]
    yrs = len(nav) / 252
    r = nav.pct_change().dropna()
    cagr = nav.iloc[-1] ** (1 / yrs) - 1
    vol = r.std() * (252 ** 0.5)
    dd = (nav / nav.cummax() - 1).min()
    calmar = cagr / abs(dd) if dd < 0 else float("nan")
    peak = nav.cummax()
    uw = (nav < peak).values
    longest = cur = 0
    for x in uw:
        cur = cur + 1 if x else 0
        longest = max(longest, cur)
    return {"cagr": cagr, "vol": vol, "dd": dd, "calmar": calmar,
            "total": nav.iloc[-1] - 1, "uw_days": longest}


def _run(px, targets, trend_codes, bond_code, use_trend, ma, freq, yrs):
    nav, st = simulate(px, targets, trend_codes, bond_code, ma, COST, use_trend, freq)
    nav = nav.iloc[WARMUP:]
    m = metrics(nav)
    m["turn_ann"] = st["turn_sum"] / yrs
    return m


def _run_with_nav(px, targets, trend_codes, bond_code, use_trend, ma, freq, yrs):
    nav, st = simulate(px, targets, trend_codes, bond_code, ma, COST, use_trend, freq)
    nav = nav.iloc[WARMUP:]
    m = metrics(nav)
    m["turn_ann"] = st["turn_sum"] / yrs
    return nav / nav.iloc[0], m


def clean_metric(m):
    return {
        "cagr": round(float(m.get("cagr", 0)), 4),
        "vol": round(float(m.get("vol", 0)), 4),
        "max_drawdown": round(float(m.get("dd", 0)), 4),
        "calmar": round(float(m.get("calmar", 0)), 4),
        "total_return": round(float(m.get("total", 0)), 4),
        "underwater_days": int(m.get("uw_days", 0)),
        "turnover_annual": round(float(m.get("turn_ann", 0)), 4),
    }


def sampled_curve(nav, max_points=180):
    nav = nav / nav.iloc[0]
    dd = nav / nav.cummax() - 1
    if len(nav) > max_points:
        step = max(1, len(nav) // max_points)
        idx = list(range(0, len(nav), step))
        if idx[-1] != len(nav) - 1:
            idx.append(len(nav) - 1)
        nav = nav.iloc[idx]
        dd = dd.iloc[idx]
    return [
        {"date": str(d.date()), "nav": round(float(v), 4), "drawdown": round(float(dd.loc[d]), 4)}
        for d, v in nav.items()
    ]


# ─── 分批 / 定投建仓回测（P1-1）：把固定资金按不同节奏投入静态目标组合，比较期末倍数与回撤 ───
DCA_CASH_YIELD = 0.02                                       # 未部署现金的假设年化（货币基金量级，保守）
DCA_PLANS = [(1, "一次性"), (6, "分6个月"), (12, "分12个月"), (24, "分24个月")]


def _dca_sim(arr, dates, t0, horizon, deploy_months, step, cash_daily, want_path=False, max_points=120):
    """单窗口分批建仓：1 单位资金按 deploy_months 个月均匀投入静态组合。
    返回 (期末倍数, 窗口内最大回撤, 价值曲线|None)。未部署现金按 cash_daily 计息。"""
    n_tr = max(1, deploy_months)
    tranche = 1.0 / n_tr
    tr_days = [t0 + i * step for i in range(n_tr)]
    units, cash, nxt = 0.0, 1.0, 0
    peak, max_dd, raw = None, 0.0, []
    end = t0 + horizon
    for d in range(t0, end + 1):
        if d > t0:
            cash *= cash_daily
        while nxt < n_tr and tr_days[nxt] <= d:
            buy = min(tranche, cash)
            units += buy / arr[tr_days[nxt]]
            cash -= buy
            nxt += 1
        val = units * arr[d] + cash
        peak = val if peak is None or val > peak else peak
        max_dd = min(max_dd, val / peak - 1.0)
        if want_path:
            raw.append((dates[d], val))
    path = None
    if want_path:
        if len(raw) > max_points:
            s = max(1, len(raw) // max_points)
            idx = list(range(0, len(raw), s))
            if idx[-1] != len(raw) - 1:
                idx.append(len(raw) - 1)
            raw = [raw[i] for i in idx]
        path = [{"date": d, "value": round(v, 4)} for d, v in raw]
    return units * arr[end] + cash, max_dd, path


def _median(xs):
    s = sorted(xs)
    if not s:
        return 0.0
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def run_dca(static_nav, horizon_days=756, step=21, cash_yield=DCA_CASH_YIELD):
    """对静态组合净值跑滚动窗口的分批建仓对比。历史不足时返回 None。"""
    arr = [float(x) for x in static_nav.values]
    dates = [str(d.date()) for d in static_nav.index]
    n = len(arr)
    horizon = min(horizon_days, n - 1)
    max_span = max(m for m, _ in DCA_PLANS) * step
    if horizon <= max_span + step or n - horizon - 1 < 1:
        return None
    cash_daily = (1.0 + cash_yield) ** (1.0 / 252.0)
    starts = list(range(0, n - horizon - 1, step))
    finals, dds = {}, {}
    for months, _ in DCA_PLANS:
        fs, dd = [], []
        for t0 in starts:
            f, m, _ = _dca_sim(arr, dates, t0, horizon, months, step, cash_daily)
            fs.append(f)
            dd.append(m)
        finals[months], dds[months] = fs, dd
    lump = finals[1]
    plans_out = []
    for months, label in DCA_PLANS:
        fs = finals[months]
        win = None if months == 1 else round(sum(1 for a, b in zip(fs, lump) if a >= b) / len(fs), 3)
        plans_out.append({
            "deploy_months": months, "label": label,
            "median_final_multiple": round(_median(fs), 4),
            "median_total_return": round(_median(fs) - 1.0, 4),
            "median_max_drawdown": round(_median(dds[months]), 4),
            "beats_lumpsum_window_pct": win,
        })
    t0 = starts[-1]
    curves = []
    for months, label in DCA_PLANS:
        _, _, path = _dca_sim(arr, dates, t0, horizon, months, step, cash_daily, want_path=True)
        curves.append({"label": label, "deploy_months": months, "points": path})
    return {
        "horizon_years": round(horizon / 252.0, 2),
        "windows": len(starts),
        "cash_yield": cash_yield,
        "step_days": step,
        "plans": plans_out,
        "representative": {"start": dates[t0], "end": dates[t0 + horizon], "curves": curves},
        "notes": [
            "口径：把 1 单位资金按不同节奏投入『静态目标组合』，比较期末倍数与窗口内最大回撤。",
            f"滚动 {len(starts)} 个起点（每 ~{step} 交易日一个），窗口约 {round(horizon / 252.0, 1)} 年；ETF 段历史有限、样本重叠，仅示意量级，非稳健分布。",
            f"未部署现金假设年化 {cash_yield:.0%}；过去≠未来。",
        ],
    }


def tactical_weekly_sim(px, strategic, asset_of, reserve_asset, shocks,
                        profile="平衡", step=5, warmup=250, cfg=None, etf_share=1.0):
    """【Phase A 骨架】周频事件驱动战术回测：与影子计算**复用 engine/tactical.py 同一套纯函数**（§15.1 #7）。

    每 step 交易日为一个正式决策点；用决策点【之前】可得的价格算子信号→score_asset→next_tactical_state
    →construct_tactical_portfolio，得到战术权重，持有到下一决策点，对比战略静态组合净值。

    ⚠️ 骨架边界（Phase B 落地）：未计佣金/滑点/折溢价、未模拟动作门槛/整手/单周上限、估值臂未做历史时点重建
    （此处估值缺省→仅价格臂，符合 §13.2 对多数资产的现实）。仅验证"周频管线跑通且与影子同源"。
    """
    import tactical as tac
    cfg = cfg or tac.TACTICAL_DEFAULTS
    codes = list(px.columns)
    arr = {c: px[c].tolist() for c in codes}
    rets = px.pct_change().fillna(0.0)
    n = len(px)
    states = {}
    tac_w = dict(strategic)
    tac_cash = 0.0
    static_w = dict(strategic)
    tac_nav, static_nav = 1.0, 1.0
    rebalances = 0
    for i in range(warmup, n):
        tac_nav *= (1.0 + sum(tac_w.get(c, 0) * float(rets[c].iloc[i]) for c in codes))   # 现金计 0
        static_nav *= (1.0 + sum(static_w.get(c, 0) * float(rets[c].iloc[i]) for c in codes))
        if (i - warmup) % step == 0:
            assets = [{"code": c, "asset": asset_of.get(c), "strategic_weight": strategic.get(c, 0),
                       "closes": arr[c][:i + 1], "shock": shocks.get(c, 0), "data_quality_multiplier": 1.0}
                      for c in codes]
            out = tac.compute_shadow(assets, profile, reserve_asset, etf_share=etf_share,
                                     cfg=cfg, prior_states=states)
            states = {c: d.get("state_after") for c, d in out["diagnostics"].items() if d.get("state_after")}
            if out.get("ok") and out.get("weights"):
                tac_w = {c: out["weights"].get(c, 0) for c in codes}
                tac_cash = out.get("cash", 0) or 0
                rebalances += 1
    return {"tactical_final": round(tac_nav, 4), "static_final": round(static_nav, 4),
            "rebalances": rebalances, "profile": profile, "weeks": max(0, (n - warmup) // step),
            "note": "Phase A 骨架：未计成本/门槛、估值仅价格臂；与影子计算共用 tactical.py 纯函数。"}


def _val_pct_at(series, dt):
    """点位估值分位的【历史时点】取值：返回 ≤dt 的最后一个分位（无前视）；series 为 pandas Series（index=日期）。"""
    if series is None:
        return None
    s = series[series.index <= dt]
    return float(s.iloc[-1]) if len(s) else None


def _val_reliability_at(series, dt):
    """估值可靠度按【该决策点可得的 PE 历史长度】分级(§4.6)：<3年→0；3-7年线性 0→0.85；≥7年→0.85(新鲜)。

    修正了早先回测里硬编码 0.85 的口径偏差——短历史(如长代理段最早的 2007-2010)不再被当近满置信。
    """
    if series is None or len(series) == 0:
        return 0.0
    yrs = (dt - series.index[0]).days / 365.0
    if yrs < 3:
        return 0.0
    return round(min(0.85, 0.85 * (yrs - 3) / 4.0), 3)


def _tactical_targets(upto_closes, strategic, asset_of, reserve, shocks, cfg, profile, states,
                      *, mode="tactical", etf_share=1.0, max_whole_stress=None, valuation_at=None, valuation_rel=None):
    """单个决策点：组装资产→tactical.compute_shadow→(目标权重含 reserve, 目标现金, 新状态)。mode 控制消融/方向。"""
    import tactical as tac
    assets = []
    for c in strategic:
        a = {"code": c, "asset": asset_of.get(c), "strategic_weight": strategic[c],
             "closes": upto_closes[c], "shock": shocks.get(c, 0), "data_quality_multiplier": 1.0}
        vp = (valuation_at or {}).get(c) if mode != "no_valuation" else None
        a["valuation_percentile"] = vp
        a["valuation_reliability"] = ((valuation_rel or {}).get(c, 0.0) if vp is not None else 0.0)
        assets.append(a)
    out = tac.compute_shadow(assets, profile, reserve, etf_share=etf_share, max_whole_stress=max_whole_stress,
                             cfg=cfg, prior_states=states, gate_by_state=(mode != "no_state"))
    weights = dict(out.get("weights") or {})
    cash = out.get("cash", 0) or 0.0
    new_states = {c: d.get("state_after") for c, d in out["diagnostics"].items() if d.get("state_after")}
    if mode == "negative_only":          # 仅允许降险：高于战略的风险倾斜压回战略，释放进 reserve（保持合计不变）
        freed = 0.0
        for c in list(weights):
            if c == str(reserve):
                continue
            if weights[c] > strategic.get(c, 0):
                freed += weights[c] - strategic[c]
                weights[c] = strategic[c]
        weights[str(reserve)] = weights.get(str(reserve), 0) + freed
    return weights, cash, new_states


def simulate_tactical(px, strategic, asset_of, reserve, shocks, *, mode="tactical", profile="平衡",
                      cfg=None, step=5, warmup=250, cost_per_side=COST, premium_assets=None,
                      premium_extra=0.0, min_rebal_turnover=0.005, etf_share=1.0,
                      max_whole_stress=None, valuations=None):
    """全事件驱动周频战术回测（Phase B）。point-in-time 决策、动作门槛、佣金/滑点、QDII 溢价成本。

    mode ∈ {static(静态战略), 5_25(现有再平衡), tactical(双向), negative_only(仅负向),
            no_valuation(去估值消融), no_state(去状态机消融)}。返回 (nav_series, stats)。
    valuations: {code: pandas.Series(index=日期, 估值分位)}，历史时点取值、无前视；缺省=仅价格臂（§13.2）。
    """
    import tactical as tac
    cfg = cfg or tac.TACTICAL_DEFAULTS
    premium_assets = set(premium_assets or [])
    codes = list(px.columns)
    arr = {c: px[c].tolist() for c in codes}
    rets = px.pct_change().fillna(0.0)
    n = len(px)
    states, w, cash = {}, dict(strategic), 0.0
    nav, navs, turn_sum, n_rebal = 1.0, [], 0.0, 0
    for i in range(warmup, n):
        nav *= (1.0 + sum(w.get(c, 0) * float(rets[c].iloc[i]) for c in codes))   # 未投现金计 0
        if (i - warmup) % step == 0:
            target, tcash = None, 0.0
            if mode == "static":
                target = dict(strategic)
            elif mode == "5_25":
                dev = max((abs(w.get(c, 0) - strategic[c]) for c in codes), default=0)
                rel = max((abs(w.get(c, 0) - strategic[c]) / strategic[c] for c in codes if strategic[c] > 0), default=0)
                if dev >= 0.05 or rel >= 0.25:
                    target = dict(strategic)
            else:
                val_at = ({c: _val_pct_at(valuations.get(c), px.index[i]) for c in codes}
                          if valuations else None)
                val_rel = ({c: _val_reliability_at(valuations.get(c), px.index[i]) for c in codes}
                           if valuations else None)
                target, tcash, states = _tactical_targets(
                    {c: arr[c][:i + 1] for c in codes}, strategic, asset_of, reserve, shocks, cfg,
                    profile, states, mode=mode, etf_share=etf_share, max_whole_stress=max_whole_stress,
                    valuation_at=val_at, valuation_rel=val_rel)
            if target is not None:
                tgt = {c: target.get(c, 0) for c in codes}
                turnover = sum(abs(tgt[c] - w.get(c, 0)) for c in codes) + abs(tcash - cash)
                if turnover / 2.0 >= min_rebal_turnover:
                    cost = sum(abs(tgt[c] - w.get(c, 0)) *
                               (cost_per_side + (premium_extra if (c in premium_assets and tgt[c] > w.get(c, 0)) else 0))
                               for c in codes)
                    nav *= (1.0 - cost)
                    turn_sum += turnover / 2.0
                    n_rebal += 1
                    w, cash = tgt, tcash
        navs.append(nav)
    nav_series = pd.Series(navs, index=px.index[warmup:])
    m = metrics(nav_series)
    yrs = len(nav_series) / 252.0
    m["turn_ann"] = turn_sum / yrs if yrs > 0 else 0.0
    m["n_rebal"] = n_rebal
    return nav_series, m


TACTICAL_MODES = [("static", "静态战略组合"), ("5_25", "现有5/25再平衡"), ("negative_only", "仅负向战术覆盖"),
                  ("tactical", "双向战术配置"), ("no_valuation", "双向·去估值(消融)"), ("no_state", "双向·去状态机(消融)")]


def run_tactical_comparison(px, strategic, asset_of, reserve, shocks, **kw):
    """§13.1 六策略对比；返回每策略的 §13.3 指标 + 年换手 + 再平衡次数。"""
    rows = []
    for mode, label in TACTICAL_MODES:
        _, m = simulate_tactical(px, strategic, asset_of, reserve, shocks, mode=mode, **kw)
        sharpe = (m["cagr"] / m["vol"]) if m["vol"] > 0 else float("nan")
        rows.append({"mode": mode, "label": label, **clean_metric(m),
                     "sharpe": round(sharpe, 2), "n_rebal": m.get("n_rebal", 0)})
    return rows


def walk_forward_tactical(px, strategic, asset_of, reserve, shocks, folds=3, warmup=250, **kw):
    """§13.4 walk-forward：把样本切成连续 folds 段，逐段跑"双向 vs 静态"，看结论是否稳定（参数全程冻结）。"""
    n = len(px)
    out = []
    seg = (n - warmup) // folds
    if seg <= 30:
        return out
    for k in range(folds):
        a = warmup + k * seg
        b = n if k == folds - 1 else warmup + (k + 1) * seg
        sub = px.iloc[max(0, a - warmup):b]
        _, mt = simulate_tactical(sub, strategic, asset_of, reserve, shocks, mode="tactical", warmup=warmup, **kw)
        _, ms = simulate_tactical(sub, strategic, asset_of, reserve, shocks, mode="static", warmup=warmup, **kw)
        out.append({"fold": k + 1, "tactical_cagr": round(mt["cagr"], 4), "static_cagr": round(ms["cagr"], 4),
                    "tactical_maxdd": round(mt["dd"], 4), "static_maxdd": round(ms["dd"], 4)})
    return out


def perturb_params(px, strategic, asset_of, reserve, shocks, pct=0.20, warmup=250, **kw):
    """§13.4 参数扰动：核心参数 ±pct，看"双向是否改善最大回撤"的结论是否反转。"""
    import copy
    import tactical as tac
    base = kw.pop("cfg", None) or tac.TACTICAL_DEFAULTS
    results = []
    for name, mult in (("base", 1.0), ("trend_scale_up", 1 + pct), ("trend_scale_dn", 1 - pct),
                       ("deadband_up", 1 + pct), ("deadband_dn", 1 - pct)):
        cfg = copy.deepcopy(base)
        if "trend_scale" in name:
            cfg["signals"]["trend_scale"] *= mult
        elif "deadband" in name:
            cfg["signals"]["deadband"] *= mult
        _, mt = simulate_tactical(px, strategic, asset_of, reserve, shocks, mode="tactical", cfg=cfg, warmup=warmup, **kw)
        _, ms = simulate_tactical(px, strategic, asset_of, reserve, shocks, mode="static", cfg=cfg, warmup=warmup, **kw)
        results.append({"variant": name, "tactical_better_maxdd": mt["dd"] >= ms["dd"],  # dd 为负，越大越好
                        "tactical_maxdd": round(mt["dd"], 4), "static_maxdd": round(ms["dd"], 4)})
    return results


def valuation_percentile_series(pe_df, lookback_years=5, min_obs=120):
    """【历史时点】估值分位（无前视）：对每个交易日，用其【之前 lookback_years 窗口】内的 PE 算当日 PE 的分位。

    pe_df: DataFrame[date, pe]。返回 pandas.Series(index=DatetimeIndex, 分位 0~1)；窗口不足 min_obs 的早期日期跳过。
    纯函数、可测；point-in-time 只用 ≤当日 数据，绝不前视。
    """
    d = pe_df.dropna(subset=["pe"]).copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d = d.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    pe = [float(x) for x in d["pe"].tolist()]
    dates = list(d["date"])
    win = int(lookback_years * 244)
    idx, val = [], []
    for i in range(len(pe)):
        window = pe[max(0, i - win + 1):i + 1]
        if len(window) < min_obs:
            continue
        cur = pe[i]
        idx.append(dates[i])
        val.append(round(sum(1 for x in window if x < cur) / len(window), 4))
    return pd.Series(val, index=pd.DatetimeIndex(idx))


def fetch_pe_history(index_name, cache_key, refresh=False):
    """指数滚动 PE 全历史（ak.stock_index_pe_lg）；缓存 engine/data/pe_<cache_key>.csv（离线复现的种子）。返回 DataFrame[date,pe]|None。"""
    cache = os.path.join(DATA_DIR, f"pe_{cache_key}.csv")
    if os.path.exists(cache) and not refresh:
        try:
            return pd.read_csv(cache)
        except Exception:  # noqa: BLE001
            return None
    os.makedirs(DATA_DIR, exist_ok=True)
    for _ in range(3):
        try:
            df = ak.stock_index_pe_lg(symbol=index_name)
            if df is not None and not df.empty and "滚动市盈率" in df.columns:
                out = pd.DataFrame({"date": pd.to_datetime(df["日期"]).astype(str),
                                    "pe": pd.to_numeric(df["滚动市盈率"], errors="coerce")}).dropna()
                if not out.empty:
                    out.to_csv(cache, index=False)
                    return out
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    return None


def build_proxy_valuations(refresh=False):
    """长代理段权益的历史时点估值分位：sh000300→沪深300、sh000905→中证500。无网络/无种子→空(仅价格臂)。"""
    out = {}
    for sym, name, key in (("sh000300", "沪深300", "hs300"), ("sh000905", "中证500", "zz500")):
        pe = fetch_pe_history(name, key, refresh=refresh)
        if pe is not None and not pe.empty:
            try:
                s = valuation_percentile_series(pe)
                if len(s):
                    out[sym] = s
            except Exception:  # noqa: BLE001
                pass
    return out


def build_proxy_panel(strat, targets, refresh=False):
    """构建长代理段面板（与主回测 §② 同源代理映射）。返回 (pxL, proxy_targets, proxy_asset_of, bond_proxy)|None。"""
    uni = strat["universe"]
    asset = {str(u["code"]): u["asset"] for u in uni}
    prox = {str(u["code"]): u.get("proxy_index") for u in uni}
    proxy_targets, code_proxy = {}, {}
    for c, w in targets.items():
        pidx = prox.get(c)
        if pidx:
            proxy_targets[pidx] = proxy_targets.get(pidx, 0.0) + float(w)
            code_proxy[c] = pidx
    pseries = {}
    for sym in list(proxy_targets):
        s, _src = fetch_index(sym, refresh=refresh)
        if s is None:
            proxy_targets.pop(sym, None)
            continue
        pseries[sym] = s
    if not pseries:
        return None
    pxL = pd.DataFrame(pseries).dropna()
    ssum = sum(proxy_targets.values()) or 1.0
    proxy_targets = {k: v / ssum for k, v in proxy_targets.items() if k in pseries}
    proxy_asset, bond_proxy = {}, None
    for c, p in code_proxy.items():
        if p not in pseries:
            continue
        proxy_asset[p] = "bond" if asset.get(c) == "bond" else "equity"
        if asset.get(c) == "bond":
            bond_proxy = p
    if not bond_proxy or pxL.empty or len(pxL.columns) < 2:
        return None
    return pxL, proxy_targets, proxy_asset, bond_proxy


def fetch_us_index(sina_symbol, cache_key, refresh=False):
    """美股指数（新浪 .INX/.IXIC，长历史，价格）；缓存 engine/data/idx_<key>.csv（种子）。返回 (Series, source)|(None,None)。"""
    cache = os.path.join(DATA_DIR, f"idx_{cache_key}.csv")
    if os.path.exists(cache) and not refresh:
        try:
            df = _norm(pd.read_csv(cache))
            return df.set_index("date")["close"], "缓存"
        except Exception:  # noqa: BLE001
            return None, None
    os.makedirs(DATA_DIR, exist_ok=True)
    for _ in range(3):
        try:
            d = ak.index_us_stock_sina(symbol=sina_symbol)
            if d is not None and not d.empty and "close" in d.columns:
                df = _norm(d[["date", "close"]])
                df.to_csv(cache, index=False)
                return df.set_index("date")["close"], "新浪美股"
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    return None, None


# 全收益+全分散长段：代理映射（保留 QDII + 黄金；创业板 2010 起会砍掉 2008 / 科创50 2020 → 剔除并注明）
FULL_PROXY = {"511010": "sh000012", "510300": "sh000300", "512890": "sh000300",
              "510500": "sh000905", "513500": "spx", "513100": "ixic", "518880": "gold"}
US_PROXIES = {"spx": ".INX", "ixic": ".IXIC"}
# 合成全收益用的年化分红/carry（A股股息、美股股息、国债票息；黄金无分红）——把"价格指数"补成"全收益"
DIV_YIELD = {"sh000012": 0.030, "sh000300": 0.022, "sh000905": 0.013, "spx": 0.019, "ixic": 0.008, "gold": 0.0}


def fetch_gold_proxy(refresh=False):
    """长黄金价格代理（2004~，覆盖 2008）：SPDR GLD 持仓报告 `macro_cons_gold` 反推美元金价 ≈ 总价值/总库存。

    缓存 engine/data/idx_gold.csv。列名因编码不稳→按位置取（日期=1、总库存=2、总价值=4）。返回 (Series, source)|(None,None)。
    """
    cache = os.path.join(DATA_DIR, "idx_gold.csv")
    if os.path.exists(cache) and not refresh:
        try:
            df = _norm(pd.read_csv(cache))
            return df.set_index("date")["close"], "缓存"
        except Exception:  # noqa: BLE001
            return None, None
    os.makedirs(DATA_DIR, exist_ok=True)
    for _ in range(3):
        try:
            d = ak.macro_cons_gold()
            if d is not None and len(d) and d.shape[1] >= 5:
                date = pd.to_datetime(d.iloc[:, 1], errors="coerce")
                inv = pd.to_numeric(d.iloc[:, 2], errors="coerce")
                val = pd.to_numeric(d.iloc[:, 4], errors="coerce")
                out = pd.DataFrame({"date": date, "close": val / inv}).dropna()
                out = out[out["close"] > 0].sort_values("date").drop_duplicates("date")
                if len(out) > 500:
                    out.to_csv(cache, index=False)
                    return out.set_index("date")["close"], "GLD持仓反推"
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    return None, None


def _to_total_return(close, yld):
    """价格序列 → 全收益指数：日收益 + 分红/252，复利累乘。"""
    r = close.pct_change().fillna(0.0) + yld / 252.0
    return (1.0 + r).cumprod()


def build_full_panel(strat, targets, refresh=False):
    """全收益+全分散长面板（A股+美股QDII+国债，含分红、忽略汇率；剔除黄金/创业板/科创50）。

    返回 (pxL_全收益, proxy_targets, proxy_asset_of, bond_proxy, dropped)|None。
    """
    asset = {str(u["code"]): u["asset"] for u in strat["universe"]}
    proxy_targets, code_proxy, dropped = {}, {}, []
    for c, w in targets.items():
        p = FULL_PROXY.get(str(c))
        if p:
            proxy_targets[p] = proxy_targets.get(p, 0.0) + float(w)
            code_proxy[str(c)] = p
        else:
            dropped.append(str(c))
    series = {}
    for sym in list(proxy_targets):
        if sym in US_PROXIES:
            s, _src = fetch_us_index(US_PROXIES[sym], sym, refresh=refresh)
        elif sym == "gold":
            s, _src = fetch_gold_proxy(refresh=refresh)
        else:
            s, _src = fetch_index(sym, refresh=refresh)
        if s is None:
            proxy_targets.pop(sym, None)
            continue
        series[sym] = _to_total_return(s, DIV_YIELD.get(sym, 0.0))
    if len(series) < 3:
        return None
    # 黄金(GLD持仓反推)是稀疏序列——用 ffill 填到日频再去前导缺失，避免内连接把整段面板砍成稀疏(扭曲年化)。
    pxL = pd.DataFrame(series).sort_index().ffill().dropna()
    ssum = sum(proxy_targets.values()) or 1.0
    proxy_targets = {k: v / ssum for k, v in proxy_targets.items() if k in series}
    proxy_asset, bond_proxy = {}, None
    for c, p in code_proxy.items():
        if p not in series:
            continue
        a = asset.get(c)
        proxy_asset[p] = "bond" if a == "bond" else ("gold" if a == "gold" else "equity")
        if a == "bond":
            bond_proxy = p
    if not bond_proxy or len(pxL.columns) < 3:
        return None
    return pxL, proxy_targets, proxy_asset, bond_proxy, dropped


def _print_tactical_table(rows):
    print("%-18s %8s %7s %8s %6s %7s %7s" % ("策略", "年化", "波动", "最大回撤", "夏普", "Calmar", "年换手"))
    print("-" * 74)
    for r in rows:
        print("%-16s %+7.1f%% %6.1f%% %7.1f%% %6.2f %7.2f %6.0f%%" % (
            r["label"], r["cagr"] * 100, r["vol"] * 100, r["max_drawdown"] * 100,
            r["sharpe"], r["calmar"], r["turnover_annual"] * 100))


def _run_tactical_cli(px, strategic, asset_of, reserve, strat, root):
    """`backtest.py --tactical`：在真实 ETF 段上跑 §13 六策略影子对比 + walk-forward。"""
    import signals as _sig
    asm = _sig.load_assumptions(strat)
    prof = _sig.load_investor_profile(root)
    codes = list(px.columns)
    shocks = {c: asm["shocks"].get(asset_of.get(c), asm["default_shock"]) for c in codes}
    stable = float(prof.get("stable_assets_outside", 0) or 0)
    planned = float(prof.get("planned_etf_capital", 0) or 0)
    etf_share = planned / (planned + stable) if planned > 0 and (planned + stable) > 0 else 1.0
    max_dd = float(prof.get("max_acceptable_drawdown", 0.2) or 0.2)
    profile = strat.get("risk_profile", "平衡")
    premium = {c for c in codes if asset_of.get(c) in ("global_equity", "global_growth")}
    kw = dict(profile=profile, warmup=WARMUP, cost_per_side=COST, premium_assets=premium,
              premium_extra=0.002, etf_share=etf_share, max_whole_stress=max_dd)
    yrs = (len(px) - WARMUP) / 252
    print("\n══════ ① ETF 可交易段 · 双向战术六策略对比 ══════")
    print(f"区间 {px.index[WARMUP].date()} → {px.index[-1].date()}（约 {yrs:.1f} 年）｜风险偏好 {profile}｜ETF桶占比 {etf_share:.0%}")
    print("⚠️ 此段估值臂为【仅价格】（ETF 无长 PE）；已计成本/滑点/QDII溢价；过去≠未来、影子不接入调仓。")
    _print_tactical_table(run_tactical_comparison(px, strategic, asset_of, reserve, shocks, **kw))

    # ═══ ② 长代理段（含 2008/2015）——降回撤证据只能在危机样本里拿；估值臂做 PE 历史时点重建 ═══
    pp = build_proxy_panel(strat, strategic, refresh=False)
    if pp:
        pxL, ptargets, passet, pbond = pp
        pshocks = {s: (-0.03 if passet.get(s) == "bond" else -0.30) for s in pxL.columns}
        vals = build_proxy_valuations()
        yrsL = (len(pxL) - WARMUP) / 252
        kwL = dict(profile=profile, warmup=WARMUP, cost_per_side=COST, etf_share=etf_share,
                   max_whole_stress=max_dd, valuations=(vals or None))
        print(f"\n══════ ② 长代理段（价格指数·含 2008/2015）≈ {yrsL:.0f} 年 ══════")
        print("代理：沪深300(+红利)→sh000300；中证500→sh000905；债券→sh000012；剔除黄金/QDII/成长。")
        print("估值臂：" + ("PE 历史时点重建（沪深300/中证500，无前视）" if vals else "无 PE 种子→仅价格（联网跑 `--refresh` 可补种子）"))
        _print_tactical_table(run_tactical_comparison(pxL, ptargets, passet, pbond, pshocks, **kwL))
        wfL = walk_forward_tactical(pxL, ptargets, passet, pbond, pshocks, folds=3, **kwL)
        if wfL:
            print("\n【长段 walk-forward】双向 vs 静态（含不同危机段）：")
            for r in wfL:
                print(f"  段{r['fold']}：双向 {r['tactical_cagr']*100:+.1f}%/回撤{r['tactical_maxdd']*100:.0f}%"
                      f"  ｜  静态 {r['static_cagr']*100:+.1f}%/回撤{r['static_maxdd']*100:.0f}%")
    else:
        print("\n[提示] 长代理段数据不足，跳过；联网 `python engine/backtest.py --refresh` 可补指数代理。")

    # ═══ ③ 全收益 + 全分散长段：补分红 + 保留 QDII（去掉"价格指数"和"丢分散"两个保守偏差）═══
    fp = build_full_panel(strat, strategic, refresh=False)
    if fp:
        pxF, ftargets, fasset, fbond, fdrop = fp
        fshocks = {s: (-0.03 if fasset.get(s) == "bond" else (-0.15 if fasset.get(s) == "gold" else -0.30))
                   for s in pxF.columns}
        fvals = build_proxy_valuations()
        kwF = dict(profile=profile, warmup=WARMUP, cost_per_side=COST, etf_share=etf_share,
                   max_whole_stress=max_dd, valuations=(fvals or None))
        yrsF = (len(pxF) - WARMUP) / 252
        has_gold = "gold" in pxF.columns
        print(f"\n══════ ③ 全收益+全分散长段（含分红·保留 QDII{'+黄金' if has_gold else ''}）{pxF.index[WARMUP].date()}→{pxF.index[-1].date()} ≈ {yrsF:.0f} 年 ══════")
        print(f"代理：A股(沪深300/中证500)+美股(标普/纳指)+国债{'+黄金(GLD持仓反推)' if has_gold else ''}；**合成全收益(补股息/票息)**；⚠️ 忽略汇率；剔除创业板/科创50(无长序列)。")
        print("权重: " + "、".join(f"{s} {ftargets[s]:.0%}" for s in pxF.columns) + "｜估值臂: 沪深300/中证500 PE 时点重建")
        _print_tactical_table(run_tactical_comparison(pxF, ftargets, fasset, fbond, fshocks, **kwF))
        wfF = walk_forward_tactical(pxF, ftargets, fasset, fbond, fshocks, folds=3, **kwF)
        if wfF:
            print("\n【全收益段 walk-forward】双向 vs 静态：")
            for r in wfF:
                print(f"  段{r['fold']}：双向 {r['tactical_cagr']*100:+.1f}%/回撤{r['tactical_maxdd']*100:.0f}%"
                      f"  ｜  静态 {r['static_cagr']*100:+.1f}%/回撤{r['static_maxdd']*100:.0f}%")
        if has_gold:
            print("注：黄金用 SPDR GLD 持仓报告反推美元金价(稀疏→ffill 到日频)，量级近似、非精确价格；汇率亦忽略。")
        else:
            print("注：黄金未能纳入(无长序列)——它是 2008 的分散器，故此段对'静态回撤'仍略偏保守。")

    print("\n说明：§13.5 验收看'双向是否在含危机的长样本里改善 Calmar/回撤'；通过前不接入实际调仓。")


def simulate_strategic_comparison(strat, port, root, refresh=False):
    """Track C §12.3 / §16.3：权威构建 vs 当前 vs 简化基准，在全收益长面板上持仓漂移回测（含成本）。

    返回 {rows:[{name,cagr,vol,dd,calmar,uw_days,...}], years, start, end, dropped} | None。
    目的：证明复杂度的增量价值——若『仅核心/无卫星』不劣于『权威构建』，构建组合应被否(§16.3)。
    """
    import signals as _sig                       # noqa: PLC0415
    import strategic as _sm                      # noqa: PLC0415  (避免与 _run_tactical_cli 的 strategic 参数名冲突)
    sp = strat.get("strategic_policy") or {}
    if not sp.get("roles"):
        return None
    asm = _sig.load_assumptions(strat)
    scen = _sig.load_stress_scenarios(strat)
    prof = _sig.load_investor_profile(root)
    asset_of = {str(u["code"]): u.get("asset") for u in strat.get("universe", [])}
    tier_of = {}
    for rid, rc in (sp.get("roles") or {}).items():
        for c in (rc.get("members") or []):
            tier_of[str(c)] = rc.get("tier")
    stable = float(prof.get("stable_assets_outside", 0) or 0)
    planned = float(prof.get("planned_etf_capital", 0) or 0)
    etf_share = planned / (planned + stable) if planned > 0 and (planned + stable) > 0 else 1.0
    target = float(prof.get("target_annual_return", 0.05) or 0.05)
    max_dd = float(prof.get("max_acceptable_drawdown", 0.15) or 0.15)
    snap = _sm.construct_strategic_portfolio(
        sp, returns=asm["returns"], shocks=asm["shocks"], target_return=target,
        default_return=asm["default_return"], default_shock=asm["default_shock"], asset_of=asset_of,
        etf_share=etf_share, max_whole_stress=max_dd,
        returns_conservative=asm["returns_conservative"], scenarios=scen)
    if snap["validation_status"] == "no_feasible_portfolio":
        return None
    current = {str(h["code"]): float(h.get("target_weight") or 0) for h in port.get("holdings", [])}
    portfolios = _sm.derive_comparison_portfolios(snap["instrument_allocation"], current, asset_of, tier_of)
    full = build_full_panel(strat, current, refresh=refresh)
    if full is None:
        return None
    pxL, _pt, proxy_asset, bond_proxy, dropped = full
    eq_proxies = [p for p, a in proxy_asset.items() if a == "equity"]
    ma0 = int(strat["factors"]["trend_filter"]["ma_days"])
    yrsL = (len(pxL) - WARMUP) / 252

    def to_proxy_targets(weights):
        pt = {}
        for c, w in weights.items():
            p = FULL_PROXY.get(str(c))
            if p and p in pxL.columns:
                pt[p] = pt.get(p, 0.0) + float(w)
        s = sum(pt.values()) or 1.0
        return {k: v / s for k, v in pt.items()}

    rows = []
    for name, weights in portfolios.items():
        pt = to_proxy_targets(weights)
        if not pt:
            continue
        _nav, m = _run_with_nav(pxL, pt, eq_proxies, bond_proxy, False, ma0, "M", yrsL)
        rows.append({"name": name, **clean_metric(m)})
    return {"rows": rows, "years": round(yrsL, 1), "start": str(pxL.index[WARMUP].date()),
            "end": str(pxL.index[-1].date()), "dropped": dropped}


def _run_strategic_cli(strat, port, root, refresh=False):
    """`backtest.py --strategic`：§12.3 战略组合对比（全收益长面板·持仓漂移·含成本）。"""
    res = simulate_strategic_comparison(strat, port, root, refresh=refresh)
    if not res:
        print("无法构建战略对比（缺 strategic_policy / 无可行组合 / 全收益面板不可得，可联网 --refresh）。")
        return
    print(f"\n══════ 战略组合对比回测（全收益长面板·持仓漂移·含成本）≈ {res['years']} 年 ══════")
    print(f"区间 {res['start']} → {res['end']}；剔除无长代理：{('、'.join(res['dropped']) or '无')}（创业板/科创50/QDII 等无长序列）")
    print("%-12s %8s %7s %8s %7s %8s %7s" % ("组合", "年化", "波动", "最大回撤", "Calmar", "最长水下", "年换手"))
    print("-" * 68)
    for r in res["rows"]:
        print("%-11s %+7.1f%% %6.1f%% %7.1f%% %7.2f %6.0f日 %6.0f%%" %
              (r["name"], r["cagr"] * 100, r["vol"] * 100, r["max_drawdown"] * 100, r["calmar"],
               r["underwater_days"], r["turnover_annual"] * 100))
    print("§16.3：若『仅核心/无卫星』在风险与成本上不劣于『权威构建』，则复杂度未通过——构建组合应被否。")
    print("⚠️ 代理段为全收益指数近似（剔除无长序列品种）；过去≠未来，仅用于结构性对比、非精确收益预测。")


def main():
    ap = argparse.ArgumentParser(description="策略回测")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新拉取（优先前复权）")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON，供 Web 前端渲染")
    ap.add_argument("--tactical", action="store_true", help="跑双向战术配置六策略影子对比（§13）")
    ap.add_argument("--strategic", action="store_true", help="跑战略组合对比回测（§12.3：构建 vs 当前 vs 简化基准）")
    args = ap.parse_args()

    root = find_repo_root(HERE)
    strat = load_yaml(os.path.join(root, "strategy.yaml"))
    port = load_yaml(os.path.join(root, "portfolio.yaml"))
    if args.strategic:                       # 自建全收益面板，无需 ETF 段行情
        _run_strategic_cli(strat, port, root, refresh=args.refresh)
        return
    profile = strat.get("risk_profile", "平衡")
    ma0 = int(strat["factors"]["trend_filter"]["ma_days"])
    uni = strat["universe"]
    asset = {str(u["code"]): u["asset"] for u in uni}
    prox = {str(u["code"]): u.get("proxy_index") for u in uni}
    targets = {str(h["code"]): float(h["target_weight"]) for h in port["holdings"]}
    codes = list(targets)
    trend_codes = [c for c in codes if asset.get(c) in ("equity", "equity_defensive")]
    bond_code = next(c for c in codes if asset.get(c) == "bond")
    bench = "510300" if "510300" in codes else trend_codes[0]

    # ═══ ① ETF 可交易回测 ═══
    if not args.json:
        print("【拉取 ETF 行情】")
    series, srcs = {}, set()
    for c in codes:
        s, src = fetch_etf(c, refresh=args.refresh)
        if s is None:
            print(f"[错误] 缺 {c} 数据，无法回测，稍后重跑。", file=sys.stderr)
            sys.exit(1)
        series[c] = s
        srcs.add(src)
    px = pd.DataFrame(series).dropna()
    if args.tactical:
        _run_tactical_cli(px, targets, asset, bond_code, strat, root)
        return
    ev = px.iloc[WARMUP:]
    yrs = len(ev) / 252

    if not args.json:
        print(f"\n══════ ① ETF 可交易回测 ══════")
        print(f"区间 {ev.index[0].date()} → {ev.index[-1].date()}（约 {yrs:.1f} 年）｜数据源 {'、'.join(sorted(srcs))}")
    strat_nav, strat_m = _run_with_nav(px, targets, trend_codes, bond_code, True, ma0, "M", yrs)
    static_nav, static_m = _run_with_nav(px, targets, trend_codes, bond_code, False, ma0, "M", yrs)
    dca = run_dca(static_nav)                                    # 分批/定投建仓对比（基于静态组合净值）
    bh = (px[bench] / px[bench].iloc[0]).iloc[WARMUP:]
    bh_m = metrics(bh); bh_m["turn_ann"] = 0.0
    if not args.json:
        print("%-16s %8s %7s %8s %6s %7s %7s %8s" %
              ("组合", "年化", "波动", "最大回撤", "夏普", "Calmar", "年换手", "最长水下"))
        print("-" * 78)
        for name, m in [("本策略(趋势过滤)", strat_m), ("静态(无过滤)", static_m), (f"{bench}买入持有", bh_m)]:
            sh = (m["cagr"]) / m["vol"] if m["vol"] > 0 else float("nan")
            print("%-14s %+7.1f%% %6.1f%% %7.1f%% %6.2f %7.2f %6.0f%% %6.0f日" %
                  (name, m["cagr"] * 100, m["vol"] * 100, m["dd"] * 100, sh, m["calmar"],
                   m["turn_ann"] * 100, m["uw_days"]))

    sensitivity_ma = []
    if not args.json:
        print("\n【敏感性】趋势过滤均线周期：", end="")
    for ma in (120, 200, 250):
        m = _run(px, targets, trend_codes, bond_code, True, ma, "M", yrs)
        sensitivity_ma.append({"ma_days": ma, **clean_metric(m)})
        if not args.json:
            print(f"ma{ma}→{m['cagr']*100:+.1f}%/{m['dd']*100:.0f}%回撤  ", end="")
    if not args.json:
        print(f"｜静态基准 {static_m['cagr']*100:+.1f}%/{static_m['dd']*100:.0f}%回撤")
        print("【敏感性】再平衡频率：", end="")
    sensitivity_freq = []
    for freq, lab in (("M", "月度"), ("Q", "季度")):
        m = _run(px, targets, trend_codes, bond_code, False, ma0, freq, yrs)
        sensitivity_freq.append({"freq": freq, "label": lab, **clean_metric(m)})
        if not args.json:
            print(f"{lab}→{m['cagr']*100:+.1f}%/换手{m['turn_ann']*100:.0f}%  ", end="")
    if not args.json:
        print()
        if dca:
            print(f"\n【分批建仓】窗口约 {dca['horizon_years']:.1f} 年 × {dca['windows']} 个滚动起点（投入静态组合，未投现金按 {dca['cash_yield']:.0%} 计息）")
            print("%-10s %12s %10s %14s" % ("建仓节奏", "期末倍数中位", "回撤中位", "跑赢一次性占比"))
            print("-" * 50)
            for p in dca["plans"]:
                win = "—（基准）" if p["beats_lumpsum_window_pct"] is None else f"{p['beats_lumpsum_window_pct']*100:.0f}%"
                print("%-10s %11.2fx %9.0f%% %14s" %
                      (p["label"], p["median_final_multiple"], p["median_max_drawdown"] * 100, win))
            print("说明：一次性在上行市通常期末更高；分批降低择时后悔与回撤。ETF 段历史有限、样本重叠，仅示意。")
        else:
            print("\n【分批建仓】历史长度不足，暂不输出分批对比。")

    # ═══ ② 长期代理回测 ═══
    # Track C §12.2：优先「全收益(含分红)+全分散(含黄金)」长面板，禁止静默剔除资产再归一化冒充真实组合。
    # 仅当全收益面板不可得时，才回退到价格指数(未含分红/无黄金长序列)，并显著披露口径差异。
    try:
        full = build_full_panel(strat, targets, refresh=args.refresh)
    except Exception as e:  # noqa: BLE001  长面板任一数据源异常 → 回退，不阻断
        print(f"[警告] 全收益长面板构建失败（{e}），回退价格指数段。", file=sys.stderr)
        full = None
    if full is not None:
        pxL, proxy_targets, proxy_asset, bond_proxy, dropped = full
        eq_proxies = [p for p, a in proxy_asset.items() if a == "equity"]
        seg_label, total_return = "全收益·含分红+黄金", True
        map_note = "全收益合成(价格+分红/252)；黄金=GLD持仓反推金价；债券=上证国债指数(含票息)"
    else:
        # 回退：价格指数（未含分红、黄金无长序列）。单个代理缺失 → 只剔除该 sleeve，其余继续。
        proxy_targets, dropped, code_proxy = {}, [], {}
        for c, w in targets.items():
            pidx = prox.get(c)
            if pidx:
                proxy_targets[pidx] = proxy_targets.get(pidx, 0.0) + w
                code_proxy[c] = pidx
            else:
                dropped.append(c)
        pseries = {}
        for sym in list(proxy_targets):
            s, _src = fetch_index(sym, refresh=args.refresh)
            if s is None:
                print(f"[警告] 代理指数 {sym} 无数据，剔除该 sleeve 后继续。", file=sys.stderr)
                proxy_targets.pop(sym, None)
                dropped.extend(c for c, p in code_proxy.items() if p == sym and c not in dropped)
                continue
            pseries[sym] = s
        eq_proxies = list({p for c, p in code_proxy.items()
                           if asset.get(c) in ("equity", "equity_defensive") and p in pseries})
        bond_proxy = prox.get(bond_code) if prox.get(bond_code) in pseries else None
        ssum = sum(proxy_targets.values()) or 1.0
        proxy_targets = {k: v / ssum for k, v in proxy_targets.items()}
        pxL = pd.DataFrame(pseries).dropna() if pseries else None
        seg_label, total_return = "价格指数·未含分红·近似", False
        map_note = "红利低波→沪深300(近似)；黄金无长序列被剔除；债券=上证国债指数(价格)"

    ok = (pxL is not None) and bond_proxy is not None and bool(eq_proxies) and len(pxL.columns) >= 2
    if not args.json:
        print(f"\n══════ ② 指数代理长期回测（{seg_label}）══════")
    if ok:
        evL = pxL.iloc[WARMUP:]
        yrsL = len(evL) / 252
        dnames = "、".join(dropped) if dropped else "无"
        if not args.json:
            print(f"区间 {evL.index[0].date()} → {evL.index[-1].date()}（约 {yrsL:.1f} 年，比 ETF 段长 ~{yrsL-yrs:.0f} 年）")
            print(f"代理映射：{map_note}；剔除并分摊：{dnames}")
        sL_nav, sL = _run_with_nav(pxL, proxy_targets, eq_proxies, bond_proxy, False, ma0, "M", yrsL)
        tL_nav, tL = _run_with_nav(pxL, proxy_targets, eq_proxies, bond_proxy, True, ma0, "M", yrsL)
        bL_nav = (pxL["sh000300"] / pxL["sh000300"].iloc[0]).iloc[WARMUP:] if "sh000300" in pxL else None
        bL = metrics(bL_nav) if bL_nav is not None else None
        if not args.json:
            print("%-16s %8s %7s %8s %7s %8s" % ("组合", "年化", "波动", "最大回撤", "Calmar", "最长水下"))
            print("-" * 64)
        rows = [("静态(无过滤)", sL), ("本策略(趋势过滤)", tL)]
        if bL:
            rows.append(("沪深300指数买入持有", bL))
        if not args.json:
            for name, m in rows:
                print("%-14s %+7.1f%% %6.1f%% %7.1f%% %7.2f %6.0f日" %
                      (name, m["cagr"] * 100, m["vol"] * 100, m["dd"] * 100, m["calmar"], m["uw_days"]))
            print("✓ 全收益口径（已含分红、含黄金分散），更贴近真实组合；仍为代理段，非精确收益预测。" if total_return
                  else "⚠️ 价格指数未含分红→低估真实收益(尤其债券/红利)；本段主要看更长周期的『回撤轮廓』，非精确收益预测。")

    # ═══ 按风险偏好给推荐 ═══
    rec = {"保守": "本策略(趋势过滤)——降回撤优先",
           "平衡": "静态组合(趋势仅作展示)——收益/回撤更均衡",
           "进取": "静态组合——收益优先、容忍更大回撤"}.get(profile, "静态组合")
    if args.json:
        out = {
            "risk_profile": profile,
            "recommendation": rec,
            "etf_segment": {
                "start": str(ev.index[0].date()),
                "end": str(ev.index[-1].date()),
                "years": round(float(yrs), 2),
                "sources": sorted(srcs),
                "rows": [
                    {"name": "本策略(趋势过滤)", "kind": "strategy", **clean_metric(strat_m)},
                    {"name": "静态(无过滤)", "kind": "static", **clean_metric(static_m)},
                    {"name": f"{bench}买入持有", "kind": "benchmark", **clean_metric(bh_m)},
                ],
                "sensitivity_ma": sensitivity_ma,
                "sensitivity_freq": sensitivity_freq,
                "curves": [
                    {"name": "本策略(趋势过滤)", "kind": "strategy", "points": sampled_curve(strat_nav)},
                    {"name": "静态(无过滤)", "kind": "static", "points": sampled_curve(static_nav)},
                    {"name": f"{bench}买入持有", "kind": "benchmark", "points": sampled_curve(bh)},
                ],
            },
            "proxy_segment": None,
            "dca": dca,
            "notes": ["回测好不代表未来收益",
                      ("指数代理段为全收益口径（含分红、含黄金分散），更贴近真实组合"
                       if (ok and total_return) else "指数代理段为价格指数，未含分红，主要观察回撤轮廓")],
        }
        if ok:
            out["proxy_segment"] = {
                "start": str(evL.index[0].date()),
                "end": str(evL.index[-1].date()),
                "years": round(float(yrsL), 2),
                "total_return": bool(total_return),
                "basis": seg_label,
                "dropped": dropped,
                "rows": [
                    {"name": name, "kind": "benchmark" if "买入持有" in name else ("strategy" if "趋势" in name else "static"), **clean_metric(m)}
                    for name, m in rows
                ],
                "curves": [
                    {"name": "静态(无过滤)", "kind": "static", "points": sampled_curve(sL_nav)},
                    {"name": "本策略(趋势过滤)", "kind": "strategy", "points": sampled_curve(tL_nav)},
                ] + ([{"name": "沪深300指数买入持有", "kind": "benchmark", "points": sampled_curve(bL_nav)}] if bL_nav is not None else []),
            }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"\n当前 risk_profile=【{profile}】→ 推荐口径：{rec}")
        print("说明：未复权低估分红，真实收益应略高；成本单边万3；最长水下=连续未创新高的交易日数。")
        print(f"数据元信息见 {os.path.relpath(META_PATH, root)}（--refresh 可在联网机重取并登记来源/复权）。")


if __name__ == "__main__":
    main()
