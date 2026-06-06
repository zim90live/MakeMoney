#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────
# 【双向战术资产配置 / TACTICAL ASSET ALLOCATION — 纯函数层】
#   权威规格：仓库根 TACTICAL_ALLOCATION_DESIGN.md（本文件是其唯一实现，勿另写一份）。
#   Phase A：只做纯计算（评分 / 状态机 / 组合构建），**不接入 actionable_rebalance、不产生可执行交易**。
#   所有函数都是确定性纯函数：同输入→逐字段同输出（§13.5 可复现要求）。
#
#   三个被 §15.1 前置门槛要求冻结的权威函数：
#     score_asset()                 §4.11 权威有序打分流水线（含手算冻结向量）
#     construct_tactical_portfolio() §7.6 权威有序组合构建（单向收缩→无需不动点 + 守恒不变量 + 失败回退）
#     next_tactical_state()          §5 状态机与迟滞（§5.1 读取/持久化契约由调用方负责）
# ─────────────────────────────────────────────────────────────────────────
"""战术配置纯函数：把市场状态转成有界、可解释、可复盘的双向倾斜。回测好 ≠ 未来赚钱。"""
import math

# ── 默认配置（strategy.yaml 的 tactical_allocation 块逐键覆盖；缺省即用这里）──
TACTICAL_DEFAULTS = {
    "enabled": False,
    "mode": "shadow",
    "reserve_asset": None,
    "signals": {
        "trend_weight_within_price": 0.55,
        "momentum_63_weight": 0.60,
        "momentum_126_weight": 0.40,
        "trend_scale": 8.0,
        "momentum_scale": 2.0,
        "deadband": 0.20,
        # 固定预算（§4.8）：价 0.70 / 估值 0.20 直接 + 0.10 交互
        "price_budget": 0.70,
        "valuation_budget": 0.20,
        "interaction_budget": 0.10,
    },
    "protection": {  # §4.9 方向保护阈值（作用于置信度前子信号，钳制 s_quality）
        "knife_threshold": -0.35,
        "chase_price_threshold": 0.50,
        "chase_valuation_threshold": -0.65,
        "chase_cap": 0.20,
        "accel_trend": -0.75,
        "accel_momentum": -0.60,
        "accel_floor": -0.55,
    },
    "confidence": {
        "cached_data_multiplier": 0.60,
        "minimum_action_confidence": 0.55,
        "valuation_min_years": 3,
        "valuation_full_years": 5,
    },
    "state_machine": {
        "enter_threshold": 0.25,
        "immediate_threshold": 0.60,
        "exit_threshold": 0.10,
        "confirmation_cycles": 2,
        "recovery_cycles": 2,
        "cooldown_cycles": 1,
    },
    "profiles": {
        "保守": {"beta_up": 0.15, "beta_down": 0.35, "active_weight_budget": 0.05},
        "平衡": {"beta_up": 0.25, "beta_down": 0.45, "active_weight_budget": 0.08},
        "进取": {"beta_up": 0.35, "beta_down": 0.55, "active_weight_budget": 0.12},
    },
    "constraints": {
        "minimum_retention_ratio": 0.40,
        "single_asset_absolute_cap": 0.30,
        "upside_band_ratio": {"保守": 0.15, "平衡": 0.25, "进取": 0.35},
        "downside_band_ratio": {"保守": 0.35, "平衡": 0.45, "进取": 0.55},
        "max_asset_stress_contribution": 0.35,
        "max_sleeve_stress_contribution": 0.45,
        "reserve_lower_bound": 0.03,    # §11 冻结 config：reserve 绝对上下界（资金缓冲带）
        "reserve_upper_bound": 0.35,
    },
    "actions": {
        "tactical_abs_threshold_pp": 1.0,
        "tactical_rel_threshold": 0.10,
        "recovery_tranches": 2,
    },
    # 按资产类别的波动率下限（§4.2），防止低波资产分数失控
    "vol_floor": {
        "bond": 0.04, "short_bond": 0.04, "cash": 0.04, "gold": 0.12,
        "equity_defensive": 0.15, "equity": 0.18, "global_equity": 0.18,
        "global_growth": 0.25, "china_growth": 0.25,
    },
    "default_vol_floor": 0.18,
}

