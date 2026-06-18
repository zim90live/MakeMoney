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
COST = 0.00005        # 单边交易成本（万0.5，与银河证券佣金同步；未含买卖价差）
RISK_FREE_RATE = 0.02 # §0C #5 夏普口径的无风险利率（短债/货基量级，可配）——真夏普=(年化−rf)/波动，不再用裸 cagr/vol


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


def sharpe_ratio(cagr, vol, rf=RISK_FREE_RATE):
    """真夏普：(年化收益 − 无风险利率) / 年化波动；vol≤0 → nan。

    §0C #5：修了早先"裸 cagr/vol（漏减 rf）"的口径——那会系统性高估约 rf/vol（rf≈2% 时对 vol=20% 的组合虚高 0.1）。
    """
    return (cagr - rf) / vol if vol and vol > 0 else float("nan")


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
        # M8（2026-06-10 审查）：决策点之间持仓权重随收益**漂移**（现金计 0 收益）。
        # 此前 w 在决策点之间保持常数 = 隐含"每日免费再平衡"：5_25 模式的偏离恒为 0 永不触发
        # （与 static 字节级恒等），且全部模式的换手/成本被低估。漂移后 5/25 基准才真实。
        day_r = {c: float(rets[c].iloc[i]) for c in codes}
        port_r = sum(w.get(c, 0) * day_r[c] for c in codes)   # 未投现金计 0
        nav *= (1.0 + port_r)
        if 1.0 + port_r > 0:
            w = {c: w.get(c, 0) * (1.0 + day_r[c]) / (1.0 + port_r) for c in codes}
            cash = cash / (1.0 + port_r)
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
        sharpe = sharpe_ratio(m["cagr"], m["vol"])
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


def fetch_usdcny(refresh=False):
    """USD/CNY 日频汇率（中国银行/新浪 `currency_boc_sina`，中行折算价÷100 = 人民币/美元，2005~）。

    返回 (Series, source)|(None,None)。供长面板把美元口径 QDII/黄金收益折算成人民币
    （人民币升值 → 持有美元资产换回人民币缩水，补上这层回测才诚实）。种子 engine/data/idx_usdcny.csv。"""
    cache = os.path.join(DATA_DIR, "idx_usdcny.csv")
    if os.path.exists(cache) and not refresh:
        df = _norm(pd.read_csv(cache))
        _ensure_meta_from_cache("idx_usdcny", df, "缓存(旧数据)", "汇率(中行折算价÷100)")
        return df.set_index("date")["close"], "缓存"
    os.makedirs(DATA_DIR, exist_ok=True)
    for _ in range(3):
        try:
            d = ak.currency_boc_sina(symbol="美元", start_date="20050101", end_date=date.today().strftime("%Y%m%d"))
            col = next((c for c in ("中行折算价", "央行中间价") if c in (d.columns if d is not None else [])), None)
            if d is not None and not d.empty and col:
                out = _norm(pd.DataFrame({"date": d["日期"], "close": pd.to_numeric(d[col], errors="coerce") / 100.0}).dropna())
                _persist(out, cache, "idx_usdcny", "中国银行/新浪", "汇率(中行折算价÷100)")
                return out.set_index("date")["close"], "中国银行/新浪"
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


def build_full_panel(strat, targets, refresh=False, bond_carry=None, fx_adjust=True):
    """全收益+全分散长面板（A股+美股QDII+国债，含分红；剔除创业板/科创50）。

    fx_adjust（#1，缺省 True）：把**美元计价代理（标普/纳指/黄金）按 USD/CNY 折算成人民币口径**
    （人民币 2005→2026 升值约 21%，持有美元资产换回人民币会缩水——补上这层回测才诚实）；
    传 False 做"忽略汇率"敏感性对照。无 `idx_usdcny.csv` 种子时自动回退为忽略汇率。
    bond_carry（批4）：覆盖债券代理年化票息——None=用 DIV_YIELD 默认（3%）；0.0 做零息敏感性。
    返回 (pxL_全收益, proxy_targets, proxy_asset_of, bond_proxy, dropped)|None。
    """
    asset = {str(u["code"]): u["asset"] for u in strat["universe"]}
    bond_code = next((str(c) for c, a in asset.items() if a == "bond"), None)
    bond_sym = FULL_PROXY.get(bond_code) if bond_code else None
    proxy_targets, code_proxy, dropped = {}, {}, []
    for c, w in targets.items():
        p = FULL_PROXY.get(str(c))
        if p:
            proxy_targets[p] = proxy_targets.get(p, 0.0) + float(w)
            code_proxy[str(c)] = p
        else:
            dropped.append(str(c))
    usd_syms = set(US_PROXIES) | {"gold"}          # 美元计价代理 → 需折人民币
    fx_series = None
    if fx_adjust and any(sym in usd_syms for sym in proxy_targets):
        fx_series, _fxsrc = fetch_usdcny(refresh=refresh)   # 种子缺失 → None → 自动回退忽略汇率
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
        yld = bond_carry if (bond_carry is not None and sym == bond_sym) else DIV_YIELD.get(sym, 0.0)
        tr = _to_total_return(s, yld)
        if fx_series is not None and sym in usd_syms:       # 美元口径 × USD/CNY 水平 → 人民币口径（捕捉汇率移动）
            tr = (tr * fx_series.reindex(tr.index).ffill().bfill()).dropna()
        series[sym] = tr
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


# ─── 历史危机情景标定（§0C #1）：从长面板按【真实峰谷】重算各资产冲击向量，替代拍脑袋"示意档" ───
#   每个历史危机给一个【一致的横截面向量】：以该窗口内跌得最深的权益代理为锚，取锚的峰值日→谷值日，
#   所有资产用同一对日期算 peak→trough 收益（如此能如实捕捉黄金/债券在权益低点的真实对冲表现）。
#   口径：价格指数（非全收益）——与"沪深300 在 2008 下跌约 70%"的直觉一致。
CRISIS_WINDOWS = [
    ("2008金融危机", "2007-10-01", "2009-03-31"),
    ("2015股灾", "2015-06-01", "2016-02-29"),
    ("2018贸易战去杠杆", "2018-01-01", "2019-01-31"),
    ("2020疫情闪崩", "2020-01-01", "2020-04-30"),
    ("2022加息回调", "2021-12-01", "2022-12-31"),
]
# 资产类 → 长价格代理；china_growth 无长 ETF 代理，用中证500（更高 beta 的 A 股）近似并标注
CRISIS_PROXY = {
    "equity": "sh000300", "equity_defensive": "sh000300", "china_growth": "sh000905",
    "global_equity": "spx", "global_growth": "ixic", "bond": "sh000012", "gold": "gold",
}
CRISIS_ANCHOR_ASSETS = ("equity", "china_growth", "global_equity", "global_growth")


