"""再平衡策略对比分析（可复现）：用真实组合 + 21 年全收益代理面板，比较不同再平衡规则。

跑法：  python3 engine/analysis/rebalance_compare.py
依赖：  engine/data/ 下的缓存（离线即可跑；联网刷新见 backtest.py --refresh）。
口径：  全收益（含分红/票息代理）、单边成本万3、剔除无长史的 创业板/科创50 后按比例归一。
        过去≠未来；本表用于"理解规则差异有多大"，不是预测。结论见仓库根 REBALANCING.md。
"""
import os
import sys

_ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ENGINE)
import yaml  # noqa: E402
import pandas as pd  # noqa: E402
import backtest as bt  # noqa: E402

_ROOT = os.path.dirname(_ENGINE)
strat = yaml.safe_load(open(os.path.join(_ROOT, "strategy.yaml")))
port = yaml.safe_load(open(os.path.join(_ROOT, "portfolio.yaml")))
targets = {str(h["code"]): float(h.get("target_weight") or 0) for h in port["holdings"]}

pxL, ptargets, passet, bond, dropped = bt.build_full_panel(strat, targets)
COST = bt.COST
cols = list(ptargets)
ZH = {"sh000012": "国债", "sh000300": "沪深300/红利", "sh000905": "中证500",
      "gold": "黄金", "spx": "标普500", "ixic": "纳指"}


def breach_525(w, t):
    return abs(w - t) >= 0.05 or (t > 0 and abs(w - t) / t >= 0.25)


def breach_5pp(w, t):
    return abs(w - t) >= 0.05


def _sim(px, check_freq, trigger):
    """check_freq: None=每日；'W'/'M'/'Q'/'Y'=按周/月/季/年首日检查。
    trigger: 'never' | 'always'(纯日历) | '525' | '5pp'。返回 metrics dict + 调仓数/年换手。"""
    rets = px.pct_change().fillna(0.0)
    idx = px.index
    chk = None
    if check_freq:
        per = pd.Series(idx.to_period(check_freq), index=idx)
        chk = ~per.duplicated().values
    nav, h, turn, nreb = [], None, 0.0, 0
    for i in range(len(px)):
        if h is not None:
            for c in cols:
                h[c] *= (1 + rets[c].iloc[i])
        total = sum(h.values()) if h is not None else 1.0
        if h is None:
            do = True
        elif trigger == "never":
            do = False
        else:
            look = True if chk is None else bool(chk[i])
            if not look:
                do = False
            elif trigger == "always":
                do = True
            else:
                br = breach_525 if trigger == "525" else breach_5pp
                do = any(br(h[c] / total, ptargets[c]) for c in cols)
        if do:
            tv = {c: ptargets[c] * total for c in cols}
            if h is not None and total > 0:
                t = sum(abs(tv[c] - h[c]) for c in cols)
                turn += t / total
                total -= t * COST
                nreb += 1
            h = {c: ptargets[c] * total for c in cols}
        nav.append(sum(h.values()) if h is not None else 1.0)
    m = bt.metrics(pd.Series(nav, index=idx))
    m["nreb"] = nreb
    m["turn_ann"] = turn / (len(px) / 252)
    return m


def main():
    yrs = len(pxL) / 252
    print(f"样本 {pxL.index[0].date()} → {pxL.index[-1].date()}（约 {yrs:.1f} 年，日频，全收益）")
    print(f"剔除无长史的 {dropped}（占真实组合 {sum(targets[c] for c in dropped) * 100:.0f}%），其余归一")
    print("组合(代理)：" + " / ".join(f"{ZH[c]} {ptargets[c] * 100:.0f}%" for c in cols))

    plans = [("买入持有(从不调)", None, "never"), ("5/25 带(每日检查)", None, "525"),
             ("月度日历", "M", "always"), ("季度日历", "Q", "always"),
             ("年度日历", "Y", "always"), ("年度 5/25(=年度+5pp)", "Y", "525")]
    print("\n%-20s %8s %8s %8s %7s %7s %7s" % ("规则", "年化", "波动", "最大回撤", "Calmar", "调仓数", "年换手"))
    print("-" * 78)
    for name, f, trig in plans:
        m = _sim(pxL, f, trig)
        print("%-18s %+7.2f%% %6.1f%% %7.1f%% %7.2f %6d %6.0f%%" % (
            name, m["cagr"] * 100, m["vol"] * 100, m["dd"] * 100, m["calmar"], m["nreb"], m["turn_ann"] * 100))

    print("\n同一条 5/25，只改检查频率（每日→每年），看'频率'这一个杠杆：")
    print("%-10s %8s %8s %7s %7s" % ("检查频率", "年化", "最大回撤", "Calmar", "年换手"))
    for lbl, f in [("每日", None), ("每周", "W"), ("每月", "M"), ("每季", "Q"), ("每年", "Y")]:
        m = _sim(pxL, f, "525")
        print("%-9s %+7.2f%% %7.1f%% %7.2f %6.0f%%" % (lbl, m["cagr"] * 100, m["dd"] * 100, m["calmar"], m["turn_ann"] * 100))

    print("\n按行情脾气切窗口（买入持有 / 5/25每周 / 年度5/25；年化 ｜ 最大回撤）：")
    windows = [("2007–09 金融危机", "2007-01-01", "2009-12-31"),
               ("2015–16 中国股灾", "2015-01-01", "2016-12-31"),
               ("2015–19 股灾后震荡", "2015-01-01", "2019-12-31"),
               ("2020–22 疫情+加息", "2020-01-01", "2022-12-31"),
               ("2016–20 平稳趋势", "2016-01-01", "2020-01-01")]
    for name, a, b in windows:
        sub = pxL.loc[a:b]
        if len(sub) < 200:
            print(f"  {name}: 数据不足")
            continue
        bh, wk, yr = _sim(sub, None, "never"), _sim(sub, "W", "525"), _sim(sub, "Y", "525")
        win = "5/25每周✅" if wk["cagr"] > yr["cagr"] else "年度✅"
        print("  %-16s 持有 %+5.1f%%/%5.0f%% ｜ 5/25周 %+5.1f%%/%5.0f%% ｜ 年度 %+5.1f%%/%5.0f%%  %s" % (
            name, bh["cagr"] * 100, bh["dd"] * 100, wk["cagr"] * 100, wk["dd"] * 100,
            yr["cagr"] * 100, yr["dd"] * 100, win))


if __name__ == "__main__":
    main()