STATES = ("neutral", "positive_watch", "positive_active",
          "negative_watch", "negative_active", "recovering")


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_tactical_config(strat):
    """tactical_allocation 配置单一来源：默认 + strategy.yaml 覆盖。缺省块即用全默认（enabled=False）。"""
    block = (strat or {}).get("tactical_allocation") or {}
    return _deep_merge(TACTICAL_DEFAULTS, block)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_tactical_config(strat):
    """轻量校验 tactical_allocation（块可选）。返回错误列表。"""
    errs = []
    block = (strat or {}).get("tactical_allocation")
    if block is None:
        return errs
    if not isinstance(block, dict):
        return ["tactical_allocation 须为映射"]
    if "mode" in block and block["mode"] not in ("shadow", "advisory"):
        errs.append("tactical_allocation.mode 须为 shadow/advisory")
    if "enabled" in block and not isinstance(block["enabled"], bool):
        errs.append("tactical_allocation.enabled 须为 true/false")
    sig = block.get("signals") or {}
    db = sig.get("deadband")
    if db is not None and not (_num(db) and 0 <= db < 1):
        errs.append("tactical_allocation.signals.deadband 须在 [0,1)")
    cons = block.get("constraints") or {}
    mrr = cons.get("minimum_retention_ratio")
    if mrr is not None and not (_num(mrr) and 0 <= mrr <= 1):
        errs.append("constraints.minimum_retention_ratio 须在 [0,1]")
    return errs


# ───────────────────────── §4：信号与评分 ─────────────────────────

def _clip(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def annualized_vol(closes, window=63):
    """近 window 日收益率的年化波动率（样本标准差×√252）。数据不足返回 None。"""
    if not closes or len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - window, len(closes))
            if closes[i - 1]]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252.0)


def price_subsignals(closes, effective_vol, cfg=None):
    """从价格序列算 (s_trend, s_mom_63, s_mom_126, availability)。tanh 平滑、按波动率标准化（§4.3/4.4）。

    需要 MA200 与 126 日回报；不足的子项 availability 记 0、子分数记 0（不重归一）。
    """
    cfg = (cfg or TACTICAL_DEFAULTS)["signals"] if "signals" in (cfg or TACTICAL_DEFAULTS) else (cfg or TACTICAL_DEFAULTS["signals"])
    n = len(closes or [])
    last = closes[-1] if n else None
    ev = effective_vol if (effective_vol and effective_vol > 0) else None
    avail = {"trend": 0, "mom_63": 0, "mom_126": 0}
    s_trend = s_mom_63 = s_mom_126 = 0.0
    if last and ev and n >= 200:
        ma200 = sum(closes[-200:]) / 200.0
        trend_z = (last / ma200 - 1.0) / (ev / math.sqrt(252.0))
        s_trend = math.tanh(trend_z / cfg["trend_scale"])
        avail["trend"] = 1
    if last and ev and n >= 64:
        r63 = last / closes[-64] - 1.0
        mom63_z = r63 / (ev * math.sqrt(63.0 / 252.0))
        s_mom_63 = math.tanh(mom63_z / cfg["momentum_scale"])
        avail["mom_63"] = 1
    if last and ev and n >= 127:
        r126 = last / closes[-127] - 1.0
        mom126_z = r126 / (ev * math.sqrt(126.0 / 252.0))
        s_mom_126 = math.tanh(mom126_z / cfg["momentum_scale"])
        avail["mom_126"] = 1
    return {"s_trend": s_trend, "s_mom_63": s_mom_63, "s_mom_126": s_mom_126, "availability": avail}


def price_coverage(availability, cfg=None):
    """§4.8：价格覆盖率 = 0.55*trend + 0.45*(0.60*m63 + 0.40*m126)。"""
    s = (cfg or TACTICAL_DEFAULTS)["signals"]
    tw = s["trend_weight_within_price"]
    return tw * availability.get("trend", 0) + (1 - tw) * (
        s["momentum_63_weight"] * availability.get("mom_63", 0)
        + s["momentum_126_weight"] * availability.get("mom_126", 0))


