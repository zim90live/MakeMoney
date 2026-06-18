#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────
# 长期战略层纯函数（Track C / STRATEGIC_ALLOCATION_DESIGN.md）。
#   v1 单模块，但按 §4 责任边界组织：本文件目前承载 ETF 产品准入(§8)的纯逻辑——
#     parse_etf_fee   : 解析 akshare fund_fee_em 输出 → 管理费/托管费/综合费率（无网络）
#     hard_admission  : §8.2 硬准入门槛（已取数的候选 → 准入裁决，fail-closed）
#   网络取数(_etf_fee 等 IO)留在 app.py，本模块只吃已取好的数 → 可秒级无网络单测。
#   Phase C 起再加 construct/validate/covariance 等纯函数到本模块。
# ─────────────────────────────────────────────────────────────────────────
import math
import re
from itertools import combinations


def parse_etf_fee(rows):
    """解析 ak.fund_fee_em(symbol=code, indicator='运作费用') 的行数据（纯函数、无网络）。

    该接口返回**无表头** DataFrame（列名是整数 0/1/2/3），行形如
        ['管理费率', '0.15%（每年）', '托管费率', '0.05%（每年）']。
    本函数不硬编码列下标，而是按"管理费率"/"托管费率"标签就近定位其百分数值，更鲁棒。

    入参 rows：可迭代的行，每行可迭代单元格（如 df.values.tolist()）。
    返回 {management_fee, custody_fee, expense_ratio}（小数年率，如 0.0015）；
    缺失项为 None；两项都缺则 expense_ratio=None。绝不编造缺失费率。
    """
    cells = []
    for row in rows or []:
        try:
            for c in row:
                cells.append("" if c is None else str(c))
        except TypeError:                       # 行不可迭代（异常结构）→ 当作单元格
            cells.append("" if row is None else str(row))

    def _rate_near(label):
        # 在含该标签的单元格本身或其右邻单元格里找百分数（覆盖"标签:值"同格与"标签 | 值"分格两种布局）
        for i, c in enumerate(cells):
            if label in c:
                for j in (i, i + 1):
                    if j < len(cells):
                        m = re.search(r"(\d+(?:\.\d+)?)\s*%", cells[j])
                        if m:
                            return round(float(m.group(1)) / 100.0, 6)
        return None

    mgmt = _rate_near("管理费率")
    cust = _rate_near("托管费率")
    expense = None
    if mgmt is not None or cust is not None:
        expense = round((mgmt or 0.0) + (cust or 0.0), 6)
    return {"management_fee": mgmt, "custody_fee": cust, "expense_ratio": expense}


# §8.2 硬准入门槛默认值（可被 cfg 覆盖；阈值须按计划资金规模动态核算，见 §8.2）。
ADMISSION_DEFAULTS = {
    "liquidity_fraction": 0.05,     # 单笔计划交易 ≤ 5% × 近20日均成交额
    "capacity_fraction": 0.01,      # 计划持仓 ≤ 1% × 基金规模
    "min_market_cap": 2.0e8,        # 规模最低门槛（2 亿元）
    "min_listed_years": 1.0,        # 上市最短年限
    "max_abs_premium": 0.03,        # 折溢价绝对值上限（±3%）
    "purchase_block_keys": ("不可申购", "暂停申购", "暂停", "限大额", "限制申购"),
}

# 关键检查：缺数据即"降资格/待复核"（不准入），绝不默认通过（§8.3 缺失≠中性）。
# 注意：折溢价**不在**关键检查里——它是**执行时点**问题（下单那刻的实时偏离），由「执行质量闸」在
# 调仓/本周决策时把关；不该让一个瞬时报价决定 30 年的长期战略准入（也避免非交易时段陈旧折价误判）。
_CRITICAL_CHECKS = {"scale", "capacity", "liquidity", "purchase"}


def hard_admission(cand, *, planned_single_trade=None, planned_position=None, cfg=None):
    """§8.2 ETF 产品硬准入门槛（纯函数）。吃**已取好数**的候选指标，给准入裁决。

    cand 字段（任一可为 None=缺失）：
        market_cap(元) / avg_turnover_20d(元) / premium(小数,正=溢价)
        purchase_status(str) / listed_years(float) / fee({expense_ratio,...})
    planned_single_trade / planned_position：按计划资金规模动态核算的元值（None=不核该项）。

    返回 {admitted, checks:[{name,status(pass|fail|gap|info),detail}], blockers, data_gaps}。
    准入 = 无 fail 且关键检查无 gap。fee/listed_years 缺失为软 gap（不阻断；§8.2 允许有据缺失）。
    关键字段（规模/容量/流动性/申购）缺失 → 关键 gap → 不准入（降资格待复核），不 fail-open。
    **折溢价不参与准入**（status=info）：它是执行时点问题，由「执行质量闸」在下单/调仓时按实时折溢价把关，
    不让一个瞬时报价决定长期战略准入（也避免非交易时段陈旧折价误判）。
    """
    c = dict(ADMISSION_DEFAULTS)
    if cfg:
        c.update(cfg)
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    mc = cand.get("market_cap")
    tv = cand.get("avg_turnover_20d")
    pr = cand.get("premium")
    ps = cand.get("purchase_status")
    ly = cand.get("listed_years")
    fee = cand.get("fee") or {}

    # 规模（关键）
    if mc is None:
        add("scale", "gap", "规模数据缺失（不可得→降资格待复核）")
    elif mc < c["min_market_cap"]:
        add("scale", "fail", f"规模约 {mc / 1e8:.2f} 亿元，低于门槛 {c['min_market_cap'] / 1e8:.1f} 亿元，清盘风险")
    else:
        add("scale", "pass", f"规模约 {mc / 1e8:.1f} 亿元")

    # 容量（关键，需 market_cap + planned_position）
    if mc is None:
        add("capacity", "gap", "无规模数据，无法核容量上限")
    elif planned_position is not None and planned_position > c["capacity_fraction"] * mc:
        add("capacity", "fail",
            f"计划持仓 ¥{planned_position:,.0f} 超过规模的 {c['capacity_fraction']:.0%}（上限 ¥{c['capacity_fraction'] * mc:,.0f}）")
    else:
        add("capacity", "pass",
            "持仓占规模比例在容量上限内" if planned_position is not None else "未提供计划持仓，仅核规模存在")

    # 流动性（关键，需 turnover + planned_single_trade）
    if tv is None:
        add("liquidity", "gap", "近20日均成交额缺失（→降资格待复核）")
    elif planned_single_trade is not None and planned_single_trade > c["liquidity_fraction"] * tv:
        add("liquidity", "fail",
            f"计划单笔 ¥{planned_single_trade:,.0f} 超过日均成交额的 {c['liquidity_fraction']:.0%}（上限 ¥{c['liquidity_fraction'] * tv:,.0f}）")
    else:
        _tvtxt = f"{tv / 1e8:.2f} 亿" if tv >= 1e8 else f"{tv / 1e4:.0f} 万"
        add("liquidity", "pass", f"近20日均成交额约 ¥{_tvtxt}")

    # 折溢价（**执行时点**问题，不进长期准入门槛——仅信息展示；下单/调仓由「执行质量闸」按实时折溢价把关）
    if pr is None:
        add("premium", "info", "折溢价未取到（仅下单时参考，不影响长期准入）")
    elif abs(pr) > c["max_abs_premium"]:
        add("premium", "info", f"折溢价 {pr * 100:+.2f}%（偏离 ±{c['max_abs_premium'] * 100:.0f}%，下单时把关、不影响长期准入）")
    else:
        add("premium", "info", f"折溢价 {pr * 100:+.2f}%，接近净值")

    # 申购状态（关键）
    if not ps:
        add("purchase", "gap", "申购状态未知（westock 单源，缺失→降资格待复核）")
    elif any(k in ps for k in c["purchase_block_keys"]):
        add("purchase", "fail", f"申购受限：{ps}")
    else:
        add("purchase", "pass", f"申购状态：{ps}")

    # 上市年限（已知<门槛=fail 可人工豁免；未知=软 gap，不阻断）
    if ly is None:
        add("listed_years", "gap", "上市年限未知")
    elif ly < c["min_listed_years"]:
        add("listed_years", "fail", f"上市约 {ly:.1f} 年，低于最低 {c['min_listed_years']:.0f} 年")
    else:
        add("listed_years", "pass", f"上市约 {ly:.1f} 年")

    # 费率（软：缺失标记不阻断，§8.2 允许有据缺失）
    if fee.get("expense_ratio") is None:
        add("fee", "gap", "管理/托管费缺失（标记缺失，不阻断）")
    else:
        add("fee", "pass", f"综合费率约 {fee['expense_ratio'] * 100:.2f}%/年")

    blockers = [k["detail"] for k in checks if k["status"] == "fail"]
    data_gaps = [k["detail"] for k in checks if k["status"] == "gap"]
    crit_gap = any(k["status"] == "gap" and k["name"] in _CRITICAL_CHECKS for k in checks)
    admitted = (not blockers) and (not crit_gap)
    return {"admitted": admitted, "checks": checks, "blockers": blockers, "data_gaps": data_gaps}


