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


def main():
    ap = argparse.ArgumentParser(description="策略回测")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新拉取（优先前复权）")
    args = ap.parse_args()

    root = find_repo_root(HERE)
    strat = load_yaml(os.path.join(root, "strategy.yaml"))
    port = load_yaml(os.path.join(root, "portfolio.yaml"))
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
    ev = px.iloc[WARMUP:]
    yrs = len(ev) / 252

    print(f"\n══════ ① ETF 可交易回测 ══════")
    print(f"区间 {ev.index[0].date()} → {ev.index[-1].date()}（约 {yrs:.1f} 年）｜数据源 {'、'.join(sorted(srcs))}")
    strat_m = _run(px, targets, trend_codes, bond_code, True, ma0, "M", yrs)
    static_m = _run(px, targets, trend_codes, bond_code, False, ma0, "M", yrs)
    bh = (px[bench] / px[bench].iloc[0]).iloc[WARMUP:]
    bh_m = metrics(bh); bh_m["turn_ann"] = 0.0
    print("%-16s %8s %7s %8s %6s %7s %7s %8s" %
          ("组合", "年化", "波动", "最大回撤", "夏普", "Calmar", "年换手", "最长水下"))
    print("-" * 78)
    for name, m in [("本策略(趋势过滤)", strat_m), ("静态(无过滤)", static_m), (f"{bench}买入持有", bh_m)]:
        sh = (m["cagr"]) / m["vol"] if m["vol"] > 0 else float("nan")
        print("%-14s %+7.1f%% %6.1f%% %7.1f%% %6.2f %7.2f %6.0f%% %6.0f日" %
              (name, m["cagr"] * 100, m["vol"] * 100, m["dd"] * 100, sh, m["calmar"],
               m["turn_ann"] * 100, m["uw_days"]))

    print("\n【敏感性】趋势过滤均线周期：", end="")
    for ma in (120, 200, 250):
        m = _run(px, targets, trend_codes, bond_code, True, ma, "M", yrs)
        print(f"ma{ma}→{m['cagr']*100:+.1f}%/{m['dd']*100:.0f}%回撤  ", end="")
    print(f"｜静态基准 {static_m['cagr']*100:+.1f}%/{static_m['dd']*100:.0f}%回撤")
    print("【敏感性】再平衡频率：", end="")
    for freq, lab in (("M", "月度"), ("Q", "季度")):
        m = _run(px, targets, trend_codes, bond_code, False, ma0, freq, yrs)
        print(f"{lab}→{m['cagr']*100:+.1f}%/换手{m['turn_ann']*100:.0f}%  ", end="")
    print()

    # ═══ ② 指数代理长期回测 ═══
    proxy_targets, dropped = {}, []
    for c, w in targets.items():
        pidx = prox.get(c)
        if pidx:
            proxy_targets[pidx] = proxy_targets.get(pidx, 0.0) + w
        else:
            dropped.append(c)
    ssum = sum(proxy_targets.values())
    proxy_targets = {k: v / ssum for k, v in proxy_targets.items()}
    eq_proxies = list({prox[c] for c in codes if asset.get(c) in ("equity", "equity_defensive") and prox.get(c)})
    bond_proxy = prox.get(bond_code)

    print(f"\n══════ ② 指数代理长期回测（价格指数·未含分红·近似）══════")
    pseries, psrc = {}, set()
    ok = True
    for sym in proxy_targets:
        s, src = fetch_index(sym, refresh=args.refresh)
        if s is None:
            print(f"[警告] 指数 {sym} 拉取失败，跳过长期回测。", file=sys.stderr)
            ok = False
            break
        pseries[sym] = s
        psrc.add(src)
    if ok:
        pxL = pd.DataFrame(pseries).dropna()
        evL = pxL.iloc[WARMUP:]
        yrsL = len(evL) / 252
        dnames = "、".join(dropped) if dropped else "无"
        print(f"区间 {evL.index[0].date()} → {evL.index[-1].date()}（约 {yrsL:.1f} 年，比 ETF 段长 ~{yrsL-yrs:.0f} 年）")
        print(f"代理映射：红利低波→沪深300(近似)；剔除并分摊：{dnames}（黄金无长序列）；债券=上证国债指数")
        sL = _run(pxL, proxy_targets, eq_proxies, bond_proxy, False, ma0, "M", yrsL)
        tL = _run(pxL, proxy_targets, eq_proxies, bond_proxy, True, ma0, "M", yrsL)
        bL = metrics((pxL["sh000300"] / pxL["sh000300"].iloc[0]).iloc[WARMUP:]) if "sh000300" in pxL else None
        print("%-16s %8s %7s %8s %7s %8s" % ("组合", "年化", "波动", "最大回撤", "Calmar", "最长水下"))
        print("-" * 64)
        rows = [("静态(无过滤)", sL), ("本策略(趋势过滤)", tL)]
        if bL:
            rows.append(("沪深300指数买入持有", bL))
        for name, m in rows:
            print("%-14s %+7.1f%% %6.1f%% %7.1f%% %7.2f %6.0f日" %
                  (name, m["cagr"] * 100, m["vol"] * 100, m["dd"] * 100, m["calmar"], m["uw_days"]))
        print("⚠️ 价格指数未含分红→低估真实收益(尤其债券/红利)；本段主要看更长周期的『回撤轮廓』，非精确收益预测。")

    # ═══ 按风险偏好给推荐 ═══
    rec = {"保守": "本策略(趋势过滤)——降回撤优先",
           "平衡": "静态组合(趋势仅作展示)——收益/回撤更均衡",
           "进取": "静态组合——收益优先、容忍更大回撤"}.get(profile, "静态组合")
    print(f"\n当前 risk_profile=【{profile}】→ 推荐口径：{rec}")
    print("说明：未复权低估分红，真实收益应略高；成本单边万3；最长水下=连续未创新高的交易日数。")
    print(f"数据元信息见 {os.path.relpath(META_PATH, root)}（--refresh 可在联网机重取并登记来源/复权）。")


if __name__ == "__main__":
    main()