def score_asset(*, s_trend, s_mom_63, s_mom_126, valuation_percentile=None,
                valuation_reliability=0.0, price_cov=1.0, data_quality_multiplier=1.0, cfg=None):
    """§4.11 权威有序打分流水线。返回所有中间值 + effective_score。

    估值不可用（percentile=None 或 reliability=0）→ 其 0.20+0.10 预算自然归零、价格上限仍 0.70，
    不重归一、不二次惩罚（§4.8）。缺失绝不被当成中性。
    """
    cfg = cfg or TACTICAL_DEFAULTS
    sc, pr = cfg["signals"], cfg["protection"]
    val_ok = valuation_percentile is not None and valuation_reliability > 0
    # step2 价格合成
    s_momentum = sc["momentum_63_weight"] * s_mom_63 + sc["momentum_126_weight"] * s_mom_126
    s_price = sc["trend_weight_within_price"] * s_trend + (1 - sc["trend_weight_within_price"]) * s_momentum
    # step3 估值
    s_valuation = (1 - 2 * valuation_percentile) * valuation_reliability if val_ok else 0.0
    # step4 交互（依赖估值可用）
    if val_ok:
        s_interaction = max(s_price, 0) * max(s_valuation, 0) - max(-s_price, 0) * max(-s_valuation, 0)
    else:
        s_interaction = 0.0
    # step5 固定预算合成，不重归一
    s_raw = sc["price_budget"] * s_price + (
        sc["valuation_budget"] * s_valuation + sc["interaction_budget"] * s_interaction if val_ok else 0.0)
    # step6 数据质量缩放（只乘一次） + 覆盖率/置信度
    s_quality = s_raw * data_quality_multiplier
    coverage = sc["price_budget"] * price_cov + (1 - sc["price_budget"]) * valuation_reliability
    confidence = coverage * data_quality_multiplier
    # step7 方向保护：条件用置信度前子信号，钳制 s_quality
    s_guarded = s_quality
    guards = []
    if s_price <= pr["knife_threshold"]:
        s_guarded = min(s_guarded, 0.0)
        guards.append("下跌接刀保护")
    if s_price >= pr["chase_price_threshold"] and val_ok and s_valuation <= pr["chase_valuation_threshold"]:
        s_guarded = min(s_guarded, pr["chase_cap"])
        guards.append("强趋势追高限制")
    if price_cov == 1 and data_quality_multiplier == 1 and s_trend <= pr["accel_trend"] and s_momentum <= pr["accel_momentum"]:
        s_guarded = min(s_guarded, pr["accel_floor"])
        guards.append("极端风险加速")
    # step8 死区映射
    db = sc["deadband"]
    if abs(s_guarded) < db:
        effective = 0.0
    else:
        effective = (1.0 if s_guarded > 0 else -1.0) * (abs(s_guarded) - db) / (1.0 - db)
    return {
        "s_trend": s_trend, "s_mom_63": s_mom_63, "s_mom_126": s_mom_126,
        "s_momentum": s_momentum, "s_price": s_price, "s_valuation": s_valuation,
        "s_interaction": s_interaction, "s_raw": s_raw, "s_quality": s_quality,
        "coverage": coverage, "confidence": confidence, "valuation_available": val_ok,
        "guards": guards, "effective_score": effective,
    }


# ───────────────────────── §6：分数 → 单资产战术目标 ─────────────────────────

def raw_tactical_weight(strategic_weight, effective_score, beta_up, beta_down):
    """§6.2：raw_tilt = strategic * beta * score；beta 按方向取。返回未约束战术权重。"""
    beta = beta_up if effective_score > 0 else beta_down
    return strategic_weight * (1.0 + beta * effective_score)


def asset_bounds(strategic_weight, upside_band_ratio, downside_band_ratio,
                 minimum_retention_ratio, single_asset_absolute_cap):
    """§6.3：相对带宽→绝对上下界。"""
    upper = min(strategic_weight + strategic_weight * upside_band_ratio, single_asset_absolute_cap)
    lower = max(strategic_weight * minimum_retention_ratio, strategic_weight - strategic_weight * downside_band_ratio)
    return lower, upper


def bounded_tactical_weight(strategic_weight, effective_score, profile_cfg, constraints_cfg, profile):
    """组合 raw_tactical_weight + asset_bounds，返回带宽内的单资产战术目标。"""
    raw = raw_tactical_weight(strategic_weight, effective_score,
                              profile_cfg["beta_up"], profile_cfg["beta_down"])
    lo, hi = asset_bounds(
        strategic_weight,
        constraints_cfg["upside_band_ratio"][profile],
        constraints_cfg["downside_band_ratio"][profile],
        constraints_cfg["minimum_retention_ratio"],
        constraints_cfg["single_asset_absolute_cap"])
    return _clip(raw, lo, hi)


# ───────────────────────── §5：状态机与迟滞 ─────────────────────────

def new_state(state="neutral"):
    return {"state": state, "direction": 0, "consecutive_enter_count": 0,
            "consecutive_recovery_count": 0, "cooldown_remaining_cycles": 0,
            "last_effective_score": 0.0, "transition_reason": ""}