def _load_crisis_series(refresh=False):
    """加载危机标定所需的价格代理（价格口径、非全收益——峰谷与"下跌 X%"直觉一致）。"""
    out = {}
    for sym in set(CRISIS_PROXY.values()):
        if sym in US_PROXIES:
            s, _src = fetch_us_index(US_PROXIES[sym], sym, refresh=refresh)
        elif sym == "gold":
            s, _src = fetch_gold_proxy(refresh=refresh)
        else:
            s, _src = fetch_index(sym, refresh=refresh)
        if s is not None and len(s):
            out[sym] = s.sort_index()
    return out


def compute_crisis_scenarios(refresh=False):
    """从长面板按真实峰谷标定历史危机的资产冲击向量（§0C #1）。

    返回 [{name, window:[peak,trough], anchor, shocks:{asset:shock}, note}]；
    缺数据的窗口/资产如实跳过并在 note 标注。纯标定，不预测。
    """
    series = _load_crisis_series(refresh=refresh)
    if len(series) < 3:
        return None
    daily = pd.DataFrame(series).sort_index().ffill()
    scenarios = []
    for name, start, end in CRISIS_WINDOWS:
        seg = daily.loc[start:end].dropna(how="all")
        if len(seg) < 20:
            continue
        # 锚 = 该窗口内跌得最深的可得权益代理（用其峰→谷日期统一横截面）
        best = None  # (dd, sym, t_peak, t_trough)
        for asset in CRISIS_ANCHOR_ASSETS:
            sym = CRISIS_PROXY[asset]
            if sym not in seg.columns:
                continue
            s = seg[sym].dropna()
            if len(s) < 20:
                continue
            dd_series = s / s.cummax() - 1.0
            t_trough = dd_series.idxmin()
            t_peak = s.loc[:t_trough].idxmax()
            dd = float(dd_series.min())
            if best is None or dd < best[0]:
                best = (dd, sym, t_peak, t_trough)
        if best is None:
            continue
        _dd, anchor_sym, t_peak, t_trough = best
        shocks, missing = {}, []
        for asset, sym in CRISIS_PROXY.items():
            if sym not in seg.columns:
                missing.append(asset)
                continue
            s = seg[sym].dropna()
            sp, st = s.loc[:t_peak], s.loc[:t_trough]
            if len(sp) == 0 or len(st) == 0:
                missing.append(asset)
                continue
            shocks[asset] = round(float(st.iloc[-1] / sp.iloc[-1] - 1.0), 4)
        if "equity" not in shocks:
            continue
        shocks["short_bond"] = round(shocks.get("bond", 0.0) * 0.5, 4)   # 短债无长代理：取国债一半近似
        shocks["cash"] = 0.0
        note = []
        if "china_growth" in shocks:
            note.append("china_growth 用中证500代理")
        if missing:
            note.append("缺代理跳过: " + ",".join(missing))
        note.append("据真实峰谷")
        scenarios.append({
            "name": name, "window": [str(t_peak.date()), str(t_trough.date())],
            "anchor": anchor_sym, "shocks": shocks, "note": "；".join(note),
        })
    return scenarios or None


def _print_tactical_table(rows):
    print("%-18s %8s %7s %8s %6s %7s %7s" % ("策略", "年化", "波动", "最大回撤", "夏普", "Calmar", "年换手"))
    print("-" * 74)
    for r in rows:
        print("%-16s %+7.1f%% %6.1f%% %7.1f%% %6.2f %7.2f %6.0f%%" % (
            r["label"], r["cagr"] * 100, r["vol"] * 100, r["max_drawdown"] * 100,
            r["sharpe"], r["calmar"], r["turnover_annual"] * 100))
    print(f"  注：夏普 =（年化 − 无风险 {RISK_FREE_RATE * 100:.0f}%）/ 波动（真夏普口径，已减 rf）。")


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
        print(f"代理：A股(沪深300/中证500)+美股(标普/纳指)+国债{'+黄金(GLD持仓反推)' if has_gold else ''}；**合成全收益(补股息/票息)**；**美元资产已按 USD/CNY 折人民币口径**(2005→2026 升值约21%·拖累QDII约1%/年)；剔除创业板/科创50(无长序列)。")
        print("权重: " + "、".join(f"{s} {ftargets[s]:.0%}" for s in pxF.columns) + "｜估值臂: 沪深300/中证500 PE 时点重建")
        _print_tactical_table(run_tactical_comparison(pxF, ftargets, fasset, fbond, fshocks, **kwF))
        wfF = walk_forward_tactical(pxF, ftargets, fasset, fbond, fshocks, folds=3, **kwF)
        if wfF:
            print("\n【全收益段 walk-forward】双向 vs 静态：")
            for r in wfF:
                print(f"  段{r['fold']}：双向 {r['tactical_cagr']*100:+.1f}%/回撤{r['tactical_maxdd']*100:.0f}%"
                      f"  ｜  静态 {r['static_cagr']*100:+.1f}%/回撤{r['static_maxdd']*100:.0f}%")
        if has_gold:
            print("注：黄金用 SPDR GLD 持仓报告反推美元金价(稀疏→ffill 到日频)，量级近似、非精确价格；已同 QDII 按 USD/CNY 折人民币。")
        else:
            print("注：黄金未能纳入(无长序列)——它是 2008 的分散器，故此段对'静态回撤'仍略偏保守。")

    print("\n说明：§13.5 验收看'双向是否在含危机的长样本里改善 Calmar/回撤'；通过前不接入实际调仓。")