# ─────────────────────────────────────────────────────────────
# §8.3 ETF 产品评分（硬准入之后才评分）。每子分记 score/status/confidence/detail；
#   缺失=None（绝不中性填补）；总分只在可得子分上按可得权重归一，并显式给覆盖率/置信度（§8.3）。
#   不因近期收益领先而提分（本评分不含收益项）。
# ─────────────────────────────────────────────────────────────
SCORING_WEIGHTS = {
    "tracking_quality": 0.25, "total_cost_quality": 0.20, "liquidity_quality": 0.20,
    "scale_and_survival_quality": 0.15, "premium_stability": 0.10, "operational_quality": 0.10,
}
_CRITICAL_SUBSCORES = {"total_cost_quality", "liquidity_quality", "scale_and_survival_quality"}


def _clamp01(x):
    return max(0.0, min(1.0, x))


def _sub(score, status, confidence, detail):
    return {"score": score, "status": status, "confidence": confidence, "detail": detail}


def score_tracking(cand):
    """跟踪质量(§8.4)。tracking_dispersion=年化"相对跟踪离散度"(best-effort，无全收益指数→非绝对TE)。"""
    td = cand.get("tracking_dispersion")
    if td is None:
        return _sub(None, "missing", "low", "跟踪数据未接入（无全收益指数→best-effort，Step 3 补）")
    s = _clamp01(1.0 - td / 0.05)                       # 0%→1，≥5%→0
    return _sub(round(s, 3), "degraded", "low", f"相对跟踪离散度约 {td * 100:.2f}%（非绝对TE，仅横向排序）")


def score_cost(cand):
    """总成本(§8.5)。显性持有成本=管理费+托管费（隐性跟踪偏离待 TE 接入）。"""
    er = (cand.get("fee") or {}).get("expense_ratio")
    if er is None:
        return _sub(None, "missing", "low", "管理/托管费缺失")
    s = _clamp01(1.0 - (er - 0.0015) / (0.010 - 0.0015))   # 0.15%→1，1.0%→0
    return _sub(round(s, 3), "ok", "high", f"综合费率约 {er * 100:.2f}%/年")


def score_liquidity(cand):
    tv = cand.get("avg_turnover_20d")
    if tv is None:
        return _sub(None, "missing", "low", "近20日成交额缺失")
    if tv >= 1e8:
        s = 1.0
    elif tv >= 5e7:
        s = 0.7 + 0.3 * (tv - 5e7) / 5e7
    elif tv >= 1e7:
        s = 0.3 + 0.4 * (tv - 1e7) / 4e7
    else:
        s = tv / 1e7 * 0.3
    txt = f"{tv / 1e8:.2f} 亿" if tv >= 1e8 else f"{tv / 1e4:.0f} 万"
    return _sub(round(_clamp01(s), 3), "ok", "high", f"20日均成交额约 {txt}")


def score_scale(cand):
    mc = cand.get("market_cap")
    if mc is None:
        return _sub(None, "missing", "low", "规模缺失")
    if mc >= 10e8:
        s = 1.0
    elif mc >= 2e8:
        s = 0.5 + 0.5 * (mc - 2e8) / 8e8
    elif mc >= 0.5e8:
        s = 0.1 + 0.4 * (mc - 0.5e8) / 1.5e8
    else:
        s = mc / 0.5e8 * 0.1
    return _sub(round(_clamp01(s), 3), "ok", "high", f"规模约 {mc / 1e8:.1f} 亿元")


def score_premium_stability(cand):
    """折溢价稳定(§8.5)。仅有实时点值→可评但置信度低（稳定性需时序，Step 3 补）。"""
    pr = cand.get("premium")
    if pr is None:
        return _sub(None, "missing", "low", "折溢价缺失")
    s = _clamp01(1.0 - abs(pr) / 0.03)                 # 0→1，±3%→0
    return _sub(round(s, 3), "degraded", "low", f"折溢价 {pr * 100:+.2f}%（仅实时点值，稳定性需时序）")


def score_operational(cand):
    ps = cand.get("purchase_status")
    if not ps:
        return _sub(None, "missing", "low", "申购状态未知")
    blocked = any(k in ps for k in ADMISSION_DEFAULTS["purchase_block_keys"])
    return _sub(0.4 if blocked else 1.0, "ok", "medium", f"申购状态：{ps}")


def product_score(cand, *, weights=None):
    """§8.3 综合产品分（纯函数）。返回 total(仅可得子分按可得权重归一) + coverage + confidence + 各子分。

    缺失子分=None 不计入、不中性填补；关键子分(成本/流动性/规模)缺失 → 降资格(status=degraded/insufficient)。
    """
    w = dict(SCORING_WEIGHTS)
    if weights:
        w.update(weights)
    subs = {
        "tracking_quality": score_tracking(cand),
        "total_cost_quality": score_cost(cand),
        "liquidity_quality": score_liquidity(cand),
        "scale_and_survival_quality": score_scale(cand),
        "premium_stability": score_premium_stability(cand),
        "operational_quality": score_operational(cand),
    }
    for k, v in subs.items():
        v["weight"] = w[k]
    avail_w = sum(w[k] for k, v in subs.items() if v["score"] is not None)
    weighted = sum(w[k] * v["score"] for k, v in subs.items() if v["score"] is not None)
    total = round(weighted / avail_w, 3) if avail_w > 0 else None
    coverage = round(avail_w, 3)                        # 权重合计=1 → 可得权重即覆盖率
    missing_crit = [k for k in _CRITICAL_SUBSCORES if subs[k]["score"] is None]
    # 选型/惩罚用「有效分」：关键子分(成本/流动性/规模)缺失 = 惩罚而非丢弃——把缺失关键子分
    # 的权重留在分母（视作 0 分），故信息贫乏产品不会因丢弃拖累项而 total 虚高反超透明产品。
    # 无关键缺失时 effective_total == total；关键全缺(numerator=0) 随 total → None（全额惩罚）。
    crit_missing_w = sum(w[k] for k in _CRITICAL_SUBSCORES if subs[k]["score"] is None)
    eff_denom = avail_w + crit_missing_w
    effective_total = round(weighted / eff_denom, 3) if eff_denom > 0 else None
    flags = []
    if missing_crit:
        flags.append("关键子分缺失（降资格/观察）：" + "、".join(missing_crit))
    if coverage < 0.5:
        status = "insufficient"
    elif missing_crit or coverage < 0.8:
        status = "degraded"
    else:
        status = "scored"
    confidence = "high" if status == "scored" else ("medium" if coverage >= 0.5 and not missing_crit else "low")
    return {"total": total, "effective_total": effective_total, "coverage": coverage,
            "status": status, "confidence": confidence, "subscores": subs, "flags": flags}


def _effective_score(score):
    """选型/惩罚读「有效分」：优先 effective_total（关键子分缺失已惩罚），无则回退 total。
    兼容手工构造的 quality dict（只含 total）；两者皆 None → None（调用方按全额惩罚处理）。"""
    eff = (score or {}).get("effective_total")
    return eff if eff is not None else (score or {}).get("total")