def next_tactical_state(prev, score, cfg=None):
    """§5：根据本周期 effective_score 推进每只 ETF 的战术状态（纯函数）。

    prev 为上一【正式】周期的状态（读取契约见 §5.1，由调用方负责）。返回新状态 dict。
    score 即 §4.11 的 effective_score（死区内为 0）。
    """
    cfg = (cfg or TACTICAL_DEFAULTS)["state_machine"]
    enter, imm, exitt = cfg["enter_threshold"], cfg["immediate_threshold"], cfg["exit_threshold"]
    conf, rec = cfg["confirmation_cycles"], cfg["recovery_cycles"]
    p = prev or new_state()
    st = p.get("state", "neutral")
    ec = int(p.get("consecutive_enter_count", 0) or 0)
    rc = int(p.get("consecutive_recovery_count", 0) or 0)
    cd = max(0, int(p.get("cooldown_remaining_cycles", 0) or 0) - 1)   # 每周期递减
    out = {"direction": p.get("direction", 0), "consecutive_enter_count": ec,
           "consecutive_recovery_count": rc, "cooldown_remaining_cycles": cd,
           "last_effective_score": round(score, 6)}
    in_db = (score == 0)
    wpos, wneg = score >= enter, score <= -enter
    spos, sneg = score >= imm, score <= -imm

    def to(state, reason, **kw):
        out.update({"state": state, "transition_reason": reason})
        out["consecutive_enter_count"] = kw.get("ec", 0)
        out["consecutive_recovery_count"] = kw.get("rc", 0)
        if "direction" in kw:
            out["direction"] = kw["direction"]
        return out

    if st == "neutral":
        if wpos:
            return to("positive_watch", "score≥进入阈值，进入正向观察", ec=1, direction=1)
        if wneg:
            return to("negative_watch", "score≤负进入阈值，进入负向观察", ec=1, direction=-1)
        return to("neutral", "维持中性")
    if st == "positive_watch":
        if spos:
            return to("positive_active", "单周期极强→正向激活", direction=1)
        if wpos:
            return to("positive_active", "连续确认→正向激活", direction=1) if ec + 1 >= conf \
                else to("positive_watch", "正向观察确认中", ec=ec + 1, direction=1)
        return to("neutral", "正向信号消退→中性")
    if st == "negative_watch":
        if sneg:
            return to("negative_active", "单周期极弱→负向激活", direction=-1)
        if wneg:
            return to("negative_active", "连续确认→负向激活", direction=-1) if ec + 1 >= conf \
                else to("negative_watch", "负向观察确认中", ec=ec + 1, direction=-1)
        return to("neutral", "负向信号消退→中性")
    if st == "positive_active":
        if sneg:                                   # 极端反转（跨 ±0.60）可直转，免经 recovering
            return to("negative_active", "极端反转→负向激活", direction=-1)
        if score < exitt:
            return to("recovering", "正向信号回落→恢复", direction=1)
        return to("positive_active", "维持正向激活", direction=1)
    if st == "negative_active":
        if spos:
            return to("positive_active", "极端反转→正向激活", direction=1)
        if score > -exitt:
            return to("recovering", "负向信号回升→恢复", direction=-1)
        return to("negative_active", "维持负向激活", direction=-1)
    if st == "recovering":
        if spos:
            return to("positive_active", "恢复中再次极强→正向激活", direction=1)
        if sneg:
            return to("negative_active", "恢复中再次极弱→负向激活", direction=-1)
        if in_db:
            return to("neutral", "连续死区→回归中性") if rc + 1 >= rec \
                else to("recovering", "恢复中（死区计数）", rc=rc + 1, direction=p.get("direction", 0))
        if wpos:
            return to("positive_active", "恢复中重新连续确认→正向激活", direction=1) if ec + 1 >= conf \
                else to("recovering", "恢复中正向确认", ec=ec + 1, direction=1)
        if wneg:
            return to("negative_active", "恢复中重新连续确认→负向激活", direction=-1) if ec + 1 >= conf \
                else to("recovering", "恢复中负向确认", ec=ec + 1, direction=-1)
        return to("recovering", "恢复中（弱信号、计数重置）", direction=p.get("direction", 0))
    return to("neutral", "未知状态→重置中性")


# ───────────────────────── §7.6：权威有序组合构建 ─────────────────────────