# 批3收尾：数据驱动收益区间——sleeve→估波动用的代理（china_growth 无长序列→ETF 短史，缺则中证500兜底）。
SLEEVE_VOL_PROXY = {
    "bond": ("index", "sh000012"), "equity": ("index", "sh000300"),
    "equity_defensive": ("index", "sh000300"), "global_equity": ("us", "spx"),
    "global_growth": ("us", "ixic"), "gold": ("gold", None),
    "china_growth": ("etf_avg", ["159915", "588000"]),
}


def _annualized_vol(series):
    if series is None:
        return None
    r = series.pct_change().dropna()
    return float(r.std() * (252 ** 0.5)) if len(r) > 60 else None


def _sleeve_vol(kind, ref, refresh=False):
    if kind == "index":
        return _annualized_vol(fetch_index(ref, refresh=refresh)[0])
    if kind == "us":
        return _annualized_vol(fetch_us_index(US_PROXIES[ref], ref, refresh=refresh)[0])
    if kind == "gold":
        return _annualized_vol(fetch_gold_proxy(refresh=refresh)[0])
    if kind == "etf_avg":
        vols = [v for c in ref if (v := _annualized_vol(fetch_etf(c, refresh=refresh)[0])) is not None]
        if vols:
            return sum(vols) / len(vols)
        return _annualized_vol(fetch_index("sh000905", refresh=refresh)[0])   # 兜底：中证500
    return None


def compute_return_intervals(strat, refresh=False):
    """数据驱动收益区间（批3收尾，回答"靠谱算法"）：保守/乐观折扣按各 sleeve 的历史年化波动率缩放
    （波动越大折扣越大；系数标定为让 A 股核心权益得到 default haircut）；且**自承乐观的成长桶
    （china_growth/global_growth）保守值封顶在核心权益保守值**——最坏情形下不假设乐观成长跑赢普通股票。

    用 `backtest.py --return-intervals` 复算。返回 {asset:{vol,haircut,central,conservative,optimistic}}|None。
    """
    import signals as _sig                       # noqa: PLC0415
    asm = _sig.load_assumptions(strat)
    central, base_haircut = asm["returns"], asm["return_haircut"]
    vols = {a: v for a, (k, r) in SLEEVE_VOL_PROXY.items()
            if (v := _sleeve_vol(k, r, refresh=refresh)) is not None}
    eq_vol = vols.get("equity")
    if not eq_vol:
        return None
    k = base_haircut / eq_vol
    eq_cons = round(central.get("equity", 0.07) - base_haircut, 4)
    growth = {"china_growth", "global_growth"}
    out = {}
    for asset, v in vols.items():
        c = central.get(asset)
        if c is None:
            continue
        hc = round(k * v, 4)
        cons = round(c - hc, 4)
        if asset in growth:
            cons = min(cons, eq_cons)            # 最坏情形：乐观成长不假设跑赢核心权益
        out[asset] = {"vol": round(v, 4), "haircut": hc, "central": round(c, 4),
                      "conservative": cons, "optimistic": round(c + hc, 4)}
    return out


