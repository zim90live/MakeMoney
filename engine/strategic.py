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
    total = (round(sum(w[k] * v["score"] for k, v in subs.items() if v["score"] is not None) / avail_w, 3)
             if avail_w > 0 else None)
    coverage = round(avail_w, 3)                        # 权重合计=1 → 可得权重即覆盖率
    missing_crit = [k for k in _CRITICAL_SUBSCORES if subs[k]["score"] is None]
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
    return {"total": total, "coverage": coverage, "status": status,
            "confidence": confidence, "subscores": subs, "flags": flags}


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


def _enumerate_role_allocations(role_items, step):
    """枚举满足各角色区间、合计==1 的角色权重组合（确定性网格，递归 + 边界剪枝）。

    role_items: [(role, lo, hi)]。返回 [{role: weight}]。
    """
    units = int(round(1.0 / step))
    bounds = [(r, max(0, int(round(lo / step))), int(round(hi / step))) for r, lo, hi in role_items]
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


def _construct_strategic_portfolio_legacy(policy, *, returns, shocks, target_return,
                                          default_return=0.05, default_shock=-0.25, asset_of=None,
                                          etf_share=1.0, max_whole_stress=None, step=0.05,
                                          returns_conservative=None, scenarios=None):
    """§10 权威战略组合构建。policy=strategic_policy(roles/caps/selection_priority)。

    returns/shocks: {asset: 假设}（load_assumptions）。asset_of: {code: asset}。
    §9.1 收益区间：returns_conservative 给时，词典序的"目标缺口"按**保守**口径（缺省回退 central）。
    §9.3 多情景压力：scenarios=[{name,shocks}] 给时取**最坏情景**损失（缺省回退 shocks 单情景）。
    返回 snapshot：policy_allocation / instrument_allocation / metrics / validation_status / diagnostics。
    无可行解 → validation_status='no_feasible_portfolio'（绝不返回超预算建议，§10.4）。
    """
    cons_returns = returns_conservative or returns
    scen = scenarios if scenarios else [{"name": "single", "shocks": shocks}]
    roles = (policy or {}).get("roles") or {}
    caps = (policy or {}).get("caps") or {}
    priority = (policy or {}).get("selection_priority") or "return_first"
    asset_of = asset_of or {}
    members_of = {rid: [str(c) for c in (rc.get("members") or [])] for rid, rc in roles.items()}
    tier_of = {rid: rc.get("tier") for rid, rc in roles.items()}
    role_items = [(rid, (rc.get("range") or [0, 1])[0], (rc.get("range") or [0, 1])[1])
                  for rid, rc in roles.items()]

    single_sat = caps.get("single_satellite_max")
    sat_max = caps.get("satellite_max")
    nonsat_min = caps.get("non_satellite_min")
    growth_max = caps.get("growth_factor_max")
    country_max = caps.get("single_country_equity_max")
    risk_currency_max = caps.get("single_risk_currency_exposure_max", caps.get("single_currency_exposure_max"))

    def evaluate(role_alloc):
        inst, max_single_sat = {}, 0.0
        asset_w, country_eq, currency_w, risk_currency_w = {}, {}, {}, {}
        exp, cons_exp, growth = 0.0, 0.0, 0.0
        for rid, w in role_alloc.items():
            mem = members_of.get(rid) or []
            if not mem:
                continue
            each = w / len(mem)
            if tier_of.get(rid) == "satellite":
                max_single_sat = max(max_single_sat, each)
            for c in mem:
                inst[c] = inst.get(c, 0.0) + each
        for c, w in inst.items():
            a = asset_of.get(c)
            asset_w[a] = asset_w.get(a, 0.0) + w
            exp += w * returns.get(a, default_return)
            cons_exp += w * cons_returns.get(a, default_return)
            if a in GROWTH_ASSETS:
                growth += w
            if a in EQUITY_ASSETS and COUNTRY_OF_ASSET.get(a):
                country_eq[COUNTRY_OF_ASSET[a]] = country_eq.get(COUNTRY_OF_ASSET[a], 0.0) + w
            if CURRENCY_OF_ASSET.get(a):
                currency_w[CURRENCY_OF_ASSET[a]] = currency_w.get(CURRENCY_OF_ASSET[a], 0.0) + w
                if a in RISK_CURRENCY_ASSETS:
                    risk_currency_w[CURRENCY_OF_ASSET[a]] = risk_currency_w.get(CURRENCY_OF_ASSET[a], 0.0) + w
        # §9.3 最坏情景损失（负=损失；正收益情景不计为压力）
        worst_loss, worst_name = 0.0, None
        for sc in scen:
            port = sum(w * sc["shocks"].get(asset_of.get(c), default_shock) for c, w in inst.items())
            if port < worst_loss:
                worst_loss, worst_name = port, sc["name"]
        sat = sum(w for rid, w in role_alloc.items() if tier_of.get(rid) == "satellite")
        return {"inst": inst, "exp": exp, "cons_exp": cons_exp,
                "whole_stress": abs(worst_loss) * etf_share, "worst_scenario": worst_name,
                "sat": sat, "growth": growth, "country_eq": country_eq, "currency": currency_w,
                "risk_currency": risk_currency_w,
                "max_single_sat": max_single_sat}

    def feasible(m):
        if sat_max is not None and m["sat"] > sat_max + 1e-9:
            return False
        if nonsat_min is not None and (1.0 - m["sat"]) < nonsat_min - 1e-9:
            return False
        if growth_max is not None and m["growth"] > growth_max + 1e-9:
            return False
        if single_sat is not None and m["max_single_sat"] > single_sat + 1e-9:
            return False
        if country_max is not None and m["country_eq"] and max(m["country_eq"].values()) > country_max + 1e-9:
            return False
        if risk_currency_max is not None and m["risk_currency"] and max(m["risk_currency"].values()) > risk_currency_max + 1e-9:
            return False
        if max_whole_stress is not None and m["whole_stress"] > max_whole_stress + 1e-9:
            return False
        return True

    def sort_key(m):
        gap = round(max(0.0, target_return - m["cons_exp"]), 4)   # §10.3：保守收益情景下的目标缺口
        stress = round(m["whole_stress"], 4)
        ret_term = round(-m["exp"], 4)
        bal_term = round(sum(w * w for w in m["inst"].values()), 4)
        ninst = sum(1 for w in m["inst"].values() if w > 1e-9)
        if priority == "defensive_first":
            return (gap, stress, stress, bal_term, ret_term, ninst)
        if priority == "balanced":
            return (gap, stress, bal_term, ret_term, ninst)
        return (gap, stress, ret_term, bal_term, ninst)        # return_first（默认）

    candidates = _enumerate_role_allocations(role_items, step)
    feas = [(ra, m) for ra, m in ((ra, evaluate(ra)) for ra in candidates) if feasible(m)]
    if not feas:
        return {"policy_allocation": {}, "instrument_allocation": {}, "metrics": {},
                "validation_status": "no_feasible_portfolio",
                "constraint_diagnostics": ["在 §18 上限 + 压力预算下无可行组合（放宽区间/上限或降目标）"],
                "candidates_evaluated": len(candidates), "feasible_count": 0,
                "selection_priority": priority}

    feas.sort(key=lambda x: sort_key(x[1]))
    best_ra, best_m = feas[0]
    codes = sorted(best_m["inst"])
    proj = _deterministic_projection([best_m["inst"][c] for c in codes], step=0.01)
    instrument_allocation = {c: w for c, w in zip(codes, proj) if w > 0}

    diags = []
    wsum = sum(instrument_allocation.values())
    if abs(wsum - 1.0) > 1e-3:
        diags.append(f"合计 {wsum:.3f} ≠ 1")
    if max_whole_stress is not None and best_m["whole_stress"] > max_whole_stress + 5e-3:
        diags.append(f"全组合压力 {best_m['whole_stress']:.1%} 超预算 {max_whole_stress:.1%}")
    status = "passed" if not diags else "violated"
    return {
        "policy_allocation": {k: round(v, 4) for k, v in best_ra.items()},
        "instrument_allocation": {c: round(w, 4) for c, w in instrument_allocation.items()},
        "metrics": {
            "expected_etf_return": round(best_m["exp"], 4),
            "expected_etf_return_conservative": round(best_m["cons_exp"], 4),
            "whole_portfolio_stress": round(best_m["whole_stress"], 4),
            "worst_scenario": best_m["worst_scenario"],
            "satellite_total": round(best_m["sat"], 4),
            "growth_factor_total": round(best_m["growth"], 4),
            "country_equity": {k: round(v, 4) for k, v in best_m["country_eq"].items()},
            "currency_exposure": {k: round(v, 4) for k, v in best_m["currency"].items()},
            "risk_currency_exposure": {k: round(v, 4) for k, v in best_m["risk_currency"].items()},
            "target_return": round(target_return, 4),
            "target_gap": round(max(0.0, target_return - best_m["exp"]), 4),
            "target_gap_conservative": round(max(0.0, target_return - best_m["cons_exp"]), 4),
        },
        "validation_status": status, "constraint_diagnostics": diags,
        "candidates_evaluated": len(candidates), "feasible_count": len(feas),
        "selection_priority": priority,
    }


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
                                  require_quality=False):
    """Authoritative strategic construction with product selection and final validation.

    require_quality=True（live 调用，§8.2 阻断项 #1）：没有质量/准入记录的 code 按**未准入**处理
    （fail-closed）——in-portfolio 的封顶在当前权重（freeze），非持仓的剔除——绝不当成"已准入"放行。
    """
    cons_returns = returns_conservative or returns
    scen = scenarios or [{"name": "single", "shocks": shocks}]
    roles = (policy or {}).get("roles") or {}
    caps = (policy or {}).get("caps") or {}
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
            if admitted is False or unverified:
                if code in incumbent_codes:
                    if admission.get("blockers") or unverified:
                        restricted_max[code] = incumbent_weights.get(code, 0.0)
                        why = ("admission failure blocks increases" if admission.get("blockers")
                               else "quality data unavailable; held at current weight (fail-closed)")
                        selection_diags.append(f"{code} retained provisionally at no more than current weight: {why}")
                    else:
                        selection_diags.append(f"{code} retained provisionally: admission data gaps require review")
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
                total = score.get("total")
                coverage = score.get("coverage")
                provisional = 1 if admission.get("admitted") is False else 0
                return (provisional, -(float(total) if total is not None else -1.0),
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

    def evaluate_instruments(inst):
        role_w, country_eq, currency_w, risk_currency_w = {}, {}, {}, {}
        exp = cons_exp = growth = max_single_sat = 0.0
        for code, weight in inst.items():
            rid = role_of.get(code)
            role_w[rid] = role_w.get(rid, 0.0) + weight
            if tier_of.get(rid) == "satellite":
                max_single_sat = max(max_single_sat, weight)
            asset = asset_of.get(code)
            exp += weight * returns.get(asset, default_return)
            cons_exp += weight * cons_returns.get(asset, default_return)
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
            score = ((instrument_quality.get(code) or {}).get("score") or {}).get("total")
            quality_penalty += weight * (1.0 - float(score)) if score is not None else weight
        risk = risk_contributions(covariance, inst) if covariance else None
        return {"inst": inst, "role_w": role_w, "exp": exp, "cons_exp": cons_exp,
                "whole_stress": abs(worst_loss) * etf_share, "worst_scenario": worst_name,
                "sat": satellite, "growth": growth, "country_eq": country_eq,
                "currency": currency_w, "risk_currency": risk_currency_w, "max_single_sat": max_single_sat,
                "quality_penalty": quality_penalty, "risk": risk}

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
            return (gap, stress, risk_term, role_balance, quality_term, ret_term, count)
        if priority == "balanced":
            return (gap, stress, risk_term, role_balance, quality_term, ret_term, count)
        return (gap, stress, ret_term, risk_term, role_balance, quality_term, count)

    candidates = _enumerate_role_allocations(role_items, step)
    feasible_candidates = []
    for role_alloc in candidates:
        metrics = evaluate_roles(role_alloc)
        if not violations(metrics):
            feasible_candidates.append((role_alloc, metrics))
    if not feasible_candidates:
        return {"policy_allocation": {}, "instrument_allocation": {}, "metrics": {},
                "validation_status": "no_feasible_portfolio",
                "constraint_diagnostics": ["no portfolio satisfies policy and stress constraints"],
                "selected_instruments": selected, "selection_diagnostics": selection_diags,
                "candidates_evaluated": len(candidates), "feasible_count": 0, "selection_priority": priority}

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
                "candidates_evaluated": len(candidates), "feasible_count": len(feasible_candidates),
                "selection_priority": priority}
    allocation, final = projected
    diagnostics = violations(final)
    if abs(sum(allocation.values()) - 1.0) > 1e-9:
        diagnostics.append("final weights do not sum to 100%")
    return {
        "policy_allocation": {rid: round(final["role_w"].get(rid, 0.0), 4) for rid in roles},
        "instrument_allocation": {code: round(weight, 4) for code, weight in allocation.items()},
        "metrics": {
            "expected_etf_return": round(final["exp"], 4),
            "expected_etf_return_conservative": round(final["cons_exp"], 4),
            "whole_portfolio_stress": round(final["whole_stress"], 4),
            "worst_scenario": final["worst_scenario"],
            "satellite_total": round(final["sat"], 4),
            "growth_factor_total": round(final["growth"], 4),
            "country_equity": {key: round(value, 4) for key, value in final["country_eq"].items()},
            "currency_exposure": {key: round(value, 4) for key, value in final["currency"].items()},
            "risk_currency_exposure": {key: round(value, 4) for key, value in final["risk_currency"].items()},
            "effective_risk_sources": (final["risk"] or {}).get("effective_bets"),
            "product_quality_penalty": round(final["quality_penalty"], 4),
            "target_return": round(target_return, 4),
            "target_gap": round(max(0.0, target_return - final["exp"]), 4),
            "target_gap_conservative": round(max(0.0, target_return - final["cons_exp"]), 4),
        },
        "validation_status": "passed" if not diagnostics else "violated",
        "constraint_diagnostics": diagnostics,
        "selected_instruments": selected,
        "selection_diagnostics": selection_diags,
        "candidates_evaluated": len(candidates),
        "feasible_count": len(feasible_candidates),
        "selection_priority": priority,
    }


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
    """Ledoit-Wolf 式收缩协方差：向"恒定相关"目标收缩，避免短样本矩阵不稳定（§9.2）。

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
    return {"labels": labels, "matrix": M, "obs": n, "avg_corr": round(rbar, 4), "shrink": d}


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