def construct_tactical_portfolio(strategic, bounded_targets, reserve_asset,
                                 reserve_bounds=(0.0, 1.0), active_weight_budget=None,
                                 shocks=None, etf_share=1.0, max_whole_stress=None,
                                 max_asset_stress=0.35, max_sleeve_stress=0.45, asset_of=None):
    """§7.6 权威有序组合构建（v1）。所有约束只【单向收缩】风险资产倾斜，释放权重只进 reserve/现金，
    决不重新分配给其它风险资产——因此后置约束不会破坏前置约束，无需不动点迭代。

    入参：
      strategic         {code: w}（含 reserve，合计应为 1）
      bounded_targets   {code: w}（各风险资产经带宽钳制后的目标，§6.3 的结果；reserve 不在此）
      reserve_asset     债券 reserve 的 code
      reserve_bounds    (lower, upper)
      active_weight_budget  战术偏离预算（§7.3），None=不约束
      shocks            {code: shock(负)}，配合 max_whole_stress 做 §7.5 全组合压力收缩；None=不约束
      etf_share, max_whole_stress  全组合口径压力闸
    返回 {weights, reserve, cash, active_weight_budget_used, whole_stress, ok, fallback, asserts}。
    """
    strategic = {str(k): float(v) for k, v in (strategic or {}).items()}
    reserve_asset = str(reserve_asset)
    rlo, rhi = reserve_bounds
    risk_codes = [c for c in strategic if c != reserve_asset]
    strat_reserve = strategic.get(reserve_asset, 0.0)

    def settle(risk_targets):
        sum_risk = sum(risk_targets.values())
        desired_reserve = 1.0 - sum_risk
        reserve = _clip(desired_reserve, rlo, rhi)
        cash = max(1.0 - sum_risk - reserve, 0.0)
        return reserve, cash

    # step1-3：带宽后释放/需求 → 确定性同比缩放正向需求 → 结算 reserve/现金
    alloc, release, demand = {}, 0.0, {}
    for c in risk_codes:
        sw = strategic[c]
        tgt = float(bounded_targets.get(c, sw))
        if tgt < sw:
            alloc[c] = tgt
            release += sw - tgt
        elif tgt > sw:
            demand[c] = tgt - sw
            alloc[c] = sw          # 先放回战略，稍后按可用资金加回
        else:
            alloc[c] = sw
    available = release + max(strat_reserve - rlo, 0.0)
    total_demand = sum(demand.values())
    pos_scale = min(1.0, available / total_demand) if total_demand > 1e-12 else 1.0
    for c, d in demand.items():
        alloc[c] = strategic[c] + d * pos_scale

    # step4：战术偏离预算（含目标现金）。超限→对所有风险倾斜同比缩放（单调二分）。
    def scale_all(s):
        return {c: strategic[c] + (alloc[c] - strategic[c]) * s for c in risk_codes}

    def budget_used(risk_targets):
        reserve, cash = settle(risk_targets)
        dev = sum(abs(risk_targets[c] - strategic[c]) for c in risk_codes)
        dev += abs(reserve - strat_reserve) + abs(cash - 0.0)
        return dev / 2.0, reserve, cash

    if active_weight_budget is not None:
        used, _, _ = budget_used(alloc)
        if used > active_weight_budget + 1e-9:
            s = _bisect_max_scale(lambda s: budget_used(scale_all(s))[0] <= active_weight_budget + 1e-9)
            alloc = scale_all(s)

    # step5：单资产 + sleeve 压力集中度（v1 用压力冲击；只收缩"增压"倾斜、降仓不动）。
    # 上限 = max(配置比例 × 战略总压力, 战略自身贡献)——战略已超限时不恶化、也不强行修复战略。
    if shocks:
        strat_total = _total_stress(strategic, shocks)
        for c in sorted(risk_codes):                       # 单资产
            sw, sh = strategic[c], abs(shocks.get(c, 0))
            if alloc[c] > sw and sh > 1e-12:
                cap = max(max_asset_stress * strat_total, abs(sw * shocks.get(c, 0)))
                if abs(alloc[c] * shocks.get(c, 0)) > cap + 1e-9:
                    alloc[c] = max(sw, cap / sh)
        if asset_of:                                       # sleeve（同 asset 一组）
            sleeves = {}
            for c in risk_codes:
                sleeves.setdefault(asset_of.get(c), []).append(c)
            for sleeve in sorted(sleeves, key=lambda x: str(x)):
                members = sleeves[sleeve]
                cap = max(max_sleeve_stress * strat_total,
                          sum(abs(strategic[m] * shocks.get(m, 0)) for m in members))
                contrib = sum(abs(alloc[m] * shocks.get(m, 0)) for m in members)
                if contrib > cap + 1e-9:
                    up = [m for m in members if alloc[m] > strategic[m] and abs(shocks.get(m, 0)) > 1e-12]
                    fixed = sum(abs(alloc[m] * shocks.get(m, 0)) for m in members if m not in up)   # 非增压成员(不动)
                    up_strat = sum(strategic[m] * abs(shocks.get(m, 0)) for m in up)                 # 增压成员的战略基线(不缩)
                    up_tilt = sum((alloc[m] - strategic[m]) * abs(shocks.get(m, 0)) for m in up)      # 只有这部分可缩
                    if up_tilt > 1e-12:
                        scale = min(1.0, max(0.0, cap - fixed - up_strat) / up_tilt)
                        for m in up:
                            alloc[m] = strategic[m] + (alloc[m] - strategic[m]) * scale

    # step6：全组合压力回撤闸——只对增压正向倾斜同比缩放（单调二分）。
    whole_stress = None
    if shocks and max_whole_stress is not None:
        def whole_after(s):
            rt = {c: (strategic[c] + (alloc[c] - strategic[c]) * (s if alloc[c] > strategic[c] else 1.0))
                  for c in risk_codes}
            reserve, _ = settle(rt)
            etf = sum(rt[c] * abs(shocks.get(c, 0)) for c in risk_codes) + reserve * abs(shocks.get(reserve_asset, 0))
            return etf_share * etf, rt
        ws, _ = whole_after(1.0)
        if ws > max_whole_stress + 1e-9:
            s = _bisect_max_scale(lambda s: whole_after(s)[0] <= max_whole_stress + 1e-9)
            _, alloc = whole_after(s)
        whole_stress, _ = whole_after(1.0)

    # step7：结算
    reserve, cash = settle(alloc)
    weights = dict(alloc)
    weights[reserve_asset] = reserve
    used, _, _ = budget_used(alloc)
    if whole_stress is None and shocks:
        whole_stress = etf_share * (sum(alloc[c] * abs(shocks.get(c, 0)) for c in risk_codes)
                                    + reserve * abs(shocks.get(reserve_asset, 0)))

    # step8：守恒 + 约束断言；失败→回退战略组合（合法失败结果）
    checks = []
    total = sum(weights.values()) + cash
    checks.append(("conservation_sum_1", abs(total - 1.0) <= 1e-9))
    checks.append(("non_negative", all(w >= -1e-9 for w in weights.values()) and cash >= -1e-9))
    checks.append(("reserve_in_bounds", rlo - 1e-9 <= reserve <= rhi + 1e-9))
    if active_weight_budget is not None:
        checks.append(("budget", used <= active_weight_budget + 1e-9))
    if shocks:                                              # §7.6 集中度不变量（容差 1e-6）
        st = _total_stress(strategic, shocks)
        checks.append(("asset_stress_concentration", all(
            abs(weights.get(c, 0) * shocks.get(c, 0)) <= max(max_asset_stress * st, abs(strategic.get(c, 0) * shocks.get(c, 0))) + 1e-6
            for c in risk_codes)))
        if asset_of:
            grp = {}
            for c in risk_codes:
                grp.setdefault(asset_of.get(c), []).append(c)
            checks.append(("sleeve_stress_concentration", all(
                sum(abs(weights.get(m, 0) * shocks.get(m, 0)) for m in ms)
                <= max(max_sleeve_stress * st, sum(abs(strategic[m] * shocks.get(m, 0)) for m in ms)) + 1e-6
                for ms in grp.values())))
    ok = all(v for _, v in checks)
    if not ok:
        return {"weights": dict(strategic), "reserve": strat_reserve, "cash": 0.0,
                "active_weight_budget_used": 0.0, "whole_stress": whole_stress,
                "ok": False, "fallback": True,
                "asserts": [{"name": n, "pass": v} for n, v in checks]}
    return {"weights": {c: round(weights[c], 6) for c in weights}, "reserve": round(reserve, 6),
            "cash": round(cash, 6), "active_weight_budget_used": round(used, 6),
            "whole_stress": (round(whole_stress, 6) if whole_stress is not None else None),
            "ok": True, "fallback": False,
            "asserts": [{"name": n, "pass": v} for n, v in checks]}