# ─────────────────────────────────────────────────────────────
# 三层目录骨架（§3.1 角色→暴露→产品）。v1 先做"角色→产品"映射 + 区间状态；
#   暴露层在 universe 的 index/proxy_index 里隐含，Phase C 再显式化。纯函数。
# ─────────────────────────────────────────────────────────────
def build_catalog(strat, port=None):
    """从 strategic_policy.roles + universe + 当前权重构建三层目录骨架（纯函数）。

    返回 {roles:[{role,tier,range,members:[{code,name,current_weight}],current_total,range_status}]}。
    range_status ∈ {within, below, above}（角色合计 vs 允许区间）。
    """
    sp = (strat or {}).get("strategic_policy") or {}
    roles = sp.get("roles") or {}
    uni = {str(u["code"]): u for u in ((strat or {}).get("universe") or [])}
    cur = {str(h.get("code")): float(h.get("target_weight") or 0)
           for h in ((port or {}).get("holdings") or [])}
    out = []
    for rid, rc in roles.items():
        members = [{"code": str(c), "name": (uni.get(str(c)) or {}).get("name") or str(c),
                    "current_weight": round(cur.get(str(c), 0.0), 4)}
                   for c in (rc.get("members") or [])]
        total = round(sum(m["current_weight"] for m in members), 4)
        rng = (rc.get("range") or [None, None])
        lo, hi = (rng + [None, None])[:2]
        status = "within"
        if lo is not None and total < lo - 1e-9:
            status = "below"
        elif hi is not None and total > hi + 1e-9:
            status = "above"
        out.append({"role": rid, "tier": rc.get("tier"), "range": [lo, hi],
                    "members": members, "current_total": total, "range_status": status})
    return {"roles": out}


# ─────────────────────────────────────────────────────────────
# §8.4 跟踪 / §7.3 重合 / §11 incumbent 处置（纯函数）。
# ─────────────────────────────────────────────────────────────
def tracking_dispersion(etf_returns, index_returns, *, periods_per_year=252):
    """年化"相对跟踪离散度"(§8.4 best-effort)。etf/index 为按日期对齐的周期收益序列(小数)。

    = std(etf_ret − index_ret) × sqrt(periods_per_year)。不足 20 个点返回 None（不输出伪精确）。
    注：指数腿若为价格指数(未含分红)，差值均值含分红缺口漂移，故只可横向排序、非绝对 TE。
    """
    n = min(len(etf_returns or []), len(index_returns or []))
    if n < 20:
        return None
    diffs = [float(etf_returns[i]) - float(index_returns[i]) for i in range(n)]
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    return round((var ** 0.5) * (periods_per_year ** 0.5), 6)


def weighted_jaccard(a, b):
    """加权 Jaccard 重合(§7.3)。a,b 为 {标的: 权重} dict。= Σ min / Σ max over 并集。

    任一为空 → None（无法判定，绝不默认低重合，§7.3）。QDII↔A股 成分不交集 → 自然为 0（非 bug）。
    """
    if not a or not b:
        return None
    keys = set(a) | set(b)
    num = sum(min(float(a.get(k, 0.0)), float(b.get(k, 0.0))) for k in keys)
    den = sum(max(float(a.get(k, 0.0)), float(b.get(k, 0.0))) for k in keys)
    return round(num / den, 4) if den > 0 else None


def incumbent_disposition(*, role_range_status, single_cap_exceeded=False, admitted=True,
                          redundant=False, has_blockers=True):
    """§11 incumbent 处置：keep / trim / review / review_data / replace_candidate（纯函数）。

    硬准入不过：有**真实阻断**(溢价/限购/规模/流动性等 has_blockers) → replace_candidate；
    **仅数据缺失**(无真实阻断、关键数据取不到，如周末/盘后/限频) → review_data（待复核、先持有、别带病加仓）。
    角色超区间或单卫星超上限 → trim（若同时冗余则 review 二选一）；仅冗余未超标 → review；否则 keep。
    """
    if admitted is False:
        return "replace_candidate" if has_blockers else "review_data"
    if role_range_status == "above" or single_cap_exceeded:
        return "review" if redundant else "trim"
    if redundant:
        return "review"
    return "keep"


def overlap_matrix(holdings_by_code):
    """两两加权 Jaccard 重合矩阵(§7.3)。holdings_by_code: {code:{stock:weight}}。

    返回 {code: {other: jaccard|None}}。无成分（None/空，如 债/金）或 QDII↔A股（成分不交集）→ 对应项 None/0。
    """
    codes = [c for c in (holdings_by_code or {})]
    out = {c: {} for c in codes}
    for i, a in enumerate(codes):
        for b in codes[i + 1:]:
            j = weighted_jaccard(holdings_by_code.get(a), holdings_by_code.get(b))
            out[a][b] = j
            out[b][a] = j
    return out


def assess_incumbents(strat, port, quality_by_code, *, asset_of=None,
                      holdings_by_code=None, overlap_threshold=0.30):
    """汇总 incumbent 审视表(§11)：角色/层/权重/区间/单卫星上限/准入/产品分/冗余/处置（纯函数）。

    quality_by_code: {code: {admission:{admitted}, score:{total,status}}}（已取好数；缺则该项 None）。
    冗余两路：① 结构精简(§6.3/§11)——同卫星角色 + 同 asset 的多成员 = 二选一候选(consolidation)；
             ② 持仓重合(§7.3)——同角色内加权 Jaccard ≥ 阈值（holdings_by_code 给时才算，否则 None）。
    任一路命中 → redundant，进 incumbent_disposition（超区间则评审二选一、未超则 review）。
    """
    sp = (strat or {}).get("strategic_policy") or {}
    single_max = (sp.get("caps") or {}).get("single_satellite_max")
    asset_of = asset_of or {str(u["code"]): u.get("asset") for u in ((strat or {}).get("universe") or [])}
    cat = build_catalog(strat, port)

    # ① 结构精简：卫星角色内同 asset 多成员 → 二选一候选
    consolidation = set()
    for r in cat["roles"]:
        if r["tier"] != "satellite":
            continue
        by_asset = {}
        for m in r["members"]:
            by_asset.setdefault(asset_of.get(m["code"]), []).append(m["code"])
        for grp in by_asset.values():
            if len(grp) >= 2:
                consolidation.update(grp)

    # ② 持仓重合：同角色内最大 Jaccard ≥ 阈值
    mat = overlap_matrix(holdings_by_code) if holdings_by_code else {}
    holdings_redundant, max_overlap = set(), {}
    for r in cat["roles"]:
        codes = [m["code"] for m in r["members"]]
        for a in codes:
            peers = [mat.get(a, {}).get(b) for b in codes if b != a]
            peers = [p for p in peers if p is not None]
            if peers:
                mx = max(peers)
                max_overlap[a] = round(mx, 4)
                if mx >= overlap_threshold:
                    holdings_redundant.add(a)

    rows = []
    for r in cat["roles"]:
        for m in r["members"]:
            code, w = m["code"], m["current_weight"]
            q = quality_by_code.get(code) or {}
            admission = q.get("admission") or {}
            adm = admission.get("admitted")
            has_blockers = bool(admission.get("blockers"))   # 区分"真实阻断"与"仅数据缺失"
            sc = q.get("score") or {}
            single_exceeded = bool(r["tier"] == "satellite" and single_max is not None and w > single_max + 1e-9)
            cons, hred = code in consolidation, code in holdings_redundant
            disp = incumbent_disposition(
                role_range_status=r["range_status"], single_cap_exceeded=single_exceeded,
                admitted=(adm if adm is not None else True), redundant=cons or hred, has_blockers=has_blockers)
            rows.append({
                "code": code, "name": m["name"], "role": r["role"], "tier": r["tier"],
                "current_weight": w, "role_range_status": r["range_status"],
                "single_cap_exceeded": single_exceeded,
                "admitted": adm, "has_blockers": has_blockers,
                "product_total": sc.get("total"), "product_status": sc.get("status"),
                "consolidation_candidate": cons, "holdings_redundant": hred,
                "max_same_role_overlap": max_overlap.get(code), "redundant": cons or hred,
                "disposition": disp,
            })
    return rows


def _deterministic_projection(weights, step=0.01):
    """§10.4 确定性投影：把已归一化权重按 step 量化，最大余数法保持「合计==1、各项≥0、确定性」。

    残差按小数余数大小公平分配（并列按下标升序），**不塞最大项**——同输入必得同输出。
    （Track C 唯一权威实现；app.py 别名复用。）
    """
    n = len(weights)
    if n == 0:
        return []
    w = [max(0.0, float(x)) for x in weights]
    s = sum(w)
    if s <= 0:
        return [0.0] * n
    w = [x / s for x in w]
    units = int(round(1.0 / step))
    raw = [x * units for x in w]
    floor = [int(r) for r in raw]
    deficit = units - sum(floor)
    order = sorted(range(n), key=lambda i: (-(raw[i] - floor[i]), i))
    for k in range(max(0, deficit)):
        floor[order[k % n]] += 1
    return [round(f * step, 10) for f in floor]