def simulate_strategic_comparison(strat, port, root, refresh=False,
                                  constructed_override=None, construct_meta=None):
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
    stable = float(_sm.employment_resilience(prof)["risk_buffer_available"])
    planned = float(prof.get("planned_etf_capital", 0) or 0)
    etf_share = planned / (planned + stable) if planned > 0 and (planned + stable) > 0 else 1.0
    profile_target = float(prof.get("target_annual_return", 0.05) or 0.05)
    target_meta = _sm.resolve_construct_target(
        sp, profile_target, planned, prof.get("stable_assets_outside", 0),
        stable_return=prof.get("stable_assets_yield"))
    target = target_meta["construct_target"]
    max_dd = float(prof.get("max_acceptable_drawdown", 0.15) or 0.15)
    construct_budget = _sm.resolve_construct_budget(sp, max_dd)
    current = {str(h["code"]): float(h.get("target_weight") or 0) for h in port.get("holdings", [])}
    full = build_full_panel(strat, current, refresh=refresh)
    if full is None:
        return None
    pxL, _pt, proxy_asset, bond_proxy, dropped = full
    wk = pxL.resample("W").last().pct_change().dropna()
    code_returns = {}
    for code in asset_of:
        proxy = FULL_PROXY.get(code)
        if proxy in wk.columns:
            code_returns[code] = wk[proxy].tolist()
    construct_cov = _sm.shrinkage_covariance(code_returns)
    exposure_of = {str(u["code"]): u.get("exposure_id") or u.get("index") or u.get("proxy_index") or str(u["code"])
                   for u in strat.get("universe", [])}  # 批3：与 live 一致，暴露身份优先 exposure_id
    if constructed_override:
        snap = {"instrument_allocation": {str(c): float(w) for c, w in constructed_override.items()},
                "validation_status": "passed"}
        construct_source = "live_construct_override"
    else:
        snap = _sm.construct_strategic_portfolio(
            sp, returns=asm["returns"], shocks=asm["shocks"], target_return=target,
            default_return=asm["default_return"], default_shock=asm["default_shock"], asset_of=asset_of,
            etf_share=etf_share, max_whole_stress=construct_budget,
            returns_conservative=asm["returns_conservative"], scenarios=scen,
            exposure_of=exposure_of, covariance=construct_cov, incumbent_codes=current)
        if snap["validation_status"] == "no_feasible_portfolio":
            return None
        construct_source = "frozen_assumption_reconstruction"
    # 批4(§0B #5-①)：把"无20年长代理的品种"(成长卫星 159915/588000) 统一从所有被比组合剔除并各自归一，
    #   再交给 derive_comparison_portfolios——避免旧实现按比例把它们的权重摊回其它桶(把权威构建悄悄抬向美股)，
    #   使"权威构建"与"仅核心/无卫星"(本就无这些卫星)不可比。剔除的权重显式披露(excluded_weight)。
    covered = {c for c in asset_of if FULL_PROXY.get(str(c)) in pxL.columns}
    constructed_full = snap["instrument_allocation"]

    def restrict(weights):
        kept = {c: float(w) for c, w in weights.items() if c in covered and w > 0}
        s = sum(kept.values())
        return {c: w / s for c, w in kept.items()} if s > 0 else {}

    excluded_weight = {
        "权威构建": round(sum(w for c, w in constructed_full.items() if c not in covered and w > 0), 4),
        "当前": round(sum(w for c, w in current.items() if c not in covered and w > 0), 4),
    }
    portfolios = _sm.derive_comparison_portfolios(restrict(constructed_full), restrict(current), asset_of, tier_of)
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

    # §9.2 收缩协方差（周频收益，用于风险贡献/有效风险来源数）
    cov = _sm.shrinkage_covariance({col: wk[col].tolist() for col in wk.columns})

    # 批4(§0B #5-③)：去退化重复基准——映射到同一组代理目标的基准(如 gold=0 时"无黄金"==权威构建)只测一次。
    rows, navs, pt_by_name, deduped, seen = [], {}, {}, [], {}
    for name, weights in portfolios.items():
        pt = to_proxy_targets(weights)
        if not pt:
            continue
        sig = tuple(sorted((k, round(v, 4)) for k, v in pt.items()))
        if sig in seen:
            deduped.append({"name": name, "same_as": seen[sig]})
            continue
        seen[sig] = name
        pt_by_name[name] = pt
        nav, m = _run_with_nav(pxL, pt, eq_proxies, bond_proxy, False, ma0, "M", yrsL)
        row = {"name": name, **clean_metric(m)}
        rc = _sm.risk_contributions(cov, pt) if cov else None
        if rc:
            row["vol_cov"] = rc["vol"]
            row["effective_bets"] = rc["effective_bets"]
            row["risk_contributions"] = rc["contributions"]
        rows.append(row)
        if name in ("当前", "权威构建", "更低权益"):
            navs[name] = nav

    # 批4(§0B #5-②)：债券票息 Calmar 敏感性——零息(bond_carry=0)重跑同一组组合，
    #   看"债重/更简单组合 Calmar 更高"是否被 +3% 零波动票息假设驱动（每行附 calmar_zero_coupon）。
    bond_sensitivity = None
    try:
        full0 = build_full_panel(strat, current, refresh=False, bond_carry=0.0)
        if full0:
            pxL0, _pt0, pa0, bond0, _dr0 = full0
            eq0 = [p for p, a in pa0.items() if a == "equity"]
            yrsL0 = (len(pxL0) - WARMUP) / 252
            srows = []
            for r in rows:
                pt = pt_by_name.get(r["name"])
                if not pt:
                    continue
                _n0, m0 = _run_with_nav(pxL0, pt, eq0, bond0, False, ma0, "M", yrsL0)
                r["calmar_zero_coupon"] = round(m0["calmar"], 2)
                srows.append({"name": r["name"], "calmar": r["calmar"],
                              "calmar_zero_coupon": r["calmar_zero_coupon"]})
            bond_sensitivity = {"bond_carry": round(DIV_YIELD.get(bond_proxy, 0.0), 4), "rows": srows}
    except Exception:  # noqa: BLE001
        bond_sensitivity = None

    # 稳健性①：滚动子期 Calmar（§12.4——构建的风险调整优势是否跨子期一致，而非靠单一窗口）
    rolling = []
    for name, nav in navs.items():
        flen = max(1, len(nav) // 3)
        fc = []
        for f in range(3):
            seg = nav.iloc[f * flen:(f + 1) * flen] if f < 2 else nav.iloc[f * flen:]
            if len(seg) >= 120:
                fc.append(round(metrics(seg / seg.iloc[0])["calmar"], 2))
        if fc:
            rolling.append({"name": name, "fold_calmar": fc})

    # 稳健性②：假设 ±20% 收益扰动重构（构建是否稳定守住 §18 上限、不剧烈摆动）
    perturbation = []
    for delta in (-0.2, 0.0, 0.2):
        rp = {a: v * (1 + delta) for a, v in asm["returns"].items()}
        rpc = {a: v * (1 + delta) for a, v in asm["returns_conservative"].items()}
        sp2 = _sm.construct_strategic_portfolio(
            sp, returns=rp, shocks=asm["shocks"], target_return=target,
            default_return=asm["default_return"] * (1 + delta), default_shock=asm["default_shock"],
            asset_of=asset_of, etf_share=etf_share, max_whole_stress=construct_budget,
            returns_conservative=rpc, scenarios=scen, exposure_of=exposure_of,
            covariance=construct_cov, incumbent_codes=current)
        mm = sp2.get("metrics") or {}
        perturbation.append({"return_delta": delta, "status": sp2["validation_status"],
                             "satellite": mm.get("satellite_total"), "growth": mm.get("growth_factor_total"),
                             "whole_stress": mm.get("whole_portfolio_stress")})

    names = {str(u["code"]): u.get("name") for u in strat.get("universe", [])}
    tested = [r["name"] for r in rows]
    weights = {n: {c: round(w, 4) for c, w in portfolios[n].items()} for n in tested if n in portfolios}
    return {"rows": rows, "years": round(yrsL, 1), "start": str(pxL.index[WARMUP].date()),
            "end": str(pxL.index[-1].date()), "dropped": dropped,
            "excluded_weight": excluded_weight, "deduped": deduped,
            "weights": weights, "names": names,           # 各被比组合的实际配仓（覆盖子集、已归一）
            "bond_sensitivity": bond_sensitivity,
            "rolling": rolling, "perturbation": perturbation,
            "construct_source": construct_source,
            "construct_meta": construct_meta or {"return_basis": "frozen", "stress_budget": construct_budget,
                                                    "target_return_basis": target_meta["basis"]},
            "risk_model": ({"obs": cov["obs"], "avg_corr": cov["avg_corr"], "shrink": cov["shrink"],
                            "estimator": cov.get("estimator")}
                           if cov else None)}


def trend_protection_benefit(strat, port, root, refresh=False):
    """§0C #4：长面板上「趋势过滤 vs 静态」的最大回撤差——量化"跌破 MA200 不动手会多扛多少回撤"。

    趋势过滤 = 价跌破 MA200 的权益移到债券（simulate use_trend）。返回
    {static_maxdd, trend_maxdd, delta_pp(过滤少扛的回撤), trend_cagr, static_cagr, years, start, end} | None。
    """
    current = {str(h["code"]): float(h.get("target_weight") or 0) for h in port.get("holdings", [])}
    full = build_full_panel(strat, current, refresh=refresh)
    if full is None:
        return None
    pxL, proxy_targets, proxy_asset, bond_proxy, _dropped = full
    if not bond_proxy:
        return None
    eq_proxies = [p for p, a in proxy_asset.items() if a == "equity"]
    ma0 = int(strat["factors"]["trend_filter"]["ma_days"])
    yrs = (len(pxL) - WARMUP) / 252.0
    _t, mT = _run_with_nav(pxL, proxy_targets, eq_proxies, bond_proxy, True, ma0, "M", yrs)
    _s, mS = _run_with_nav(pxL, proxy_targets, eq_proxies, bond_proxy, False, ma0, "M", yrs)
    return {"static_maxdd": round(float(mS["dd"]), 4), "trend_maxdd": round(float(mT["dd"]), 4),
            "delta_pp": round(float(mT["dd"] - mS["dd"]) * 100, 1),   # dd 为负；过滤回撤更浅(less negative) → 正=少扛的回撤
            "trend_cagr": round(float(mT["cagr"]), 4), "static_cagr": round(float(mS["cagr"]), 4),
            "years": round(float(yrs), 1), "start": str(pxL.index[WARMUP].date()), "end": str(pxL.index[-1].date())}


def walk_forward_strategic(strat, port, root, folds=3, refresh=False):
    """§0C #2 真 walk-forward：每折只用【过去】数据估协方差→构建权威组合→机械派生简化基准，
    再在【held-out 未来段】评估风险调整表现。检验"建议简化"结论是否【样本外】成立，
    而非把同一份全样本权重切三段（旧 `rolling` 的局限——权重选择本身用了全样本）。

    简化基准（更低权益/无卫星/仅核心）是 `derive_comparison_portfolios` 的机械变换、非事后挑选。
    返回 {folds:[{fold,train_end,test,test_years,rows,simpler_ge_construct}], summary:{...}} | None。
    """
    import signals as _sig                       # noqa: PLC0415
    import strategic as _sm                      # noqa: PLC0415
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
    exposure_of = {str(u["code"]): u.get("exposure_id") or u.get("index") or u.get("proxy_index") or str(u["code"])
                   for u in strat.get("universe", [])}
    stable = float(_sm.employment_resilience(prof)["risk_buffer_available"])
    planned = float(prof.get("planned_etf_capital", 0) or 0)
    etf_share = planned / (planned + stable) if planned > 0 and (planned + stable) > 0 else 1.0
    profile_target = float(prof.get("target_annual_return", 0.05) or 0.05)
    target = _sm.resolve_construct_target(
        sp, profile_target, planned, prof.get("stable_assets_outside", 0),
        stable_return=prof.get("stable_assets_yield"))["construct_target"]
    max_dd = float(prof.get("max_acceptable_drawdown", 0.15) or 0.15)
    construct_budget = _sm.resolve_construct_budget(sp, max_dd)
    current = {str(h["code"]): float(h.get("target_weight") or 0) for h in port.get("holdings", [])}
    full = build_full_panel(strat, current, refresh=refresh)
    if full is None:
        return None
    pxL, _pt, proxy_asset, bond_proxy, _dropped = full
    covered = {c for c in asset_of if FULL_PROXY.get(str(c)) in pxL.columns}
    eq_proxies = [p for p, a in proxy_asset.items() if a == "equity"]
    ma0 = int(strat["factors"]["trend_filter"]["ma_days"])

    def restrict(weights):
        kept = {c: float(w) for c, w in weights.items() if c in covered and w > 0}
        s = sum(kept.values())
        return {c: w / s for c, w in kept.items()} if s > 0 else {}

    def to_proxy_targets(weights):
        pt = {}
        for c, w in weights.items():
            p = FULL_PROXY.get(str(c))
            if p and p in pxL.columns:
                pt[p] = pt.get(p, 0.0) + float(w)
        s = sum(pt.values()) or 1.0
        return {k: v / s for k, v in pt.items()}

    ev = pxL.iloc[WARMUP:]
    n = len(ev)
    seg = n // (folds + 1)
    if seg < 250:                                # 每段至少 ~1 年评估窗，否则 Calmar 不稳
        return None
    track = ("权威构建", "更低权益", "无卫星", "仅核心", "当前")
    fold_rows, simpler_wins = [], 0
    for k in range(folds):
        train_end = seg * (k + 1)
        test_end = n if k == folds - 1 else seg * (k + 2)
        train_px = ev.iloc[:train_end]
        wk = train_px.resample("W").last().pct_change().dropna()
        code_returns = {c: wk[FULL_PROXY[c]].tolist() for c in asset_of if FULL_PROXY.get(c) in wk.columns}
        cov_train = _sm.shrinkage_covariance(code_returns)
        snap = _sm.construct_strategic_portfolio(
            sp, returns=asm["returns"], shocks=asm["shocks"], target_return=target,
            default_return=asm["default_return"], default_shock=asm["default_shock"], asset_of=asset_of,
            etf_share=etf_share, max_whole_stress=construct_budget,
            returns_conservative=asm["returns_conservative"], scenarios=scen,
            exposure_of=exposure_of, covariance=cov_train, incumbent_codes=current)
        if snap["validation_status"] == "no_feasible_portfolio":
            continue
        constructed = restrict(snap["instrument_allocation"])
        portfolios = _sm.derive_comparison_portfolios(constructed, restrict(current), asset_of, tier_of)
        # held-out 未来段：slice 含 WARMUP 前置（取自训练尾部、给 MA 暖机），_run_with_nav 丢弃后只评测试段
        seg_px = pxL.iloc[train_end:WARMUP + test_end]
        yrs_seg = (len(seg_px) - WARMUP) / 252.0
        if yrs_seg <= 0.5:
            continue
        rows, cal = [], {}
        for name in track:
            w = portfolios.get(name)
            if not w:
                continue
            pt = to_proxy_targets(w)
            if not pt:
                continue
            _nav, m = _run_with_nav(seg_px, pt, eq_proxies, bond_proxy, False, ma0, "M", yrs_seg)
            rows.append({"name": name, **clean_metric(m)})
            c = m["calmar"]
            cal[name] = c if c == c else 999.0    # 无回撤(NaN)→视作极好，避免误判
        simpler = max((cal.get(x, float("-inf")) for x in ("更低权益", "无卫星", "仅核心")), default=float("-inf"))
        construct_cal = cal.get("权威构建", float("-inf"))
        win = simpler >= construct_cal
        simpler_wins += int(win)
        fold_rows.append({
            "fold": k + 1, "train_end": str(train_px.index[-1].date()),
            "test": [str(seg_px.index[WARMUP].date()), str(seg_px.index[-1].date())],
            "test_years": round(yrs_seg, 1), "rows": rows, "simpler_ge_construct": bool(win),
        })
    if not fold_rows:
        return None
    nf = len(fold_rows)
    verdict = "样本外仍倾向简化" if simpler_wins >= (nf + 1) // 2 else "样本外不支持简化（构建更优）"
    return {"folds": fold_rows,
            "summary": {"n_folds": nf, "simpler_wins": simpler_wins, "verdict": verdict,
                        "evidence_scope": "policy_structure_with_frozen_return_assumptions",
                        "note": "每折只用过去数据估风险并在未来段评估；收益使用冻结假设，不冒充实时锚定配置的样本外证明"}}


# ─── §0C #2 证据台账：把每条隐含"更优"主张与其证据档显式登记 ───────────────────────────
#   证据档强弱：logic（仅逻辑）< in_sample（样本内回测）< walk_forward（样本外）< live（实盘）。
#   维度2 诚实护栏：UI 渲染任何"更优"措辞不得强过此处登记的 tier；live 档须 §0C #6 实盘记账积累。
EVIDENCE_TIER_ORDER = ["logic", "in_sample", "walk_forward", "live"]
EVIDENCE_CLAIMS = [
    {"id": "trend_ma200", "claim": "MA200 趋势过滤限制回撤", "tier": "in_sample",
     "basis": "simulate(use_trend) / walk_forward_tactical 段内回撤更小",
     "caveat": "线上仅 trend_alerts 提醒、不自动执行（见 #4）；规则系看着历史写"},
    {"id": "valuation_meanrev", "claim": "估值分位均值回归（便宜加/贵减）改善风险调整", "tier": "in_sample",
     "basis": "simulate_tactical + no_valuation 消融对照（point-in-time、无前视）",
     "caveat": "样本内；点位估值仅 A 股权益适用"},
    {"id": "diversification", "claim": "跨金/全球/债分散降低全组合尾部回撤", "tier": "in_sample",
     "basis": "§0C #1 据真实峰谷标定多情景：同情景内债/金抵损（2008 债 +7%、A 股 −71%）",
     "caveat": "协方差压力/覆盖率/有效风险源已进接受判定；无长代理资产仍依赖线性压力情景"},
    {"id": "dca", "claim": "分批/DCA 降低一次性择时风险", "tier": "in_sample",
     "basis": "run_dca 重叠窗口 beats_lumpsum%",
     "caveat": "窗口重叠不独立、代码已自标'仅示意量级、非稳健分布'"},
    {"id": "simplify", "claim": "简化组合（更低权益/无卫星）风险调整 ≥ 复杂构建", "tier": "walk_forward",
     "basis": "（运行时填入真 walk-forward 结论）",
     "caveat": "不覆盖无长代理的成长卫星 159915/588000；样本外≠已证明赚钱"},
]


def build_evidence_ledger(strat, port, root, refresh=False, with_walk_forward=True):
    """§0C #2：组装证据台账；把"简化"主张用 live 真 walk-forward 结论实化、定档。

    返回 {claims:[{id,claim,tier,basis,caveat,evidence?}], tier_order, note}。
    """
    ledger = [dict(c) for c in EVIDENCE_CLAIMS]
    wf = None
    if with_walk_forward:
        wf = walk_forward_strategic(strat, port, root, refresh=refresh)
        for c in ledger:
            if c["id"] != "simplify":
                continue
            if wf:
                s = wf["summary"]
                c["basis"] = (f"真 walk-forward {s['n_folds']} 折中 {s['simpler_wins']} 折简化≥构建："
                              f"{s['verdict']}")
                c["evidence"] = s
                c["tier"] = "walk_forward" if s["simpler_wins"] >= (s["n_folds"] + 1) // 2 else "in_sample"
            else:
                c["tier"] = "in_sample"
                c["basis"] = "walk-forward 不可得（缺面板），回退样本内子期一致性"
    # §0C #6：把真实 NAV 记账接进台账——live 档随快照积累点亮（≥8 周快照才算"够档"，否则诚实标"积累中"）。
    LIVE_SNAPSHOT_MIN = 8
    try:
        import reports as _rp                  # noqa: PLC0415  懒加载，避免循环依赖
        perf = _rp.performance_summary()
        n = int(perf.get("snapshots", 0) or 0)
        twr = perf.get("twr") or {}
        start = (perf.get("nav_curve") or [{}])[0].get("date") if perf.get("nav_curve") else None
        if n >= LIVE_SNAPSHOT_MIN and twr.get("available"):
            tier = "live"
            basis = f"{n} 个 NAV 快照、TWR {twr['twr'] * 100:.1f}%（已剔除注入本金）"
        else:
            tier = "logic"      # 数据不够"档"——只如实标记时钟在走，不冒充 live
            basis = f"实盘记账已起步：{n} 个 NAV 快照{f'、自 {start}' if start else ''}（需 ≥{LIVE_SNAPSHOT_MIN} 周才点亮 live 档）"
        ledger.append({"id": "live_track_record", "claim": "工具的真实风险调整收益（实盘）", "tier": tier,
                       "basis": basis, "caveat": "少额真金期样本小；TWR/MWR 非承诺、仅历史回看、剔除本金注入"})
    except Exception:  # noqa: BLE001  实盘档是增益项，缺了不影响其余台账
        pass
    return {"claims": ledger, "tier_order": EVIDENCE_TIER_ORDER, "walk_forward": wf,
            "note": "维度2 护栏：UI 任何'更优'措辞不得强过此处 tier；live 档随 §0C #6 实盘 NAV 快照积累点亮。"}


def _run_strategic_cli(strat, port, root, refresh=False):
    """`backtest.py --strategic`：§12.3 战略组合对比（全收益长面板·持仓漂移·含成本）。"""
    override = json.loads(os.environ["STRATEGIC_ALLOCATION_JSON"]) if os.environ.get("STRATEGIC_ALLOCATION_JSON") else None
    meta = json.loads(os.environ["STRATEGIC_CONSTRUCT_META_JSON"]) if os.environ.get("STRATEGIC_CONSTRUCT_META_JSON") else None
    res = simulate_strategic_comparison(strat, port, root, refresh=refresh,
                                        constructed_override=override, construct_meta=meta)
    if not res:
        print("无法构建战略对比（缺 strategic_policy / 无可行组合 / 全收益面板不可得，可联网 --refresh）。")
        return
    print(f"\n══════ 战略组合对比回测（全收益长面板·持仓漂移·含成本）≈ {res['years']} 年 ══════")
    print(f"区间 {res['start']} → {res['end']}；剔除无长代理：{('、'.join(res['dropped']) or '无')}（创业板/科创50/QDII 等无长序列）")
    ew = res.get("excluded_weight") or {}
    if ew:
        print("批4·诚实口径：以下结果为【可代理子集】对比——已把无 20 年长代理的成长卫星统一从各组合剔除并各自归一。"
              f"被剔权重：权威构建 {ew.get('权威构建', 0):.0%}、当前 {ew.get('当前', 0):.0%}（其增量价值不在本回测覆盖内）。")
    if res.get("deduped"):
        print("去退化重复基准：" + "、".join(f"{d['name']}≡{d['same_as']}" for d in res["deduped"]) + "（与既有基准字节相同，仅测一次）。")
    rm = res.get("risk_model")
    print("%-12s %8s %7s %8s %7s %9s %7s %8s" % ("组合", "年化", "波动", "最大回撤", "Calmar", "Calmar零息", "有效风险源", "年换手"))
    print("-" * 80)
    for r in res["rows"]:
        eff = r.get("effective_bets")
        cz = r.get("calmar_zero_coupon")
        print("%-11s %+7.1f%% %6.1f%% %7.1f%% %7.2f %9s %7s %7.0f%%" %
              (r["name"], r["cagr"] * 100, r["vol"] * 100, r["max_drawdown"] * 100, r["calmar"],
               (f"{cz:.2f}" if cz is not None else "-"),
               (f"{eff:.1f}" if eff is not None else "-"), r["turnover_annual"] * 100))
    bs = res.get("bond_sensitivity")
    if bs:
        print(f"债券票息敏感性：主表用 +{bs['bond_carry']:.0%}/年票息；「Calmar零息」列为 0% 票息重跑——"
              "若结论(谁的 Calmar 更高)在两列间翻转，说明它被债券票息假设驱动，不可作上线依据。")
    if rm:
        print(f"风险模型：收缩协方差（周频 {rm['obs']} 期、平均相关 {rm['avg_corr']}、收缩 {rm['shrink']}）；"
              "「有效风险源」=风险贡献 HHI 倒数，越高越分散（§12.1）。")
    if res.get("rolling"):
        print("\n【稳健性①·滚动子期 Calmar】（跨期一致性，非单窗口）")
        for r in res["rolling"]:
            print(f"  {r['name']:8s} 三段 Calmar：" + " / ".join(f"{x:.2f}" for x in r["fold_calmar"]))
    if res.get("perturbation"):
        print("\n【稳健性②·假设 ±20% 收益扰动重构】（构建是否稳定守住 §18 上限）")
        for p in res["perturbation"]:
            print(f"  收益×{1 + p['return_delta']:.1f}：{p['status']}｜卫星 {p['satellite']:.0%}"
                  f"｜成长 {p['growth']:.0%}｜压力 {p['whole_stress']:.0%}")
    print("§16.3：若『仅核心/无卫星』在风险与成本上不劣于『权威构建』，则复杂度未通过——构建组合应被否。")
    print("⚠️ 代理段为全收益指数近似（剔除无长序列品种）；过去≠未来，仅用于结构性对比、非精确收益预测。")


def main():
    ap = argparse.ArgumentParser(description="策略回测")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新拉取（优先前复权）")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON，供 Web 前端渲染")
    ap.add_argument("--tactical", action="store_true", help="跑双向战术配置六策略影子对比（§13）")
    ap.add_argument("--strategic", action="store_true", help="跑战略组合对比回测（§12.3：构建 vs 当前 vs 简化基准）")
    ap.add_argument("--return-intervals", action="store_true", dest="return_intervals",
                    help="按历史波动率算数据驱动收益区间（保守/乐观），供 strategy.yaml 登记")
    ap.add_argument("--stress-scenarios", action="store_true", dest="stress_scenarios",
                    help="从长面板按真实峰谷标定历史危机情景（2008/2015/2018/2020/2022），供 signals 登记")
    ap.add_argument("--walk-forward", action="store_true", dest="walk_forward",
                    help="§0C #2 真 walk-forward：每折只用过去数据构建、在未来段评估，检验'建议简化'是否样本外成立")
    ap.add_argument("--evidence", action="store_true", dest="evidence",
                    help="§0C #2 证据台账：每条'更优'主张 → 证据档(logic/in_sample/walk_forward/live) + 依据 + 局限")
    ap.add_argument("--trend-benefit", action="store_true", dest="trend_benefit",
                    help="§0C #4 趋势过滤回撤保护：长面板上'趋势过滤 vs 静态'的最大回撤差，供 signals 登记")
    args = ap.parse_args()

    root = find_repo_root(HERE)
    strat = load_yaml(os.path.join(root, "strategy.yaml"))
    port = load_yaml(os.path.join(root, "portfolio.yaml"))
    if args.return_intervals:
        ri = compute_return_intervals(strat, refresh=args.refresh)
        if not ri:
            print("无法计算（缺核心权益代理数据，可 --refresh 联网）。")
            return
        print("数据驱动收益区间（haircut 按历史年化波动缩放；成长桶保守值封顶在核心权益保守值）：")
        print("%-16s %7s %8s %8s %9s %9s" % ("sleeve", "年化波动", "折扣", "中枢", "保守", "乐观"))
        print("-" * 64)
        for a, d in ri.items():
            print("%-16s %6.1f%% %8.3f %7.1f%% %8.1f%% %8.1f%%" % (
                a, d["vol"] * 100, d["haircut"], d["central"] * 100,
                d["conservative"] * 100, d["optimistic"] * 100))
        print("\n建议登记到 strategy.yaml 的成长/QDII sleeve（其余维持对称默认折扣）：")
        for a in ("global_equity", "global_growth", "china_growth"):
            if a in ri:
                print(f"    {a}: ... return_conservative: {ri[a]['conservative']}, return_optimistic: {ri[a]['optimistic']}")
        return
    if args.stress_scenarios:                # 自建价格代理面板，无需 ETF 段行情
        scs = compute_crisis_scenarios(refresh=args.refresh)
        if not scs:
            print("无法标定（缺核心价格代理种子，可 --refresh 联网）。")
            return
        if args.json:
            print(json.dumps(scs, ensure_ascii=False, indent=2))
            return
        print("历史危机情景标定（据 idx_*.csv 种子真实峰谷；锚=窗口内跌最深的权益代理）：\n")
        order = ["equity", "equity_defensive", "china_growth", "global_equity", "global_growth", "bond", "gold"]
        print("%-16s %-23s " % ("情景", "峰→谷") + " ".join("%8s" % a[:8] for a in order))
        for sc in scs:
            sh = sc["shocks"]
            row = " ".join("%7.1f%%" % (sh[a] * 100) if a in sh else "%8s" % "—" for a in order)
            print("%-16s %-23s %s" % (sc["name"], "%s→%s" % tuple(sc["window"]), row))
        print("\n建议登记到 signals.py 的 HISTORICAL_CRISIS_SCENARIOS（如下，已含 short_bond/cash）：\n")
        compact = [{"name": s["name"], "window": s["window"], "anchor": s["anchor"],
                    "shocks": s["shocks"]} for s in scs]
        print(json.dumps(compact, ensure_ascii=False, indent=2))
        return
    if args.trend_benefit:                   # §0C #4：趋势过滤回撤保护
        b = trend_protection_benefit(strat, port, root, refresh=args.refresh)
        if not b:
            print("无法计算（缺面板/债券代理，可 --refresh）。")
            return
        if args.json:
            print(json.dumps(b, ensure_ascii=False, indent=2))
            return
        print(f"趋势过滤回撤保护（{b['years']} 年长面板 {b['start']}→{b['end']}）：")
        print(f"  静态最大回撤 {b['static_maxdd']*100:.0f}% → 趋势过滤 {b['trend_maxdd']*100:.0f}%"
              f"（少扛约 {b['delta_pp']:.0f}pp）")
        print(f"  年化：趋势 {b['trend_cagr']*100:.1f}% vs 静态 {b['static_cagr']*100:.1f}%")
        print(f"\n建议登记到 signals.py TREND_PROTECTION_BENEFIT：{json.dumps(b, ensure_ascii=False)}")
        print("[诚实] 样本内；线上不自动执行，需人确认。")
        return
    if args.evidence:                        # §0C #2：证据台账
        led = build_evidence_ledger(strat, port, root, refresh=args.refresh)
        if args.json:
            print(json.dumps(led, ensure_ascii=False, indent=2))
            return
        print("证据台账（每条'更优'主张 → 证据档 + 依据 + 局限）：")
        print("证据档强弱：logic < in_sample < walk_forward < live\n")
        for c in led["claims"]:
            print(f"[{c['tier']:<12}] {c['claim']}")
            print(f"  依据：{c['basis']}")
            print(f"  局限：{c['caveat']}\n")
        print(led["note"])
        return
    if args.walk_forward:                    # §0C #2：真 walk-forward（样本外构建+评估）
        wf = walk_forward_strategic(strat, port, root, refresh=args.refresh)
        if not wf:
            print("无法跑 walk-forward（缺 policy / 面板不可得 / 段太短，可 --refresh）。")
            return
        if args.json:
            print(json.dumps(wf, ensure_ascii=False, indent=2))
            return
        s = wf["summary"]
        print(f"真 walk-forward（每折只用过去数据构建、未来段评估）：{s['n_folds']} 折中 "
              f"{s['simpler_wins']} 折简化≥构建 → {s['verdict']}\n")
        for f in wf["folds"]:
            print(f"段{f['fold']} 训练截至 {f['train_end']}｜测试 {f['test'][0]}→{f['test'][1]}"
                  f"（{f['test_years']}年）｜简化{'≥' if f['simpler_ge_construct'] else '<'}构建")
            print("  %-10s %8s %9s %8s" % ("组合", "年化", "最大回撤", "Calmar"))
            for r in f["rows"]:
                print("  %-10s %+7.1f%% %8.1f%% %7.2f" %
                      (r["name"], r["cagr"] * 100, r["max_drawdown"] * 100, r["calmar"]))
            print()
        print("[诚实] 样本外≠已证明赚钱；规则仍是看着历史写的，且不覆盖无长代理的成长卫星(159915/588000)。")
        return
    if args.strategic:                       # 自建全收益面板，无需 ETF 段行情
        override = json.loads(os.environ["STRATEGIC_ALLOCATION_JSON"]) if os.environ.get("STRATEGIC_ALLOCATION_JSON") else None
        meta = json.loads(os.environ["STRATEGIC_CONSTRUCT_META_JSON"]) if os.environ.get("STRATEGIC_CONSTRUCT_META_JSON") else None
        if args.json:
            res = simulate_strategic_comparison(strat, port, root, refresh=args.refresh,
                                                constructed_override=override, construct_meta=meta)
            print(json.dumps(res or {"error": "无法构建战略对比（缺 policy / 无可行组合 / 面板不可得）"},
                             ensure_ascii=False, indent=2))
        else:
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
            sh = sharpe_ratio(m["cagr"], m["vol"])
            print("%-14s %+7.1f%% %6.1f%% %7.1f%% %6.2f %7.2f %6.0f%% %6.0f日" %
                  (name, m["cagr"] * 100, m["vol"] * 100, m["dd"] * 100, sh, m["calmar"],
                   m["turn_ann"] * 100, m["uw_days"]))
        print(f"  注：夏普 =（年化 − 无风险 {RISK_FREE_RATE * 100:.0f}%）/ 波动（真夏普口径，已减 rf）。")

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
        print("说明：未复权低估分红，真实收益应略高；成本单边万0.5；最长水下=连续未创新高的交易日数。")
        print(f"数据元信息见 {os.path.relpath(META_PATH, root)}（--refresh 可在联网机重取并登记来源/复权）。")


if __name__ == "__main__":
    main()