def _total_stress(weights, shocks):
    return sum(abs(float(w) * shocks.get(c, 0)) for c, w in weights.items())


def _bisect_max_scale(feasible):
    """在 [0,1] 找满足 feasible 的最大缩放系数（feasible(0)=True、单调）。固定 40 次二分、返回保守侧。"""
    if feasible(1.0):
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if feasible(mid):
            lo = mid
        else:
            hi = mid
    return lo


# ────────────── 影子产出编排（Phase A：只读、绝不产生可执行交易）──────────────

def tactical_actions(shadow, current_weights, total_value, *, min_trade=0.0, max_weekly=0.0,
                     abs_thr_pp=1.0, rel_thr=0.10, struct_abs_pp=5.0, struct_rel=0.25,
                     strategic_weights=None):
    """从影子诊断生成【净战术动作】(current_weight → tactical_weight)。§8.1/§8.3：

    方向只由 current→tactical_weight 决定；触发 = **结构 5/25(向 tactical_weight)** 或 **(状态 active 且过战术门槛)**。
    当状态中性时 tactical_weight≈strategic，结构 5/25 即承接普通再平衡（不被状态机门控吞掉）；
    状态 active 时 tactical_weight 带倾斜，战术门槛更敏感地触发小幅调整。

    纯函数、**始终可算**；只有调用方 `mode==advisory` 时才接入可执行——shadow 下仅展示、绝不进 actionable_rebalance。
    reserve 不产生独立战术动作（§7.2）。
    """
    diag = (shadow or {}).get("diagnostics") or {}
    strategic_weights = strategic_weights or {}
    abs_thr = abs_thr_pp / 100.0
    actions, weekly_used = [], 0.0
    for code, d in diag.items():
        if not isinstance(d, dict) or d.get("role") == "reserve":
            continue
        tac_w = d.get("tactical_weight")
        if tac_w is None:
            continue
        cur = float(current_weights.get(code, 0) or 0)
        delta = tac_w - cur
        state = d.get("state")
        sw = float(strategic_weights.get(code, 0) or 0)
        active = state in ("positive_active", "negative_active", "recovering")
        struct_trig = abs(delta) >= struct_abs_pp / 100.0 or (sw > 0 and abs(delta) / sw >= struct_rel)
        tac_trig = active and (abs(delta) >= abs_thr or (sw > 0 and abs(delta) / sw >= rel_thr))
        triggered = struct_trig or tac_trig
        trigger = "structural" if struct_trig else ("tactical" if tac_trig else None)
        side = "add" if delta > 0 else "trim"
        amount = round(abs(delta) * float(total_value or 0), 0)
        reasons = []
        if not triggered:
            reasons.append(f"未达结构 5/25 或战术门槛（{abs_thr_pp:.1f}pp 且 active）")
        if amount < min_trade:
            reasons.append(f"金额低于最小交易门槛 {min_trade:.0f} 元")
        if max_weekly > 0 and side == "add" and weekly_used + amount > max_weekly:
            reasons.append(f"超过单周交易上限 {max_weekly:.0f} 元")
        actionable = triggered and not reasons
        if actionable and side == "add":
            weekly_used += amount
        actions.append({"code": code, "source": "tactical", "side": side, "trigger": trigger,
                        "current_weight": round(cur, 4), "tactical_weight": round(tac_w, 4),
                        "deviation_pp": round(delta * 100, 2), "approx_amount": amount,
                        "state": state, "actionable": bool(actionable), "blocked_reasons": reasons})
    return actions