def _constrained_projection(weights, codes, feasible_fn, step=0.01):
    """Project to the weight grid while preserving all final constraints."""
    if not weights or len(weights) != len(codes):
        return None
    clean = [max(0.0, float(x)) for x in weights]
    total = sum(clean)
    if total <= 0:
        return None
    clean = [x / total for x in clean]
    units = int(round(1.0 / step))
    raw = [x * units for x in clean]
    floors = [int(x) for x in raw]
    deficit = units - sum(floors)
    if deficit < 0 or deficit > len(codes):
        return None
    ranked = sorted(range(len(codes)), key=lambda i: (-(raw[i] - floors[i]), codes[i]))
    best = None
    for chosen_tuple in combinations(ranked, deficit):
        chosen = set(chosen_tuple)
        projected = {codes[i]: round((floors[i] + (1 if i in chosen else 0)) * step, 10)
                     for i in range(len(codes))}
        projected = {c: w for c, w in projected.items() if w > 0}
        metrics = feasible_fn(projected)
        if metrics is None:
            continue
        error = sum((projected.get(codes[i], 0.0) - clean[i]) ** 2 for i in range(len(codes)))
        key = (round(error, 12), tuple(projected.get(c, 0.0) for c in sorted(codes)))
        if best is None or key < best[0]:
            best = (key, projected, metrics)
    return (best[1], best[2]) if best else None


# ─────────────────────────────────────────────────────────────
# §10 权威战略组合构建 v1（纯函数、确定性）。
#   角色网格候选 → §18 上限 + 压力预算拒绝 → 词典序选择 → 等权分配到产品 → 确定性投影 → 最终验证。
#   v1：单点收益 + 单情景压力。收益区间(§9.1)/收缩协方差(§9.2)/多情景压力(§9.3) 为 Phase C Step 2。
#   建议/回测/解释/应用须复用本函数（§10 唯一权威顺序）。
# ─────────────────────────────────────────────────────────────
COUNTRY_OF_ASSET = {
    "equity": "CN", "equity_defensive": "CN", "china_growth": "CN",
    "global_equity": "US", "global_growth": "US", "bond": None, "gold": None,
}
CURRENCY_OF_ASSET = {
    "equity": "CNY", "equity_defensive": "CNY", "china_growth": "CNY", "bond": "CNY",
    "global_equity": "USD", "global_growth": "USD", "gold": "USD",
}
EQUITY_ASSETS = {"equity", "equity_defensive", "china_growth", "global_equity", "global_growth"}
GROWTH_ASSETS = {"china_growth", "global_growth"}
RISK_CURRENCY_ASSETS = {"equity", "equity_defensive", "china_growth", "global_equity", "global_growth", "gold"}


def live_concentration_checks(holdings, asset_of, policy):
    """据真实 target_weight 体检 strategic_policy 集中度上限（货币/国家/卫星/成长/非卫星下限）。

    warn 口径（不硬拦）：返回单个 preflight 风格 check {id,label,status(pass/warn),message}。
    复用 construct 同一套 COUNTRY/CURRENCY/EQUITY/GROWTH/RISK_CURRENCY 映射（单一事实源），
    确保"对真实持仓的体检"与"对建议组合的硬约束"口径完全一致。
    """
    sp = policy or {}
    caps = (sp.get("caps") or {})
    roles = (sp.get("roles") or {})
    sat_codes = set()
    for r in roles.values():
        if (r or {}).get("tier") == "satellite":
            sat_codes.update(str(c) for c in ((r or {}).get("members") or []))
    weights = {}
    for h in (holdings or []):
        c = str(h.get("code"))
        weights[c] = weights.get(c, 0.0) + float(h.get("target_weight", 0) or 0)
    country_eq, risk_cur, single_sat = {}, {}, {}
    growth_total = sat_total = 0.0
    for c, w in weights.items():
        a = asset_of.get(c)
        if a in EQUITY_ASSETS and COUNTRY_OF_ASSET.get(a):
            country_eq[COUNTRY_OF_ASSET[a]] = country_eq.get(COUNTRY_OF_ASSET[a], 0.0) + w
        if a in RISK_CURRENCY_ASSETS and CURRENCY_OF_ASSET.get(a):
            risk_cur[CURRENCY_OF_ASSET[a]] = risk_cur.get(CURRENCY_OF_ASSET[a], 0.0) + w
        if a in GROWTH_ASSETS:
            growth_total += w
        if c in sat_codes:
            sat_total += w
            single_sat[c] = single_sat.get(c, 0.0) + w
    flags = []

    def _note(name, val, cap):
        if cap is None:
            return
        if val > cap + 1e-9:
            flags.append(f"{name} {val * 100:.0f}% 超过上限 {cap * 100:.0f}%")
        elif val >= cap - 1e-9:
            flags.append(f"{name} {val * 100:.0f}% 已达上限 {cap * 100:.0f}%")

    for cty, val in sorted(country_eq.items()):
        _note(f"{cty}股票合计", val, caps.get("single_country_equity_max"))
    for cur, val in sorted(risk_cur.items()):
        _note(f"{cur}风险货币暴露", val,
              caps.get("single_risk_currency_exposure_max", caps.get("single_currency_exposure_max")))
    _note("成长因子合计", growth_total, caps.get("growth_factor_max"))
    _note("卫星合计", sat_total, caps.get("satellite_max"))
    for c, val in sorted(single_sat.items()):
        _note(f"单一卫星 {c}", val, caps.get("single_satellite_max"))
    nonsat_min = caps.get("non_satellite_min")
    if nonsat_min is not None and (1.0 - sat_total) < nonsat_min - 1e-9:
        flags.append(f"非卫星合计 {(1.0 - sat_total) * 100:.0f}% 低于下限 {nonsat_min * 100:.0f}%")
    return {
        "id": "concentration_policy",
        "label": "集中度政策",
        "status": "warn" if flags else "pass",
        "message": ("触及长期政策集中度上限（提示·需人工确认，不阻断）：" + "；".join(flags)) if flags
                   else "货币/国家/卫星集中度均在长期政策上限内",
    }


def _enumerate_role_allocations(role_items, step):
    """枚举满足各角色区间、合计==1 的角色权重组合（确定性网格，递归 + 边界剪枝）。

    role_items: [(role, lo, hi)]。返回 [{role: weight}]。
    """
    units = int(round(1.0 / step))
    # 保守内逼近：下限 ceil、上限 floor（带 1e-9 消 FP 抖动）——网格点绝不低于政策下限或高于上限。
    # 旧 round() 会把非网格倍数的下限悄悄抬/压过界（如 floor 0.02→0.0 违下限、cap 0.08→0.10 违上限）。
    # 当区间窄于一格、放不下任何网格点时 lo_u>hi_u → 该角色枚举为空（病因由 _structural_infeasibility 显式给出）。
    bounds = [(r, max(0, math.ceil(lo / step - 1e-9)), math.floor(hi / step + 1e-9))
              for r, lo, hi in role_items]
    n = len(bounds)
    out = []

    def rec(i, remaining, acc):
        if i == n - 1:
            r, lo, hi = bounds[i]
            if lo <= remaining <= hi:
                d = {bounds[k][0]: round(acc[bounds[k][0]] * step, 6) for k in range(n - 1)}
                d[r] = round(remaining * step, 6)
                out.append(d)
            return
        r, lo, hi = bounds[i]
        later_lo = sum(b[1] for b in bounds[i + 1:])
        later_hi = sum(b[2] for b in bounds[i + 1:])
        umin = max(lo, remaining - later_hi)
        umax = min(hi, remaining - later_lo)
        for u in range(umin, umax + 1):
            acc[r] = u
            rec(i + 1, remaining - u, acc)
    rec(0, units, {})
    return out


def _unit_compositions(total, parts):
    """Deterministic non-negative integer compositions used for intra-role weights."""
    if parts <= 1:
        yield (total,)
        return
    for head in range(total + 1):
        for tail in _unit_compositions(total - head, parts - 1):
            yield (head,) + tail


def _enumerate_instrument_allocations(role_alloc, members_of, step):
    """Expand a role allocation into all on-grid member splits instead of implicit equal weights."""
    roles = list(role_alloc)

    def rec(i, acc):
        if i >= len(roles):
            yield dict(acc)
            return
        rid = roles[i]
        members = list(members_of.get(rid) or [])
        units = int(round(float(role_alloc[rid]) / step))
        if not members:
            return
        for split in _unit_compositions(units, len(members)):
            changed = []
            for code, amount in zip(members, split):
                if amount:
                    acc[code] = round(amount * step, 10)
                    changed.append(code)
            yield from rec(i + 1, acc)
            for code in changed:
                acc.pop(code, None)

    yield from rec(0, {})