def reliability_from_valuation_status(status):
    """估值可靠度（§4.6 轻量版）：live 长历史→0.85，cache→0.6，缺失/不适用→0。
    历史长度精确分级（<3y=0 / 3-5y≤0.5 / ≥5y=1）待 PE 序列长度可得后细化（Phase D）。"""
    if not status or not status.get("available"):
        return 0.0
    src = status.get("source")
    return 0.85 if src == "live" else (0.6 if src == "cache" else 0.0)


def data_quality_multiplier_from_prov(prov, stale_limit=10, cached_mult=0.60):
    """单资产数据质量乘子（§4.8，按该 ETF 自身 provenance，不用组合级 grade_data）。"""
    if not prov:
        return 0.0
    if prov.get("stale_days", 0) > stale_limit:
        return 0.0
    src = prov.get("source")
    if src == "cache":
        return cached_mult
    return 1.0 if src in ("westock", "live") else 0.0


def _subsignals_and_cov(a, cfg):
    if a.get("subsignals"):
        sub = a["subsignals"]
        avail = sub.get("availability", {"trend": 1, "mom_63": 1, "mom_126": 1})
        return sub, a.get("price_cov", price_coverage(avail, cfg))
    closes = a.get("closes") or []
    vol = annualized_vol(closes, 63)
    floor = cfg["vol_floor"].get(a.get("asset"), cfg["default_vol_floor"])
    ev = max(vol, floor) if vol else floor
    sub = price_subsignals(closes, ev, cfg)
    return sub, price_coverage(sub["availability"], cfg)


def compute_shadow(assets, profile, reserve_asset, *, etf_share=1.0, max_whole_stress=None,
                   cfg=None, prior_states=None, gate_by_state=True):
    """Phase A 影子编排：每只资产 子信号→score_asset→next_tactical_state→带宽目标，再 construct_tactical_portfolio。

    assets 每项：{code, asset, strategic_weight, (subsignals|closes), valuation_percentile,
                  valuation_status|valuation_reliability, provenance|data_quality_multiplier, shock}。
    返回 {mode, profile, diagnostics:{code:{...}}, weights, reserve, cash, active_weight_budget_used,
          whole_stress, ok, fallback}。**只读、不产生可执行交易。**
    """
    cfg = cfg or TACTICAL_DEFAULTS
    prior_states = prior_states or {}
    prof_cfg = cfg["profiles"].get(profile) or cfg["profiles"]["平衡"]
    cons = cfg["constraints"]
    strategic, bounded, shocks, diag = {}, {}, {}, {}
    for a in assets:
        code = str(a["code"])
        strategic[code] = float(a.get("strategic_weight") or 0)
        shocks[code] = float(a.get("shock") or 0)
        if code == str(reserve_asset):     # §7.2：reserve 资产不参与独立战术评分/状态机，只作资金缓冲
            diag[code] = {"strategic_weight": round(strategic[code], 6), "role": "reserve",
                          "note": "资金缓冲，不参与独立战术评分/状态机"}
            continue
        sub, pc = _subsignals_and_cov(a, cfg)
        rel = a.get("valuation_reliability")
        if rel is None:
            rel = reliability_from_valuation_status(a.get("valuation_status"))
        dq = a.get("data_quality_multiplier")
        if dq is None:
            dq = data_quality_multiplier_from_prov(a.get("provenance"),
                                                   cached_mult=cfg["confidence"]["cached_data_multiplier"])
        sc = score_asset(s_trend=sub.get("s_trend", 0.0), s_mom_63=sub.get("s_mom_63", 0.0),
                         s_mom_126=sub.get("s_mom_126", 0.0), valuation_percentile=a.get("valuation_percentile"),
                         valuation_reliability=rel, price_cov=pc, data_quality_multiplier=dq, cfg=cfg)
        st = next_tactical_state(prior_states.get(code) or new_state(), sc["effective_score"], cfg)
        if code != str(reserve_asset):
            # §8.2：只有状态机处于 active/recovering 才真正倾斜；watch/neutral 维持战略（迟滞抑制未确认信号）。
            # gate_by_state=False 用于回测的"去状态机"消融。
            tilt_ok = (not gate_by_state) or st["state"] in ("positive_active", "negative_active", "recovering")
            bounded[code] = (bounded_tactical_weight(strategic[code], sc["effective_score"], prof_cfg, cons, profile)
                             if tilt_ok else strategic[code])
        diag[code] = {
            "strategic_weight": round(strategic[code], 6), "effective_score": round(sc["effective_score"], 6),
            "state": st["state"], "state_after": st, "confidence": round(sc["confidence"], 6),
            "action_confidence_ok": sc["confidence"] >= cfg["confidence"]["minimum_action_confidence"],
            "guards": sc["guards"],
            "subscores": {k: round(sc[k], 6) for k in ("s_trend", "s_momentum", "s_price", "s_valuation", "s_interaction")},
        }
    # reserve 上下界用冻结 config（§11，绝对值），不再用未文档化的派生公式（审查 #4）。
    reserve_bounds = (cons.get("reserve_lower_bound", 0.0), cons.get("reserve_upper_bound", 0.6))
    built = construct_tactical_portfolio(
        strategic, bounded, reserve_asset, reserve_bounds=reserve_bounds,
        active_weight_budget=prof_cfg["active_weight_budget"], shocks=shocks,
        etf_share=etf_share, max_whole_stress=max_whole_stress,
        max_asset_stress=cons.get("max_asset_stress_contribution", 0.35),
        max_sleeve_stress=cons.get("max_sleeve_stress_contribution", 0.45),
        asset_of={str(a["code"]): a.get("asset") for a in assets})
    for code, w in (built.get("weights") or {}).items():
        if code in diag:
            diag[code]["tactical_weight"] = round(w, 6)
            diag[code]["tilt_pp"] = round((w - strategic.get(code, 0)) * 100, 2)
    return {"mode": cfg.get("mode"), "profile": profile, "diagnostics": diag,
            "weights": built.get("weights"), "reserve": built.get("reserve"), "cash": built.get("cash"),
            "active_weight_budget_used": built.get("active_weight_budget_used"),
            "whole_stress": built.get("whole_stress"), "ok": built.get("ok"), "fallback": built.get("fallback")}