def resolve_construct_budget(policy, max_drawdown):
    """Resolve absolute or margin-based construction stress budget from the policy."""
    policy = policy or {}

    def valid(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 0.80

    absolute = policy.get("construct_stress_budget")
    if valid(absolute):
        return float(absolute)
    margin = policy.get("construct_stress_margin")
    if valid(margin) and float(max_drawdown) - float(margin) > 0:
        return round(float(max_drawdown) - float(margin), 6)
    return float(max_drawdown)


def resolve_construct_target(policy, profile_target, planned_etf, stable_assets, stable_return=None):
    """Translate a whole-portfolio return target into the ETF-bucket target when requested."""
    policy = policy or {}
    basis = policy.get("target_return_basis") or "etf_bucket"
    stable_cfg = policy.get("stable_assets") or {}
    sr = stable_return if stable_return is not None else stable_cfg.get("expected_return", 0.0)
    sr = float(sr or 0.0)
    planned, stable = max(0.0, float(planned_etf or 0)), max(0.0, float(stable_assets or 0))
    total = planned + stable
    etf_share = planned / total if total > 0 else 1.0
    target = float(profile_target)
    if basis == "whole_portfolio" and etf_share > 0:
        target = (target - (1.0 - etf_share) * sr) / etf_share
    return {"basis": basis, "profile_target": float(profile_target),
            "construct_target": round(target, 6), "etf_share": round(etf_share, 6),
            "stable_return": round(sr, 6)}


def _structural_infeasibility(role_items, step, members_of, restricted_max):
    """枚举为空时给可读病因（显式告警替代静默 no_feasible）：
    ① 网格太粗——角色区间窄于一格、放不下任何 step 倍数点；
    ② 单/全受限成员 footgun——角色下限 > 其全部选中成员受限上限之和（失败准入的 incumbent
       被封顶在当前权重、无法抬到政策下限）。两者都保留人工覆盖：所有者可放宽下限或换/准入替代品。"""
    diags = []
    for rid, lo, hi in role_items:
        lo_u = max(0, math.ceil(lo / step - 1e-9))
        hi_u = math.floor(hi / step + 1e-9)
        if lo_u > hi_u:
            diags.append(f"role {rid} band [{lo:.1%}, {hi:.1%}] admits no weight on the {step:.0%} grid "
                         f"(widen the band or use a finer grid step)")
        members = members_of.get(rid) or []
        if lo > 1e-9 and members and all(code in restricted_max for code in members):
            cap = sum(restricted_max.get(code, 0.0) for code in members)
            if cap < lo - 1e-9:
                diags.append(f"role {rid} floor {lo:.1%} exceeds the restricted cap {cap:.1%} of its only "
                             f"member(s) {', '.join(members)} (held at current weight after failed admission, "
                             f"cannot be raised) — relax the floor or admit/replace the instrument")
    return diags


# （已删除：_construct_strategic_portfolio_legacy——零调用方的旧版构建，其投影不复验全套 caps，
#   防误用而移除（L15，2026-06-10 审查）。权威路径=construct_strategic_portfolio + _constrained_projection。）


def employment_resilience(profile):
    """Reserve stable assets for an employment shock before using them as risk buffer."""
    profile = profile or {}
    stable = max(0.0, float(profile.get("stable_assets_outside") or 0))
    expense = max(0.0, float(profile.get("unemployment_monthly_expense") or 0))
    income = max(0.0, float(profile.get("unemployment_minimum_monthly_income") or 0))
    years = max(0.0, float(profile.get("unemployment_runway_years") or 0))
    tail_months = max(0.0, float(profile.get("post_stress_reserve_months") or 0))
    monthly_gap = max(0.0, expense - income)
    runway_months = years * 12.0
    required = monthly_gap * (runway_months + tail_months)
    available = max(0.0, stable - required)
    shortfall = max(0.0, required - stable)
    return {
        "monthly_gap": round(monthly_gap, 2),
        "runway_months": round(runway_months, 2),
        "post_stress_reserve_months": round(tail_months, 2),
        "required_reserve": round(required, 2),
        "stable_assets": round(stable, 2),
        "risk_buffer_available": round(available, 2),
        "shortfall": round(shortfall, 2),
        "passes": shortfall <= 1e-9,
    }


def construct_strategic_portfolio(policy, *, returns, shocks, target_return,
                                  default_return=0.05, default_shock=-0.25, asset_of=None,
                                  etf_share=1.0, max_whole_stress=None, step=0.05,
                                  returns_conservative=None, scenarios=None,
                                  instrument_quality=None, exposure_of=None, covariance=None,
                                  incumbent_codes=None, incumbent_weights=None,
                                  require_quality=False, cov_stress_z=2.0,
                                  returns_by_code=None, returns_conservative_by_code=None):
    """Authoritative strategic construction with product selection and final validation.

    require_quality=True（live 调用，§8.2 阻断项 #1）：没有质量/准入记录的 code 按**未准入**处理
    （fail-closed）——in-portfolio 的封顶在当前权重（freeze），非持仓的剔除——绝不当成"已准入"放行。

    §0C #3 协方差进接受判定：除线性情景压力外，再算协方差隐含的全组合压力
    cov_stress = cov_stress_z × 年化波动 × etf_share（用真实相关、覆盖有协方差的子集）。默认仅披露；
    policy.caps 里 `enforce_cov_stress=true` → 作硬闸（cov_stress ≤ 预算），`min_effective_bets` → 分散度下限。

    returns_by_code / returns_conservative_by_code（可选）：逐只**前瞻锚定**预期收益（积木式：债券=当前YTM、
    A股=中性锚+估值回归、QDII=美债+ERP）。传入则替代按资产类的冻结 returns 进收益/排序——优化器据"锚在今天"
    的收益选权重；缺某 code 自动回退该资产类/默认值。**只影响排序（选哪个可行候选），不进可行性判定**
    （caps/stress/role 区间用权重，与收益无关），故可行性不变。不传 → 完全沿用冻结假设（向后兼容）。
    """
    cons_returns = returns_conservative or returns
    returns_by_code = {str(c): float(r) for c, r in (returns_by_code or {}).items()}
    returns_conservative_by_code = {str(c): float(r) for c, r in (returns_conservative_by_code or {}).items()}
    scen = scenarios or [{"name": "single", "shocks": shocks}]
    roles = (policy or {}).get("roles") or {}
    caps = (policy or {}).get("caps") or {}
    min_effective_bets = caps.get("min_effective_bets")            # §0C #3 分散度下限（opt-in，缺省 None=不闸）
    enforce_cov_stress = bool(caps.get("enforce_cov_stress"))      # §0C #3 协方差压力硬闸（opt-in，缺省只披露）
    min_covariance_coverage = caps.get("min_covariance_coverage")
    cov_stress_z = float(caps.get("cov_stress_z", cov_stress_z))   # 协方差压力的 sigma 倍数（缺省 2.0）
    priority = (policy or {}).get("selection_priority") or "return_first"
    asset_of = asset_of or {}
    exposure_of = exposure_of or {}
    instrument_quality = instrument_quality or {}
    incumbent_codes = {str(code) for code in (incumbent_codes or [])}
    incumbent_weights = {str(code): float(weight) for code, weight in (incumbent_weights or {}).items()}
    restricted_max = {}
    tier_of = {rid: rc.get("tier") for rid, rc in roles.items()}
    role_of, members_of, selected = {}, {}, {}
    selection_diags = []

    for rid, rc in roles.items():
        grouped = {}
        for raw_code in (rc.get("members") or []):
            code = str(raw_code)
            role_of[code] = rid
            quality = instrument_quality.get(code)
            admission = (quality or {}).get("admission") or {}
            admitted = admission.get("admitted")
            unverified = require_quality and (quality is None or admitted is None)
            product_risk_block = ((quality or {}).get("product_risk") or {}).get("level") == "block"
            if admitted is False or unverified or product_risk_block:
                if code in incumbent_codes:
                    # 三种成因（真实阻断 / 完全无质量记录 / 关键数据缺失）一律冻结在当前权重：
                    # admitted=False 的任何形态都不允许"带病加仓"（fail-closed，与 hard_admission
                    # "关键 gap → 不准入、不 fail-open"及 incumbent_disposition 的 review_data 语义一致）。
                    restricted_max[code] = incumbent_weights.get(code, 0.0)
                    if product_risk_block:
                        why = "product risk block freezes increases"
                    elif admission.get("blockers"):
                        why = "admission failure blocks increases"
                    elif unverified:
                        why = "quality data unavailable; held at current weight (fail-closed)"
                    else:
                        why = "admission data gaps require review; frozen at current weight (fail-closed)"
                    selection_diags.append(f"{code} retained provisionally at no more than current weight: {why}")
                else:
                    if product_risk_block:
                        why = "has a product risk block"
                    else:
                        why = "failed product admission" if admitted is False else "lacks verified quality data"
                    selection_diags.append(f"{code} {why} and was excluded")
                    continue
            grouped.setdefault(exposure_of.get(code) or code, []).append(code)
        primaries, backups = [], {}
        for exposure, codes in sorted(grouped.items()):
            def product_key(code):
                admission = (instrument_quality.get(code) or {}).get("admission") or {}
                score = (instrument_quality.get(code) or {}).get("score") or {}
                eff = _effective_score(score)            # 关键子分缺失已惩罚 → 信息贫乏产品不再虚高反超
                coverage = score.get("coverage")
                risk_block = (((instrument_quality.get(code) or {}).get("product_risk") or {}).get("level") == "block")
                provisional = 1 if admission.get("admitted") is False or risk_block else 0
                return (provisional, -(float(eff) if eff is not None else -1.0),
                        -(float(coverage) if coverage is not None else -1.0), code)
            ranked = sorted(codes, key=product_key)
            primaries.append(ranked[0])
            backups[exposure] = ranked[1:]
        members_of[rid] = primaries
        selected[rid] = {"primary": primaries, "backup": backups}

    role_items = [(rid, float((rc.get("range") or [0, 1])[0]),
                   float((rc.get("range") or [0, 1])[1])) for rid, rc in roles.items()]
    invalid_roles = [rid for rid, _lo, hi in role_items if hi > 0 and not members_of.get(rid)]
    if invalid_roles:
        return {"policy_allocation": {}, "instrument_allocation": {}, "metrics": {},
                "validation_status": "no_feasible_portfolio",
                "constraint_diagnostics": [f"roles have no eligible primary instrument: {', '.join(invalid_roles)}"],
                "selected_instruments": selected, "selection_diagnostics": selection_diags,
                "candidates_evaluated": 0, "feasible_count": 0, "selection_priority": priority}

    single_sat = caps.get("single_satellite_max")
    sat_max = caps.get("satellite_max")
    nonsat_min = caps.get("non_satellite_min")
    growth_max = caps.get("growth_factor_max")
    country_max = caps.get("single_country_equity_max")
    risk_currency_max = caps.get("single_risk_currency_exposure_max", caps.get("single_currency_exposure_max"))

    def _eff_return(code):       # 逐只前瞻锚定优先（returns_by_code），缺则回退该资产类/默认（冻结）。
        r = returns_by_code.get(code)
        return r if r is not None else returns.get(asset_of.get(code), default_return)

    def _eff_cons(code):
        r = returns_conservative_by_code.get(code)
        return r if r is not None else cons_returns.get(asset_of.get(code), default_return)

    def evaluate_instruments(inst):
        role_w, country_eq, currency_w, risk_currency_w = {}, {}, {}, {}
        exp = cons_exp = growth = max_single_sat = 0.0
        for code, weight in inst.items():
            rid = role_of.get(code)
            role_w[rid] = role_w.get(rid, 0.0) + weight
            if tier_of.get(rid) == "satellite":
                max_single_sat = max(max_single_sat, weight)
            asset = asset_of.get(code)
            exp += weight * _eff_return(code)
            cons_exp += weight * _eff_cons(code)
            if asset in GROWTH_ASSETS:
                growth += weight
            country = COUNTRY_OF_ASSET.get(asset)
            if asset in EQUITY_ASSETS and country:
                country_eq[country] = country_eq.get(country, 0.0) + weight
            currency = CURRENCY_OF_ASSET.get(asset)
            if currency:
                currency_w[currency] = currency_w.get(currency, 0.0) + weight
                if asset in RISK_CURRENCY_ASSETS:
                    risk_currency_w[currency] = risk_currency_w.get(currency, 0.0) + weight
        worst_loss, worst_name = 0.0, None
        for scenario in scen:
            loss = sum(weight * scenario["shocks"].get(asset_of.get(code), default_shock)
                       for code, weight in inst.items())
            if loss < worst_loss:
                worst_loss, worst_name = loss, scenario["name"]
        satellite = sum(weight for rid, weight in role_w.items() if tier_of.get(rid) == "satellite")
        quality_penalty = 0.0
        for code, weight in inst.items():
            score = _effective_score((instrument_quality.get(code) or {}).get("score"))
            quality_penalty += weight * (1.0 - float(score)) if score is not None else weight
        risk = risk_contributions(covariance, inst) if covariance else None
        # §0C #3 协方差隐含全组合压力（真实相关，覆盖有协方差的子集；未覆盖品种如成长卫星不计入 → 披露覆盖率）
        cov_vol = (risk or {}).get("vol")
        cov_labels = set(covariance["labels"]) if covariance else set()
        cov_covered = round(sum(w for code, w in inst.items() if code in cov_labels), 4)
        cov_stress = round(cov_stress_z * cov_vol * etf_share, 4) if cov_vol else None
        return {"inst": inst, "role_w": role_w, "exp": exp, "cons_exp": cons_exp,
                "whole_stress": abs(worst_loss) * etf_share, "worst_scenario": worst_name,
                "sat": satellite, "growth": growth, "country_eq": country_eq,
                "currency": currency_w, "risk_currency": risk_currency_w, "max_single_sat": max_single_sat,
                "quality_penalty": quality_penalty, "risk": risk,
                "cov_vol": cov_vol, "cov_stress": cov_stress, "cov_covered": cov_covered}

    def evaluate_roles(role_alloc):
        inst = {}
        for rid, weight in role_alloc.items():
            members = members_of[rid]
            each = weight / len(members)
            for code in members:
                inst[code] = inst.get(code, 0.0) + each
        return evaluate_instruments(inst)

    def violations(metrics):
        out = []
        if sat_max is not None and metrics["sat"] > sat_max + 1e-9:
            out.append(f"satellite total {metrics['sat']:.1%} exceeds {sat_max:.1%}")
        if nonsat_min is not None and 1.0 - metrics["sat"] < nonsat_min - 1e-9:
            out.append(f"non-satellite total {1.0 - metrics['sat']:.1%} below {nonsat_min:.1%}")
        if growth_max is not None and metrics["growth"] > growth_max + 1e-9:
            out.append(f"growth total {metrics['growth']:.1%} exceeds {growth_max:.1%}")
        if single_sat is not None and metrics["max_single_sat"] > single_sat + 1e-9:
            out.append(f"single satellite {metrics['max_single_sat']:.1%} exceeds {single_sat:.1%}")
        if country_max is not None and metrics["country_eq"] and max(metrics["country_eq"].values()) > country_max + 1e-9:
            out.append(f"single-country equity exceeds {country_max:.1%}")
        if risk_currency_max is not None and metrics["risk_currency"] and max(metrics["risk_currency"].values()) > risk_currency_max + 1e-9:
            out.append(f"single risk-currency exposure exceeds {risk_currency_max:.1%}")
        if max_whole_stress is not None and metrics["whole_stress"] > max_whole_stress + 1e-9:
            out.append(f"whole stress {metrics['whole_stress']:.1%} exceeds {max_whole_stress:.1%}")
        # §0C #3 协方差感知接受判定（opt-in，缺省关；开了也是只让"更安全"，不制造死胡同）
        if (enforce_cov_stress and max_whole_stress is not None and metrics.get("cov_stress") is not None
                and metrics["cov_stress"] > max_whole_stress + 1e-9):
            out.append(f"covariance stress {metrics['cov_stress']:.1%} exceeds {max_whole_stress:.1%}")
        if enforce_cov_stress and metrics.get("cov_stress") is None:
            out.append("covariance stress unavailable while enforcement is enabled")
        if (min_covariance_coverage is not None
                and metrics.get("cov_covered", 0.0) < float(min_covariance_coverage) - 1e-9):
            out.append(f"covariance coverage {metrics.get('cov_covered', 0.0):.1%} below {float(min_covariance_coverage):.1%}")
        if (min_effective_bets is not None and metrics.get("risk")
                and metrics["risk"].get("effective_bets", 0.0) < float(min_effective_bets) - 1e-9):
            out.append(f"effective risk sources {metrics['risk']['effective_bets']:.2f} below {float(min_effective_bets):.2f}")
        if min_effective_bets is not None and not metrics.get("risk"):
            out.append("effective risk sources unavailable while a minimum is configured")
        for code, maximum in restricted_max.items():
            if metrics["inst"].get(code, 0.0) > maximum + 1e-9:
                out.append(f"restricted incumbent {code} exceeds current weight {maximum:.1%}")
        for rid, lo, hi in role_items:
            actual = metrics["role_w"].get(rid, 0.0)
            if actual < lo - 1e-9 or actual > hi + 1e-9:
                out.append(f"role {rid} weight {actual:.1%} outside [{lo:.1%}, {hi:.1%}]")
        return out

    def sort_key(metrics):
        gap = round(max(0.0, target_return - metrics["cons_exp"]), 4)
        stress = round(metrics["whole_stress"], 4)
        ret_term = round(-metrics["exp"], 4)
        risk_term = round(-(metrics["risk"] or {}).get("effective_bets", 0.0), 4)
        role_balance = round(sum(weight * weight for weight in metrics["role_w"].values()), 4)
        quality_term = round(metrics["quality_penalty"], 4)
        count = sum(1 for weight in metrics["inst"].values() if weight > 1e-9)
        if priority == "defensive_first":
            return (stress, gap, risk_term, quality_term, role_balance, ret_term, count)
        if priority == "balanced":
            return (gap, stress, risk_term, quality_term, role_balance, ret_term, count)
        return (gap, ret_term, stress, risk_term, quality_term, role_balance, count)

    candidates = _enumerate_role_allocations(role_items, step)
    feasible_candidates = []
    instrument_candidates_evaluated = 0
    for role_alloc in candidates:
        for inst in _enumerate_instrument_allocations(role_alloc, members_of, step):
            instrument_candidates_evaluated += 1
            metrics = evaluate_instruments(inst)
            if not violations(metrics):
                feasible_candidates.append((role_alloc, metrics))
    if not feasible_candidates:
        structural = _structural_infeasibility(role_items, step, members_of, restricted_max)
        return {"policy_allocation": {}, "instrument_allocation": {}, "metrics": {},
                "validation_status": "no_feasible_portfolio",
                "constraint_diagnostics": structural or ["no portfolio satisfies policy and stress constraints"],
                "selected_instruments": selected, "selection_diagnostics": selection_diags,
                "candidates_evaluated": instrument_candidates_evaluated, "role_candidates_evaluated": len(candidates),
                "feasible_count": 0, "selection_priority": priority}

    feasible_candidates.sort(key=lambda item: sort_key(item[1]))
    _best_role_alloc, best = feasible_candidates[0]
    codes = sorted(best["inst"])
    projected = _constrained_projection(
        [best["inst"][code] for code in codes], codes,
        lambda inst: (lambda m: m if not violations(m) else None)(evaluate_instruments(inst)),
        step=0.01)
    if projected is None:
        return {"policy_allocation": {}, "instrument_allocation": {}, "metrics": {},
                "validation_status": "no_feasible_portfolio",
                "constraint_diagnostics": ["no feasible final allocation after constrained rounding"],
                "selected_instruments": selected, "selection_diagnostics": selection_diags,
                "candidates_evaluated": instrument_candidates_evaluated, "role_candidates_evaluated": len(candidates),
                "feasible_count": len(feasible_candidates),
                "selection_priority": priority}
    allocation, final = projected
    diagnostics = violations(final)
    if abs(sum(allocation.values()) - 1.0) > 1e-9:
        diagnostics.append("final weights do not sum to 100%")
    # 冻结假设口径的对照 blend（按资产类 returns 重算最终配置）——给 UI 标"若用冻结假设是多少"；
    # 无 returns_by_code 时它就等于 expected_etf_return。
    frozen_exp = sum(weight * returns.get(asset_of.get(code), default_return)
                     for code, weight in allocation.items())
    if final["cons_exp"] >= target_return - 1e-9:
        target_feasibility = "met_conservative"
    elif final["exp"] >= target_return - 1e-9:
        target_feasibility = "central_only"
    else:
        target_feasibility = "unmet"
    target_hard_gate = bool((policy or {}).get("target_return_hard_gate", False))
    if diagnostics:
        decision_status = "blocked"
    elif target_feasibility == "met_conservative":
        decision_status = "ready"
    elif target_hard_gate:
        decision_status = "review_required"
    else:
        decision_status = "ready_with_warning"
    return {
        "policy_allocation": {rid: round(final["role_w"].get(rid, 0.0), 4) for rid in roles},
        "instrument_allocation": {code: round(weight, 4) for code, weight in allocation.items()},
        "metrics": {
            "expected_etf_return": round(final["exp"], 4),
            "expected_etf_return_conservative": round(final["cons_exp"], 4),
            "expected_etf_return_frozen": round(frozen_exp, 4),
            "return_basis": "anchored" if (returns_by_code or returns_conservative_by_code) else "frozen",
            "whole_portfolio_stress": round(final["whole_stress"], 4),
            "worst_scenario": final["worst_scenario"],
            "satellite_total": round(final["sat"], 4),
            "growth_factor_total": round(final["growth"], 4),
            "country_equity": {key: round(value, 4) for key, value in final["country_eq"].items()},
            "currency_exposure": {key: round(value, 4) for key, value in final["currency"].items()},
            "risk_currency_exposure": {key: round(value, 4) for key, value in final["risk_currency"].items()},
            "effective_risk_sources": (final["risk"] or {}).get("effective_bets"),
            # §0C #3 协方差进接受判定：披露协方差隐含压力与覆盖率（线性情景压力的真实-相关对照）
            "covariance_vol": final.get("cov_vol"),
            "covariance_stress": final.get("cov_stress"),
            "covariance_covered_weight": final.get("cov_covered"),
            "covariance_stress_z": round(cov_stress_z, 2),
            "covariance_estimator": (covariance or {}).get("estimator"),
            "product_quality_penalty": round(final["quality_penalty"], 4),
            "target_return": round(target_return, 4),
            "target_gap": round(max(0.0, target_return - final["exp"]), 4),
            "target_gap_conservative": round(max(0.0, target_return - final["cons_exp"]), 4),
        },
        "validation_status": "passed" if not diagnostics else "violated",
        "constraint_status": "passed" if not diagnostics else "violated",
        "target_feasibility": target_feasibility,
        "target_return_hard_gate": target_hard_gate,
        "decision_status": decision_status,
        "constraint_diagnostics": diagnostics,
        "selected_instruments": selected,
        "selection_diagnostics": selection_diags,
        "candidates_evaluated": instrument_candidates_evaluated,
        "role_candidates_evaluated": len(candidates),
        "feasible_count": len(feasible_candidates),
        "selection_priority": priority,
    }


# 角色/层级用途说明（中文，给"为什么这样配"用；角色优先，缺失退回层级）。
_ROLE_PURPOSE = {
    "china_core_equity": "A 股核心权益：组合长期收益的主引擎之一。",
    "us_core_equity": "美股核心权益：跨市场分散，捕捉全球龙头。",
    "defensive_equity": "防御型权益（红利低波）：波动低于宽基，靠股息提供缓冲。",
    "growth_satellite": "成长卫星：博取更高弹性收益，波动大、用上限控制风险。",
    "government_bond": "国债压舱石：低波缓冲，压低组合最坏回撤。",
    "gold": "黄金分散器：与股票相关性低，危机时对冲。",
}
_TIER_PURPOSE = {
    "core": "核心仓：长期收益主力。",
    "core_defensive": "核心防御：低波压舱。",
    "diversifier": "分散器：降低相关性、平滑波动。",
    "satellite": "卫星仓：增强收益，单独设上限。",
}


def build_construct_rationale(policy, snapshot, *, name_of=None,
                             quality=None, incumbent_weights=None):
    """把权威构建结果翻成"为什么是这几只 ETF、为什么是这个比例"（纯函数、可单测）。

    只读 snapshot 已有字段（policy_allocation / instrument_allocation /
    selected_instruments / metrics）+ policy 的角色区间与上限，不重算配置。
    返回 {objective, roles:[{role, tier, weight, range, purpose, band, members:[{code,name,weight,reason}]}], notes:[str]}。
    无可行组合（policy_allocation 为空）时 roles/notes 为空、仅保留 objective。
    """
    name_of = name_of or {}
    quality = quality or {}
    incumbent_weights = {str(k): v for k, v in (incumbent_weights or {}).items()}
    roles_cfg = (policy or {}).get("roles") or {}
    caps = (policy or {}).get("caps") or {}
    pol = snapshot.get("policy_allocation") or {}
    inst = {str(k): float(v) for k, v in (snapshot.get("instrument_allocation") or {}).items()}
    selected = snapshot.get("selected_instruments") or {}
    metrics = snapshot.get("metrics") or {}
    budget = snapshot.get("construct_stress_budget")

    def _is_frozen(code):
        adm = ((quality.get(code) or {}).get("admission") or {})
        return adm.get("admitted") is False and code in incumbent_weights

    objective = (
        "在你为每个角色设定的允许区间、以及组合级上限之内，算法按固定优先级求解："
        "① 先尽量缩小『保守口径下的收益缺口』（用偏低的保守收益假设，算离目标还差多少）；"
        "② 再把最坏压力情景下的全组合回撤压到预算"
        + (f"（≤{budget * 100:.0f}%）" if isinstance(budget, (int, float)) and not isinstance(budget, bool) else "")
        + " 以内；③ 最后在收益与集中度之间取舍。所有收益均为长期假设、非承诺。"
    )

    roles_out = []
    for rid, w in sorted(pol.items(), key=lambda kv: -float(kv[1] or 0)):
        w = float(w or 0)
        if w <= 0:
            continue
        rc = roles_cfg.get(rid) or {}
        rng = rc.get("range") or [0, 1]
        lo, hi = float(rng[0]), float(rng[1])
        tier = rc.get("tier")
        purpose = _ROLE_PURPOSE.get(rid) or _TIER_PURPOSE.get(tier) or ""
        if lo > 0 and abs(w - lo) < 1e-6:
            band = f"落在区间下限 {lo * 100:.0f}%：在满足其它目标前提下尽量少配。"
        elif abs(w - hi) < 1e-6:
            band = f"顶到区间上限 {hi * 100:.0f}%：已配到该角色允许的最大比例。"
        else:
            band = f"在区间 {lo * 100:.0f}–{hi * 100:.0f}% 内由算法选定。"
        primaries = ((selected.get(rid) or {}).get("primary")) or []
        member_codes = [str(c) for c in (rc.get("members") or [])]
        members = []
        for code in primaries:
            code = str(code)
            cw = inst.get(code, 0.0)
            if cw <= 0:
                continue
            if _is_frozen(code):
                reason = "因当前限购，冻结在你现有权重、不再加仓。"
            elif len(member_codes) <= 1:
                reason = "该角色下唯一可交易品种。"
            else:
                reason = "在同类候选中准入通过、质量分领先而入选。"
            members.append({"code": code, "name": name_of.get(code) or code,
                            "weight": round(cw, 4), "reason": reason})
        roles_out.append({"role": rid, "tier": tier, "weight": round(w, 4),
                          "range": [lo, hi], "purpose": purpose,
                          "band": band, "members": members})

    notes = []
    sat = metrics.get("satellite_total")
    growth = metrics.get("growth_factor_total")
    if isinstance(sat, (int, float)) and caps.get("satellite_max") and sat >= float(caps["satellite_max"]) - 1e-6:
        notes.append(f"卫星合计 {sat * 100:.0f}% 已顶到上限 {float(caps['satellite_max']) * 100:.0f}%。")
    if isinstance(growth, (int, float)) and caps.get("growth_factor_max") and growth >= float(caps["growth_factor_max"]) - 1e-6:
        notes.append(f"成长因子合计 {growth * 100:.0f}% 已顶到上限 {float(caps['growth_factor_max']) * 100:.0f}%。")
    frozen = [c for c in inst if inst.get(c, 0) > 0 and _is_frozen(c)]
    if frozen:
        notes.append("限购冻结：" + "、".join(name_of.get(c) or c for c in frozen) + " 维持现有权重、暂不加仓。")

    return {"objective": objective, "roles": roles_out, "notes": notes}


def derive_comparison_portfolios(constructed, current, asset_of, tier_of):
    """§12.3 必比基准：当前 / 权威构建 / 仅核心 / 无卫星 / 无黄金 / 更低权益（纯函数，各自归一）。

    目的：证明复杂度带来增量价值——若"仅核心/无卫星"在风险与成本上不劣于构建组合，构建组合不该通过(§16.3)。
    消融以"权威构建"为基底。constructed/current: {code: weight}。
    """
    def renorm(d):
        s = sum(v for v in d.values() if v > 0)
        return {k: round(v / s, 4) for k, v in d.items() if v > 0} if s > 0 else {}

    eq = {"equity", "equity_defensive", "china_growth", "global_equity", "global_growth"}
    base = constructed or {}
    return {
        "当前": renorm(dict(current or {})),
        "权威构建": renorm(dict(base)),
        "仅核心": renorm({c: w for c, w in base.items() if tier_of.get(c) in ("core", "core_defensive")}),
        "无卫星": renorm({c: w for c, w in base.items() if tier_of.get(c) != "satellite"}),
        "无黄金": renorm({c: w for c, w in base.items() if asset_of.get(c) != "gold"}),
        "更低权益": renorm({c: (w * 0.7 if asset_of.get(c) in eq else w) for c, w in base.items()}),
    }


# ─────────────────────────────────────────────────────────────
# §9.2 收缩协方差 + §12.1 风险贡献（纯 python，小矩阵；周频收益为主频率）。
#   退化：观测不足 → None（不输出统计优化结果，回退仅压力情景，§9.2）。
# ─────────────────────────────────────────────────────────────
def shrinkage_covariance(returns_by_label, *, shrink=0.3, min_obs=20):
    """固定强度恒定相关收缩协方差；不是数据估计强度的 Ledoit-Wolf 实现。

    returns_by_label: {label: [周期收益]}。返回 {labels, matrix, obs, avg_corr, shrink} | None（不足）。
    """
    labels = sorted(returns_by_label)
    series = [list(returns_by_label[l]) for l in labels]
    n = min((len(s) for s in series), default=0)
    k = len(labels)
    if n < min_obs or k < 2:
        return None
    series = [s[-n:] for s in series]
    means = [sum(s) / n for s in series]
    S = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(i, k):
            cov = sum((series[i][t] - means[i]) * (series[j][t] - means[j]) for t in range(n)) / (n - 1)
            S[i][j] = S[j][i] = cov
    var = [S[i][i] for i in range(k)]
    std = [v ** 0.5 if v > 0 else 0.0 for v in var]
    corrs = [S[i][j] / (std[i] * std[j]) for i in range(k) for j in range(i + 1, k) if std[i] > 0 and std[j] > 0]
    rbar = sum(corrs) / len(corrs) if corrs else 0.0
    d = max(0.0, min(1.0, shrink))
    M = [[(1 - d) * S[i][j] + d * (var[i] if i == j else rbar * std[i] * std[j]) for j in range(k)]
         for i in range(k)]
    return {"labels": labels, "matrix": M, "obs": n, "avg_corr": round(rbar, 4), "shrink": d,
            "estimator": "fixed_constant_correlation_shrinkage"}


def _quad_form(cov, weights):
    labels, M = cov["labels"], cov["matrix"]
    w = [float(weights.get(l, 0.0)) for l in labels]
    sw = [sum(M[i][j] * w[j] for j in range(len(w))) for i in range(len(w))]
    return w, sw, sum(w[i] * sw[i] for i in range(len(w)))


def portfolio_volatility(cov, weights, *, annualize=52.0):
    """组合年化波动 = sqrt(wᵀΣw × annualize)（周频 → annualize=52）。"""
    _w, _sw, var = _quad_form(cov, weights)
    return round((max(0.0, var) * annualize) ** 0.5, 6)


def risk_contributions(cov, weights, *, annualize=52.0):
    """§12.1 风险贡献分解 + 有效风险来源数。返回 {contributions:{label:占比}, effective_bets, vol} | None。"""
    w, sw, var = _quad_form(cov, weights)
    if var <= 0:
        return None
    labels = cov["labels"]
    rc = [w[i] * sw[i] / var for i in range(len(w))]              # 归一风险贡献（合计=1）
    contrib = {labels[i]: round(rc[i], 4) for i in range(len(w)) if abs(rc[i]) > 1e-9}
    eff = 1.0 / sum(x * x for x in rc) if any(rc) else 0.0        # 有效风险来源数（风险贡献 HHI 倒数）
    return {"contributions": contrib, "effective_bets": round(eff, 2),
            "vol": round((var * annualize) ** 0.5, 4)}
