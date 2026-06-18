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

这是自用私人投顾工具，输出的是"信号与建议"供所有者本人决策；不承诺收益，回测好 ≠ 未来赚钱。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta


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

# akshare 的新浪日线/乐咕估值接口内嵌 mini_racer(V8) 执行 JS 签名；多线程并发首次初始化 V8
# 会触发 PartitionAlloc 双重初始化、整个进程 SIGTRAP 崩掉（无 Python 异常可捕获）。
# 取数并行（ThreadPoolExecutor）下必须用这把锁串行化所有 mini_racer 路径的调用。
_AK_JS_LOCK = threading.Lock()

import tactical  # noqa: E402  双向战术配置纯函数（Phase A 影子，只读不产生可执行交易）
import strategic  # noqa: E402  战略层纯函数（此处借用 §18 集中度上限做真实持仓体检）


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE_DIR = os.path.join(HERE, "cache")
PROGRESS_PATH = os.path.join(CACHE_DIR, "progress.json")  # 长任务进度（驾驶舱轮询展示；gitignore 区）
VALUATION_DIR = os.path.join(ROOT, "journal", "valuation")  # csindex 官方 PE 按日自建累积（sync 提交、离线复现）
STALE_LIMIT_DAYS = 10        # 行情最新日期超过此日历天数 → "过旧"，禁用交易建议
VAL_STALE_LIMIT_DAYS = 30    # 估值缓存超过此天数 → 视为不可用（估值变化慢，限额可宽些）
VALUATION_ACCUM_MIN_YEARS = 3.0   # 自建累积历史 < 此年限 → 只显示当前 PE 水平、不算分位（防噪声冒充信号）
MARKET_CLOSE_SETTLE_HOUR = 15.5   # 15:30：A股收盘留发布延迟余量，过此点才认当日定稿（缓存跳过用）
# 数据源健康账本（gitignore 区；GET /api/health/data 读）。函数而非常量：跟随 CACHE_DIR（测试会重定向它）
def fetch_health_path():
    return os.path.join(CACHE_DIR, "fetch_health.json")

# 乐咕(legulegu)估值单源的备援：指数名 → 中证官网(csindex)指数代码。
# 设计是"历史与当日点解耦"：分位所需的多年历史在每次乐咕成功时本地化（缓存里的 series），
# 乐咕挂掉时只需补"今天的 PE"这一个点——由中证官网提供（取市盈率2=滚动TTM，与乐咕"滚动市盈率"同口径），
# 分位仍按本地化历史窗口算。备援点绝不混入 series（序列保持纯乐咕，两家口径的毫厘差不污染历史）。
# 创业板50 是国证系指数、中证官网不覆盖 → 无备援（仍走乐咕+30天缓存，影响面只剩 159915 一只）。
VAL_BACKUP_CSINDEX = {"沪深300": "000300", "中证500": "000905"}


_PROGRESS_LOCK = threading.Lock()


def report_progress(stage, detail="", step=None, total=None, task="signals", done=False, error=None):
    """长任务进度上报（驾驶舱 GET /api/signals/status 轮询展示）。

    尽力而为：写盘失败绝不影响信号计算；原子替换防轮询读到半个 JSON。
    不入归档、不进 signals.json——只是给"等 1–4 分钟"的人一个阶段感。"""
    try:
        with _PROGRESS_LOCK:
            os.makedirs(CACHE_DIR, exist_ok=True)
            payload = {"task": task, "stage": stage, "detail": detail, "step": step,
                       "total": total, "done": bool(done),
                       "ts": datetime.now().isoformat(timespec="seconds")}
            if error:
                payload["error"] = str(error)[:500]
            tmp = PROGRESS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, PROGRESS_PATH)
    except Exception:  # noqa: BLE001
        pass


_HEALTH_LOCK = threading.Lock()


def record_fetch_health(source, ok, error=None):
    """数据源健康账本：每次**真实触网**后记一笔（缓存跳过=没触网=不记）。尽力而为，绝不影响取数。

    结构：{source: {last_success, last_failure, consecutive_failures, last_error}}。
    consecutive_failures 为连败计数、成功清零——驾驶舱据此亮"数据源异常"灯，
    让"某个源连挂了好几天"在周报突然缺数之前就被看见。"""
    try:
        with _HEALTH_LOCK:
            path = fetch_health_path()
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f) or {}
                except Exception:  # noqa: BLE001
                    data = {}
            ent = data.get(source) or {}
            now = datetime.now().isoformat(timespec="seconds")
            if ok:
                ent.update({"last_success": now, "consecutive_failures": 0, "last_error": None})
            else:
                ent.update({"last_failure": now,
                            "consecutive_failures": int(ent.get("consecutive_failures") or 0) + 1,
                            "last_error": (str(error)[:200] if error is not None else None)})
            data[source] = ent
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _latest_completed_session(now=None):
    """最近一个『已收盘』交易日（缓存跳过的基准）。规则：工作日 ≥15:30 → 今天；否则回退到上一工作日。

    **故意不识别节假日**：节假日当天会被当成工作日 → 缓存(节前最后交易日)< 它 → 触发一次拉取（慢但绝不返回过期数据）；
    而绝不会把更老的日子当成『最新』(只在工作日之间判定、从不跳过工作日) → 永不把过期缓存当定稿。失败方向永远偏『拉』。"""
    now = now or datetime.now()
    d = now.date()
    if d.weekday() < 5 and (now.hour + now.minute / 60.0) >= MARKET_CLOSE_SETTLE_HOUR:
        return d
    d -= timedelta(days=1)
    while d.weekday() >= 5:               # 跳过周六(5)/周日(6)
        d -= timedelta(days=1)
    return d


def _cache_latest_date(code):
    """本地日线缓存的最新日期（无缓存/坏文件→None）。"""
    df = _read_cache(code)
    if df is None or df.empty:
        return None
    try:
        return df["date"].iloc[-1].date()
    except Exception:  # noqa: BLE001
        return None


def _is_cache_current(code, latest_session):
    """缓存日线是否已达最近已收盘交易日（达到 → 数据定稿、可跳过网络）。"""
    last = _cache_latest_date(code)
    return last is not None and latest_session is not None and last >= latest_session

DEFAULT_INVESTOR_PROFILE = {
    "target_annual_return": 0.05,
    "horizon_years": 5,
    "max_acceptable_drawdown": 0.15,
    "experience_level": "beginner",
    "emergency_cash_kept_outside": 0,
    "monthly_contribution": 0,
    "total_assets": 0,
    "stable_assets_outside": 0,     # 场外稳健桶（活期/固收/定存）：让算法知道有这笔缓冲，做全组合口径
    "stable_assets_yield": 0.025,   # 稳健桶假设年化（仅用于混合收益展示）
    "planned_etf_capital": 0,       # ETF 风险桶目标上限：用于缓冲比例与目标权重测算（0=不启用缓冲，按 ETF 桶自身回撤预算）
    "unemployment_monthly_expense": 6000,
    "unemployment_minimum_monthly_income": 0,
    "unemployment_runway_years": 5,
    "post_stress_reserve_months": 12,
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


def load_config_yaml(path, label):
    """读取并解析用户 YAML 配置；解析失败时给非程序员看得懂的「哪个文件第几行」提示，而非原始堆栈（U3-2）。

    用于 strategy.yaml / portfolio.yaml 这类需手改的文件——错一个缩进/括号不该抛 PyYAML scanner 栈。
    """
    try:
        return load_yaml(path)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"（第 {mark.line + 1} 行第 {mark.column + 1} 列附近）" if mark is not None else ""
        problem = getattr(e, "problem", None) or "格式错误"
        die(f"{label} 解析失败{where}：{problem}。\n"
            f"  常见原因：缩进用了 Tab、漏了引号/冒号/括号、或中英文标点混用。请对照 examples/ 下的示例修正后重试。")


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
    if "check_frequency" in rb and str(rb.get("check_frequency")).lower() not in ("weekly", "biweekly", "monthly", "quarterly"):
        errs.append("rebalance.check_frequency 须为 weekly/biweekly/monthly/quarterly")
    if "circuit_breaker_pp" in rb and not (_num_ok(rb.get("circuit_breaker_pp"), positive=True)
                                           and float(rb.get("circuit_breaker_pp")) > float(rb.get("abs_threshold_pp", 0) or 0)):
        errs.append("rebalance.circuit_breaker_pp 须为正数且大于 abs_threshold_pp")
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
    asm = strat.get("assumptions")
    if asm is not None:
        if not isinstance(asm, dict):
            errs.append("assumptions 须为映射")
        else:
            d = asm.get("defaults") or {}
            if "shock" in d and not _num_ok(d.get("shock"), lo=-1, hi=0):
                errs.append("assumptions.defaults.shock 越界（应在 [-1,0]）")
            if "expected_return" in d and not _num_ok(d.get("expected_return"), lo=-1, hi=1):
                errs.append("assumptions.defaults.expected_return 越界（应在 [-1,1]）")
            if "return_haircut" in d and not _num_ok(d.get("return_haircut"), lo=0, hi=0.15):
                errs.append("assumptions.defaults.return_haircut 越界（应在 [0,0.15]）")   # 批3：§9.1 边界校验
            sl = asm.get("sleeves") or {}
            if not isinstance(sl, dict):
                errs.append("assumptions.sleeves 须为映射")
            else:
                for asset, cfg in sl.items():
                    cfg = cfg or {}
                    if "shock" in cfg and not _num_ok(cfg.get("shock"), lo=-1, hi=0):
                        errs.append(f"assumptions.sleeves.{asset}.shock 越界（应在 [-1,0]）")
                    if "expected_return" in cfg and not _num_ok(cfg.get("expected_return"), lo=-1, hi=1):
                        errs.append(f"assumptions.sleeves.{asset}.expected_return 越界（应在 [-1,1]）")
                    for rk in ("return_conservative", "return_optimistic"):   # 批3：显式 per-sleeve 区间边界
                        if rk in cfg and not _num_ok(cfg.get(rk), lo=-1, hi=1):
                            errs.append(f"assumptions.sleeves.{asset}.{rk} 越界（应在 [-1,1]）")
                    rc_, ro_, ce_ = cfg.get("return_conservative"), cfg.get("return_optimistic"), cfg.get("expected_return")
                    if _num_ok(rc_) and _num_ok(ro_) and rc_ > ro_:           # 批3：断言 conservative ≤ optimistic
                        errs.append(f"assumptions.sleeves.{asset}：return_conservative 不得高于 return_optimistic")
                    if _num_ok(rc_) and _num_ok(ce_) and rc_ > ce_:           # conservative ≤ central
                        errs.append(f"assumptions.sleeves.{asset}：return_conservative 不得高于 expected_return")
                    if _num_ok(ro_) and _num_ok(ce_) and ro_ < ce_:           # central ≤ optimistic
                        errs.append(f"assumptions.sleeves.{asset}：return_optimistic 不得低于 expected_return")
                    for sk in ("source", "note"):
                        if sk in cfg and not isinstance(cfg.get(sk), str):
                            errs.append(f"assumptions.sleeves.{asset}.{sk} 须为字符串")
    # L1（2026-06-10 审查）：expected_return（积木式预期收益）配置块校验——此前零校验，
    # `valuation_reversion_years: "十"` 之类会裸栈崩溃，违背友好报错约定。
    er = strat.get("expected_return")
    if er is not None:
        if not isinstance(er, dict):
            errs.append("expected_return 须为映射（bond_ytm_tenor / valuation_reversion_years / ...）")
        else:
            if "valuation_reversion_years" in er and not _num_ok(er.get("valuation_reversion_years"), lo=1, hi=50):
                errs.append("expected_return.valuation_reversion_years 须为 1~50 的数字（年）")
            if "valuation_adj_cap" in er and not _num_ok(er.get("valuation_adj_cap"), lo=0, hi=1):
                errs.append("expected_return.valuation_adj_cap 须为 0~1 的数字")
            if "ytm_conservative_haircut" in er and not _num_ok(er.get("ytm_conservative_haircut"), lo=0, hi=1):
                errs.append("expected_return.ytm_conservative_haircut 须为 0~1 的数字")
            _cn_tenors = ("3月", "6月", "1年", "3年", "5年", "7年", "10年", "30年")
            if "bond_ytm_tenor" in er and str(er.get("bond_ytm_tenor")) not in _cn_tenors:
                errs.append("expected_return.bond_ytm_tenor 须为 " + "/".join(_cn_tenors) + " 之一")
            _us_tenors = ("2年", "5年", "10年", "30年")
            if "us_ytm_tenor" in er and str(er.get("us_ytm_tenor")) not in _us_tenors:
                errs.append("expected_return.us_ytm_tenor 须为 " + "/".join(_us_tenors) + " 之一")
            erp = er.get("equity_risk_premium")
            if "equity_risk_premium" in er and not isinstance(erp, dict):
                errs.append("expected_return.equity_risk_premium 须为映射 {sleeve: 数值}")
            elif isinstance(erp, dict):
                for k, v in erp.items():
                    if not _num_ok(v, lo=-1, hi=1):
                        errs.append(f"expected_return.equity_risk_premium.{k} 越界（应在 [-1,1]）")
    # ARCH-03：长期政策书 / 战术配置 schema 校验——malformed 配置（区间 lo>hi、上限>1 等）原本会一路滑到
    #   construct 深处才表现为误导性的 no_feasible 或静默错配；在加载时就拦住、给清楚病因。
    sp = strat.get("strategic_policy")
    if sp is not None:
        errs.extend(_validate_strategic_policy(sp, set(codes)))
    ta = strat.get("tactical_allocation")
    if ta is not None:
        errs.extend(_validate_tactical_allocation(ta))
    return errs


def _validate_strategic_policy(sp, codes):
    """校验 strategic_policy（§18）：角色区间 [lo,hi]⊂[0,1] 且 lo≤hi、成员∈universe、tier 合法、
    上限∈[0,1]、policy_version 正整数、construct_stress_budget=null 或 (0,1]。返回 errs 列表。"""
    if not isinstance(sp, dict):
        return ["strategic_policy 须为映射"]
    errs = []
    pv = sp.get("policy_version")
    if pv is not None and not (isinstance(pv, int) and not isinstance(pv, bool) and pv > 0):
        errs.append("strategic_policy.policy_version 须为正整数")
    roles = sp.get("roles")
    if roles is not None and not isinstance(roles, dict):
        errs.append("strategic_policy.roles 须为映射")
    elif isinstance(roles, dict):
        for rid, r in roles.items():
            r = r or {}
            rng = r.get("range")
            if not (isinstance(rng, (list, tuple)) and len(rng) == 2 and _num_ok(rng[0], lo=0, hi=1)
                    and _num_ok(rng[1], lo=0, hi=1) and float(rng[0]) <= float(rng[1])):
                errs.append(f"strategic_policy.roles.{rid}.range 须为 [lo,hi] 且 0≤lo≤hi≤1")
            members = r.get("members")
            if not isinstance(members, list) or not members:
                errs.append(f"strategic_policy.roles.{rid}.members 须为非空列表")
            else:
                unknown = [str(c) for c in members if str(c) not in codes]
                if unknown:
                    errs.append(f"strategic_policy.roles.{rid}.members 含 universe 外代码：{', '.join(unknown)}")
            tier = r.get("tier")
            if tier is not None and tier not in ("core", "core_defensive", "diversifier", "satellite"):
                errs.append(f"strategic_policy.roles.{rid}.tier 须为 core/core_defensive/diversifier/satellite")
    caps = sp.get("caps")
    if caps is not None and not isinstance(caps, dict):
        errs.append("strategic_policy.caps 须为映射")
    elif isinstance(caps, dict):
        for ck in ("non_satellite_min", "satellite_max", "single_satellite_max", "growth_factor_max",
                   "single_country_equity_max", "single_risk_currency_exposure_max", "single_currency_exposure_max",
                   "min_covariance_coverage"):
            if ck in caps and not _num_ok(caps.get(ck), lo=0, hi=1):
                errs.append(f"strategic_policy.caps.{ck} 须在 [0,1]")
        if "enforce_cov_stress" in caps and not isinstance(caps.get("enforce_cov_stress"), bool):
            errs.append("strategic_policy.caps.enforce_cov_stress 须为 true/false")
        if "cov_stress_z" in caps and not _num_ok(caps.get("cov_stress_z"), lo=0, hi=10):
            errs.append("strategic_policy.caps.cov_stress_z 须在 [0,10]")
        if "min_effective_bets" in caps and not _num_ok(caps.get("min_effective_bets"), lo=1, hi=100):
            errs.append("strategic_policy.caps.min_effective_bets 须在 [1,100]")
    if sp.get("selection_priority", "return_first") not in ("return_first", "balanced", "defensive_first"):
        errs.append("strategic_policy.selection_priority 须为 return_first/balanced/defensive_first")
    if sp.get("target_return_basis", "etf_bucket") not in ("etf_bucket", "whole_portfolio"):
        errs.append("strategic_policy.target_return_basis 须为 etf_bucket/whole_portfolio")
    if "target_return_hard_gate" in sp and not isinstance(sp.get("target_return_hard_gate"), bool):
        errs.append("strategic_policy.target_return_hard_gate 须为 true/false")
    if "show_whole_portfolio_return" in sp and not isinstance(sp.get("show_whole_portfolio_return"), bool):
        errs.append("strategic_policy.show_whole_portfolio_return 须为 true/false")
    stable_cfg = sp.get("stable_assets")
    if stable_cfg is not None:
        if not isinstance(stable_cfg, dict):
            errs.append("strategic_policy.stable_assets 须为映射")
        elif "expected_return" in stable_cfg and not _num_ok(stable_cfg.get("expected_return"), lo=-1, hi=1):
            errs.append("strategic_policy.stable_assets.expected_return 须在 [-1,1]")
    csb = sp.get("construct_stress_budget")
    if csb is not None and not (_num_ok(csb, lo=0, hi=1) and float(csb) > 0):
        errs.append("strategic_policy.construct_stress_budget 须为 null 或 (0,1]")
    csm = sp.get("construct_stress_margin")
    if csm is not None and not _num_ok(csm, lo=0, hi=0.80):
        errs.append("strategic_policy.construct_stress_margin 须为 null 或 [0,0.8]（预算=可接受回撤−margin，自动联动）")
    return errs


def _validate_tactical_allocation(ta):
    """校验 tactical_allocation：enabled 为 bool、mode ∈ {shadow, advisory}。返回 errs 列表。"""
    if not isinstance(ta, dict):
        return ["tactical_allocation 须为映射"]
    errs = []
    if "enabled" in ta and not isinstance(ta.get("enabled"), bool):
        errs.append("tactical_allocation.enabled 须为 true/false")
    if "mode" in ta and str(ta.get("mode")) not in ("shadow", "advisory"):
        errs.append("tactical_allocation.mode 须为 shadow/advisory")
    return errs


# ---------- 行情多源取数 + 缓存 ----------

def _norm(df):
    cols = ["date", "close"] + (["amount"] if "amount" in df.columns else [])
    df = df[cols].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    return df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)


def _try_em(code, retries):
    err = None
    for _ in range(retries):
        try:
            d = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="qfq")
            if d is not None and not d.empty:
                d = d.rename(columns={"日期": "date", "收盘": "close"})
                if "close" in d.columns:
                    record_fetch_health("东财日线", True)
                    return _norm(d)
            err = "bad_response"
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.2)
    record_fetch_health("东财日线", False, err)
    return None


def _try_sina(code, retries):
    prefix = "sh" if code[:1] in ("5", "6") else "sz"
    err = None
    for _ in range(retries):
        try:
            with _AK_JS_LOCK:   # 新浪接口走 mini_racer，见锁定义处
                d = ak.fund_etf_hist_sina(symbol=prefix + code)
            if d is not None and not d.empty and "close" in d.columns:
                record_fetch_health("新浪日线", True)
                return _norm(d)
            err = "bad_response"
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.2)
    record_fetch_health("新浪日线", False, err)
    return None


def _save_cache(name, df):
    """落盘日线缓存。**只保留已收盘定稿的行**：westock 等实时源在盘中会带回"今天"的盘中价行，
    若原样落盘，收盘后 `_is_cache_current` 会把它当成当日收盘定稿价（早盘价冒充收盘价）。
    故写入前剔除日期晚于最近已收盘交易日的行——缓存里永远只有定稿数据，
    `cache_current` 的"与实时拉取等价"承诺才成立。"""
    try:
        if df is not None and "date" in getattr(df, "columns", ()):
            settled = _latest_completed_session()
            if settled is not None:
                df = df[df["date"].dt.date <= settled]
            if df.empty:
                return
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_csv(os.path.join(CACHE_DIR, f"{name}.csv"), index=False)
    except Exception:  # noqa: BLE001
        pass


def _settled_signal_frame(df, latest_session):
    """返回仅含最近已收盘交易日及以前数据的信号计算帧。

    实时源盘中会附带今天的未收盘日线。它可以服务执行时点报价，但绝不能进入
    MA/动量/偏离等日终信号；否则缓存落后时的首次运行与同日第二次运行会使用
    不同口径。latest_session=None 保持调用方原行为（非正式日终信号用途）。
    """
    if df is None or latest_session is None or "date" not in getattr(df, "columns", ()):
        return df
    try:
        settled = df[df["date"].dt.date <= latest_session].copy()
        return settled.reset_index(drop=True)
    except Exception:  # noqa: BLE001
        return df


def _read_cache(name):
    p = os.path.join(CACHE_DIR, f"{name}.csv")
    if os.path.exists(p):
        try:
            return _norm(pd.read_csv(p))
        except Exception:  # noqa: BLE001
            return None
    return None


# ── westock（腾讯自选股）行情：实测更稳更全，作为日线【首选源】（东财/新浪/缓存为后备）──
# 取到的是新鲜实时前复权价 → source="westock"，按"完整"对待、不触发缓存禁令。
# 性能：main() 先调 prefetch_westock() 一次性批量取所有 code（输出含 symbol 列的单表），
# 之后 fetch_hist 的 westock 源直接命中 _WESTOCK_HIST，避免逐只 npx。
WESTOCK_PKG = "westock-data-skillhub@1.0.3"
_WESTOCK_HIST = {}   # bare_code -> DataFrame[date,close(,amount)]；由 prefetch_westock 批量填充


def _westock_symbol(code):
    """裸代码 → 带市场前缀（5/6 开头=沪市 sh，其余=深市 sz，如 159915→sz159915）。"""
    return ("sh" if str(code)[:1] in ("5", "6") else "sz") + str(code)


def _parse_westock_kline(md):
    """解析 westock `kline` 的 Markdown 表为 DataFrame[date, close(, amount)]（close 取 last 列、amount 取成交额列）。失败返回 None。"""
    if not md:
        return None
    lines = [ln for ln in md.splitlines() if ln.strip().startswith("|")]
    header, body = None, None
    for i in range(len(lines) - 1):
        sep = lines[i + 1].replace("|", "").replace(" ", "")
        if sep and set(sep) <= set("-"):
            header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            body = lines[i + 2:]
            break
    if not header or "date" not in header or "last" not in header or not body:
        return None
    di, ci = header.index("date"), header.index("last")
    ai = header.index("amount") if "amount" in header else None
    rows = []
    for ln in body:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) == len(header):
            row = {"date": cells[di], "close": cells[ci]}
            if ai is not None:
                row["amount"] = cells[ai]
            rows.append(row)
    if not rows:
        return None
    try:
        return _norm(pd.DataFrame(rows))
    except Exception:  # noqa: BLE001
        return None


def _parse_westock_kline_batch(md):
    """解析批量 kline 输出（含 symbol 列的单表）为 {bare_code: DataFrame[date,close(,amount)]}。"""
    if not md:
        return {}
    lines = [ln for ln in md.splitlines() if ln.strip().startswith("|")]
    header, body = None, None
    for i in range(len(lines) - 1):
        sep = lines[i + 1].replace("|", "").replace(" ", "")
        if sep and set(sep) <= set("-"):
            header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            body = lines[i + 2:]
            break
    if not header or not body or not {"symbol", "date", "last"} <= set(header):
        return {}
    si, di, ci = header.index("symbol"), header.index("date"), header.index("last")
    ai = header.index("amount") if "amount" in header else None
    grouped = {}
    for ln in body:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) != len(header):
            continue
        sym = cells[si]
        bare = sym[2:] if sym[:2] in ("sh", "sz") else sym
        row = {"date": cells[di], "close": cells[ci]}
        if ai is not None:
            row["amount"] = cells[ai]
        grouped.setdefault(bare, []).append(row)
    out = {}
    for bare, rows in grouped.items():
        try:
            df = _norm(pd.DataFrame(rows))
            if not df.empty:
                out[bare] = df
        except Exception:  # noqa: BLE001
            pass
    return out


def prefetch_westock(codes, limit=320):
    """一次性批量取 westock 日线，填入 _WESTOCK_HIST（bare_code->df），供 fetch_hist 命中。

    npx 不可用/失败/超时则不填充（fetch_hist 会自然回退到东财/新浪/缓存）。
    """
    codes = [str(c) for c in codes if c]
    exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not codes or not exe:
        return
    syms = ",".join(_westock_symbol(c) for c in codes)
    try:
        r = subprocess.run(
            [exe, "-y", WESTOCK_PKG, "kline", syms, "--period", "day", "--limit", str(limit), "--fq", "qfq"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r.returncode == 0:
            got = _parse_westock_kline_batch(r.stdout)
            _WESTOCK_HIST.update(got)
            record_fetch_health("westock行情", bool(got), None if got else "输出解析为空")
        else:
            record_fetch_health("westock行情", False, (r.stderr or "").strip()[:200] or f"exit={r.returncode}")
    except Exception as e:  # noqa: BLE001
        record_fetch_health("westock行情", False, e)


def _try_westock(code, limit=320):
    """取 westock 日线前复权价：优先用 prefetch 批量结果；未预取则单只取一次。失败返回 None。"""
    bare = str(code)
    if bare in _WESTOCK_HIST:
        df = _WESTOCK_HIST[bare]
        return df if df is not None and not df.empty else None
    exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "-y", WESTOCK_PKG, "kline", _westock_symbol(code),
             "--period", "day", "--limit", str(limit), "--fq", "qfq"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r.returncode == 0:
            return _parse_westock_kline(r.stdout)
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_hist(code, retries=2, latest_session=None):
    """多源取日终价格。返回 (DataFrame[date,close], source)；source ∈ {'cache_current','westock','live','cache',None}。

    顺序：[缓存已达最新已收盘交易日→cache_current] → westock(腾讯自选股, qfq, 首选) → 东财(qfq) → 新浪 → 本地缓存。
    westock 取到的是新鲜实时价，按"完整"对待（不计入 used_cache、不触发缓存交易禁令）。
    `cache_current`（缓存==最新收盘交易日）同样按"完整"对待——它就是该交易日的定稿价、与实时拉取等价，故跳过网络。
    """
    if latest_session is not None and _is_cache_current(code, latest_session):
        df = _read_cache(code)
        if df is not None and not df.empty:
            return df, "cache_current"      # 数据已定稿：跳过所有网络
    df = _try_westock(code)
    if df is not None and not df.empty:
        _save_cache(code, df)
        settled = _settled_signal_frame(df, latest_session)
        if settled is not None and not settled.empty:
            return settled, "westock"
    df = _try_em(code, retries)
    if df is not None:
        _save_cache(code, df)
        settled = _settled_signal_frame(df, latest_session)
        if settled is not None and not settled.empty:
            return settled, "live"
    df = _try_sina(code, retries)
    if df is not None:
        _save_cache(code, df)
        settled = _settled_signal_frame(df, latest_session)
        if settled is not None and not settled.empty:
            return settled, "live"
    df = _read_cache(code)
    if df is not None:
        return df, "cache"
    print(f"  [警告] {code} 所有数据源失败且无缓存", file=sys.stderr)
    return None, None


def _load_valuation_series(cache_path):
    """读乐咕缓存里本地化的历史 PE 窗口 {date: pe}（旧格式缓存无 series → 空 dict）。"""
    try:
        with open(cache_path, encoding="utf-8") as f:
            c = json.load(f)
        return {str(k): float(v) for k, v in (c.get("series") or {}).items() if v is not None}
    except Exception:  # noqa: BLE001
        return {}


def _save_valuation_cache(cache_path, res, today, series=None):
    """写乐咕估值缓存：结果字段 + 本地化历史窗口。series=None（备援路径）→ 保留既有序列原样——
    序列永远保持纯乐咕，备援点绝不混入（两家口径毫厘差不污染历史）。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if series is None:
            series = _load_valuation_series(cache_path)
        payload = {**res, "fetched_at": str(today)}
        if series:
            payload["series"] = dict(sorted(series.items()))
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, cache_path)
    except Exception:  # noqa: BLE001
        pass


def _cached_valuation_result(c):
    """缓存 dict → 对外结果字段（备援标记如实透传，让"这是备援算的"在后续缓存回放里也可见）。"""
    res = {"pe": c["pe"], "percentile": c["percentile"], "pe_median": c.get("pe_median"), "as_of": c.get("as_of")}
    if c.get("backup_source"):
        res["backup_source"] = c["backup_source"]
    return res


def _csindex_backup_pe(index_name, retries=2):
    """乐咕失败时的备援：中证官网最新一个 PE 点 (as_of, pe)。

    取"市盈率2"（滚动TTM）与乐咕"滚动市盈率"同口径；只回当日点、绝不回历史（见 VAL_BACKUP_CSINDEX 注释）。
    指数无映射（非中证系）或取数失败 → None。"""
    code = VAL_BACKUP_CSINDEX.get(index_name)
    if not code:
        return None
    err = None
    for _ in range(retries):
        try:
            df = ak.stock_zh_index_value_csindex(symbol=code)
            if df is not None and not df.empty and "市盈率2" in df.columns and "日期" in df.columns:
                d = df.copy()
                d["pe2"] = pd.to_numeric(d["市盈率2"], errors="coerce")
                d["d"] = pd.to_datetime(d["日期"], errors="coerce")
                d = d.dropna(subset=["pe2", "d"]).sort_values("d")
                d = d[d["pe2"] > 0]
                if not d.empty:
                    record_fetch_health("中证估值备援", True)
                    return str(d["d"].iloc[-1].date()), float(d["pe2"].iloc[-1])
            err = "bad_response"
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.0)
    record_fetch_health("中证估值备援", False, err)
    return None


def fetch_valuation_pct(index_name, lookback_years, retries=3, latest_session=None):
    """估值分位（滚动市盈率）。乐咕实时 → csindex 备援点×本地化历史 → 缓存。返回 (result|None, status)。

    status: {available, source('live'/'live_backup'/'cache'/'cache_current'/'cache_stale'/None), as_of, stale_days, reason?}
    缓存 as_of 已达最新已收盘交易日 → PE 已定稿、跳过 legulegu 实时（脆且慢）。
    乐咕成功时把分位窗口的历史序列一并本地化（缓存 series 字段）——之后乐咕再挂，
    只需中证官网补"当日 PE 点"即可照常算分位（source='live_backup'），估值不再因单源故障整段缺失。
    """
    cache_path = os.path.join(CACHE_DIR, f"valuation_{index_name}.json")
    today = date.today()
    if latest_session is not None and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                c = json.load(f)
            c_asof = datetime.strptime(c["as_of"], "%Y-%m-%d").date() if c.get("as_of") else None
            if c_asof and c_asof >= latest_session:
                return (_cached_valuation_result(c),
                        {"available": True, "source": "cache_current", "as_of": c.get("as_of"), "stale_days": 0})
        except Exception:  # noqa: BLE001
            pass
    err = None
    for _ in range(retries):
        try:
            with _AK_JS_LOCK:   # 乐咕接口走 mini_racer，见锁定义处
                df = ak.stock_index_pe_lg(symbol=index_name)
            if df is not None and not df.empty and "滚动市盈率" in df.columns:
                h = pd.DataFrame({"d": pd.to_datetime(df["日期"], errors="coerce"),
                                  "pe": pd.to_numeric(df["滚动市盈率"], errors="coerce")}).dropna()
                if len(h) >= 30:
                    h = h.sort_values("d").tail(int(lookback_years * 244))
                    cur = float(h["pe"].iloc[-1])
                    pct = float((h["pe"] < cur).mean())
                    as_of = str(h["d"].iloc[-1].date())
                    # pe_median：历史中位 PE，作积木式预期收益里"估值回归"的锚（向它回归）
                    res = {"pe": round(cur, 2), "percentile": round(pct, 3),
                           "pe_median": round(float(h["pe"].median()), 2), "as_of": as_of}
                    series = {str(r.d.date()): round(float(r.pe), 4) for r in h.itertuples()}
                    _save_valuation_cache(cache_path, res, today, series=series)
                    record_fetch_health("乐咕估值", True)
                    return res, {"available": True, "source": "live", "as_of": as_of, "stale_days": 0}
            # 响应形状不对（接口改版/被挡）≈ 源故障：不再直接放弃，落入备援→缓存链
            err = "bad_response"
            break
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.2)
    record_fetch_health("乐咕估值", False, err)
    # 备援：中证官网当日 PE 点 × 本地化乐咕历史窗口 → 分位（历史与当日点解耦）
    series = _load_valuation_series(cache_path)
    if len(series) >= 30:
        bk = _csindex_backup_pe(index_name)
        if bk:
            b_as_of, b_pe = bk
            series_through = max(series)
            merged = dict(series)
            merged[b_as_of] = b_pe
            window = [merged[k] for k in sorted(merged)][-int(lookback_years * 244):]
            pct = sum(1 for x in window if x < b_pe) / len(window)
            med = sorted(window)[len(window) // 2]
            res = {"pe": round(b_pe, 2), "percentile": round(pct, 3), "pe_median": round(float(med), 2),
                   "as_of": b_as_of, "backup_source": "csindex", "series_through": series_through}
            _save_valuation_cache(cache_path, res, today)   # series=None → 序列保持纯乐咕
            return res, {"available": True, "source": "live_backup", "as_of": b_as_of,
                         "stale_days": 0, "backup_source": "csindex"}
    # 回退缓存
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                c = json.load(f)
            fa = c.get("fetched_at")
            stale = (today - datetime.strptime(fa, "%Y-%m-%d").date()).days if fa else 999
            if stale <= VAL_STALE_LIMIT_DAYS:
                return (_cached_valuation_result(c),
                        {"available": True, "source": "cache", "as_of": c.get("as_of"), "stale_days": stale})
            return None, {"available": False, "source": "cache_stale", "reason": "cache_too_old", "stale_days": stale}
        except Exception:  # noqa: BLE001
            pass
    return None, {"available": False, "source": None,
                  "reason": "bad_response" if err == "bad_response" else "network_failed"}


def _val_tag(pct, cheap, rich):
    """分位 → 三态：≤cheap 便宜 / ≥rich 偏贵 / 其间中性。"""
    return "cheap" if pct <= cheap else ("rich" if pct >= rich else "neutral")


def _load_valuation_accum(code):
    """读自建累积估值文件 → (meta, {date: pe})。缺/坏文件回退空。"""
    path = os.path.join(VALUATION_DIR, f"{code}.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d, {str(k): float(v) for k, v in (d.get("series") or {}).items() if v is not None}
        except Exception:  # noqa: BLE001
            pass
    return {}, {}


def fetch_valuation_csindex(code, lookback_years, index_name=None, retries=2, latest_session=None):
    """中证指数官方估值(csindex)按日自建累积分位。

    csindex 静态文件只滚动给最近 ~20 个交易日、**无长历史** → 每次把窗口并进
    journal/valuation/<code>.json（按日去重）、分位由累积历史算（时钟从接入日起走）。
    历史 < VALUATION_ACCUM_MIN_YEARS → status accumulating：**只回当前 PE 水平、percentile=None**
    （20 个点的"分位"是噪声，绝不冒充信号）。返回 (result|None, status)。
    缓存跳过：今天已拉过(fetched_at==today) → 跳过 Excel 重下（累积只喂"分位积累中"、晚几小时无妨）。
    result: {pe, percentile|None, as_of, history_months, history_from, window_years?, source, index_name}。"""
    meta, series = _load_valuation_accum(code)
    today = date.today()
    fetched = False
    skip_live = latest_session is not None and meta.get("fetched_at") == str(today) and bool(series)
    if not skip_live:
        err = None
        for _ in range(retries):
            try:
                df = ak.stock_zh_index_value_csindex(symbol=code)
                if df is not None and not df.empty and "市盈率1" in df.columns:
                    for _, r in df.iterrows():
                        d, pe = r.get("日期"), pd.to_numeric(r.get("市盈率1"), errors="coerce")
                        if d is not None and pd.notna(pe) and float(pe) > 0:
                            series[str(d)] = round(float(pe), 4)
                    if index_name is None and "指数中文简称" in df.columns:
                        index_name = str(df["指数中文简称"].iloc[0])
                    fetched = True
                    break
                err = "bad_response"
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(1.0)
        record_fetch_health("中证估值累积", fetched, None if fetched else err)
    if fetched:
        try:
            os.makedirs(VALUATION_DIR, exist_ok=True)
            with open(os.path.join(VALUATION_DIR, f"{code}.json"), "w", encoding="utf-8") as f:
                json.dump({"code": code, "index_name": index_name or meta.get("index_name"),
                           "source": "csindex", "ttm_field": "市盈率1", "fetched_at": str(today),
                           "series": dict(sorted(series.items()))}, f, ensure_ascii=False, indent=1)
        except Exception:  # noqa: BLE001
            pass
    if not series:
        return None, {"available": False, "source": None,
                      "reason": "network_failed" if not fetched else "bad_response"}
    dates = sorted(series)
    cur, as_of = series[dates[-1]], dates[-1]
    first = datetime.strptime(dates[0], "%Y-%m-%d").date()
    last = datetime.strptime(dates[-1], "%Y-%m-%d").date()
    span_years = (last - first).days / 365.25
    history_months = int(round((last - first).days / 30.4))
    src = "csindex" if fetched else "csindex_cache"
    name = index_name or meta.get("index_name")
    base = {"pe": round(cur, 2), "as_of": as_of, "source": src, "index_name": name,
            "history_months": history_months, "history_from": dates[0],
            "needed_years": VALUATION_ACCUM_MIN_YEARS}
    if span_years < VALUATION_ACCUM_MIN_YEARS:
        return ({**base, "percentile": None, "accumulating": True},
                {"available": True, "source": src, "as_of": as_of, "accumulating": True,
                 "history_months": history_months})
    vals = [series[d] for d in dates]
    n = int(lookback_years * 244)
    window = vals[-n:] if len(vals) > n else vals
    pct = sum(1 for x in window if x < cur) / len(window)
    win_years = round(min(span_years, lookback_years), 1)
    pe_median = round(float(sorted(window)[len(window) // 2]), 2)   # 窗口中位 PE（估值回归锚）
    return ({**base, "percentile": round(pct, 3), "pe_median": pe_median, "window_years": win_years},
            {"available": True, "source": src, "as_of": as_of, "window_years": win_years})


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


LOT_SIZE = 100  # 场内 ETF 最小交易单位：1 手 = 100 份


def lots_for_amount(amount, price, lot_size=LOT_SIZE):
    """把建议金额折算到整手。四舍五入到最近整手；不足半手 → 0 手（本次不动手）。

    返回 (整手份额, 整手金额, 一手价值)。price/amount 非法时返回 (0, 0.0, 一手价值)。
    用于再平衡：高单价 ETF 在小盘里一手金额很大，连续金额建议常落在"小于一手/无法整手命中"，
    此处折算后由调用方据 lot_shares==0 诚实压制，不给无法执行的零碎建议。
    """
    lot_value = (lot_size * price) if (price and price > 0) else 0.0
    amt = abs(float(amount or 0))
    if lot_value <= 0 or amt <= 0:
        return 0, 0.0, lot_value
    lots = int(round(amt / lot_value))
    shares = lots * lot_size
    return shares, float(shares) * price, lot_value


def first_funding_orders(holdings, prices, budget, current_values=None, min_trade=0, lot_size=LOT_SIZE):
    """按【缺口优先】把一笔预算 budget 逐手铺到目标权重——首次/分批建仓的单次部署。

    缺口优先(gap-fill)：反复买入"离目标权重最远"那只的一手，直到预算买不动任何一手、或再买就会
    越过目标（不过冲）为止。高现金周→近似按目标权重比例铺开；低现金周→自动集中火力先补最欠配的
    一两只，不再把小钱平摊成一堆够不到门槛的碎单。
        current_values={code: 已持有市值}（多周累计建仓时传入；首周为空=全 0）。
        不过冲守则：仅当 缺口 > 一手金额/2 才买（买一手能让该腿更靠近目标，而非越过它）。
    返回 (orders, 实际成交额)。本函数只受"一手/预算/最小金额"约束；溢价等执行质量在 reports 层叠加。
    """
    current_values = dict(current_values or {})
    by_code = {str(h["code"]): h for h in holdings}
    codes = [str(h["code"]) for h in holdings]
    alloc = {c: 0.0 for c in codes}        # 本次新买入金额
    shares_alloc = {c: 0 for c in codes}   # 本次新买入份额
    budget = max(float(budget or 0), 0.0)
    ref_total = sum(current_values.get(c, 0.0) for c in codes) + budget
    spent = 0.0
    while budget > 0:
        best, best_deficit = None, 0.0
        for c in codes:
            price = prices.get(c)
            if not price or price <= 0:
                continue
            lot_cost = price * lot_size
            if spent + lot_cost > budget + 1e-6:
                continue                                  # 剩余预算买不起这一手
            tw = float(by_code[c].get("target_weight", 0) or 0)
            held = current_values.get(c, 0.0) + alloc[c]
            deficit = ref_total * tw - held
            if deficit <= lot_cost / 2.0:                 # 不过冲：买一手不会更靠近目标
                continue
            if deficit > best_deficit:
                best, best_deficit = c, deficit
        if best is None:
            break
        price = prices[best]
        alloc[best] += price * lot_size
        shares_alloc[best] += lot_size
        spent += price * lot_size
    orders, actual = [], 0.0
    for c in codes:
        h = by_code[c]
        price = prices.get(c)
        tw = float(h.get("target_weight", 0) or 0)
        gap = max(0.0, ref_total * tw - current_values.get(c, 0.0))
        shares, amount = shares_alloc[c], alloc[c]
        reasons = []
        if shares <= 0 and gap > 0:
            lot_cost = (price * lot_size) if price else 0
            if lot_cost and lot_cost > budget:
                reasons.append("不足一手，暂不下单")
            else:
                reasons.append("本周预算优先补更欠配品种，待下次到账再补")
        elif 0 < amount < min_trade:
            reasons.append(f"金额低于最小交易门槛 {min_trade:.0f} 元")
        actionable = shares > 0 and amount >= min_trade
        if actionable:
            actual += amount
        orders.append({
            "code": c,
            "name": h.get("name", c),
            "target_weight": round(tw, 4),
            "target_amount": round(gap, 0),
            "last": round(price, 4) if price else None,
            "estimated_shares": shares,
            "estimated_amount": round(amount, 0),
            "actionable": bool(actionable),
            "blocked_reasons": reasons,
        })
    return orders, actual


def reconcile_first_funding_plan(plan):
    """按订单最终 actionable 状态重算首建汇总，不重分配被拦资金。"""
    if not isinstance(plan, dict) or not plan.get("eligible"):
        return plan
    orders = plan.get("orders") or []
    pre_gate = float(plan.get("pre_gate_estimated_deploy_amount",
                              plan.get("estimated_deploy_amount", 0)) or 0)
    executable = sum(float(o.get("estimated_amount", 0) or 0) for o in orders if o.get("actionable"))
    planned = float(plan.get("planned_deploy_amount", 0) or 0)
    cash = float(plan.get("cash", 0) or 0)
    plan["pre_gate_estimated_deploy_amount"] = round(pre_gate, 2)
    plan["estimated_deploy_amount"] = round(executable, 2)
    plan["blocked_deploy_amount"] = round(max(pre_gate - executable, 0.0), 2)
    plan["estimated_unallocated"] = round(max(planned - executable, 0.0), 2)
    plan["remaining_cash_after_execution"] = round(max(cash - executable, 0.0), 2)
    return plan


def build_first_funding_schedule(holdings, prices, cash, max_weekly, min_trade, lot_size=LOT_SIZE):
    """0持仓账户的多周分批建仓草案：每周固定上限 max_weekly、缺口优先逐手铺开、跨周累计持仓。

    上限<=0 视为不限速（一周内铺完）。后续周次必须复盘后再执行（status=requires_prior_review）。
    注：本草案按"当前现金"快照推算周数；资金分批到账时，每次刷新会据最新现金重算（详见 DEPLOYMENT_REDESIGN.md）。
    """
    if cash <= 0:
        return []
    weekly_cap = max_weekly if max_weekly > 0 else cash
    if weekly_cap <= 0:
        return []
    weeks = int(cash // weekly_cap) + (1 if (cash % weekly_cap) > 1e-6 else 0)
    weeks = max(1, min(weeks, 52))
    schedule = []
    remaining = cash
    current_values = {str(h["code"]): 0.0 for h in holdings}   # 跨周累计，让缺口优先在周间生效
    for week in range(1, weeks + 1):
        planned = min(weekly_cap, remaining)
        if planned <= 1e-6:
            break
        orders, actual = first_funding_orders(holdings, prices, planned, current_values, min_trade, lot_size)
        for o in orders:
            current_values[o["code"]] = current_values.get(o["code"], 0.0) + o["estimated_amount"]
        schedule.append({
            "week": week,
            "planned_amount": round(planned, 0),
            "estimated_amount": round(actual, 0),
            "estimated_unallocated": round(max(planned - actual, 0), 0),
            "orders": [{
                "code": o["code"], "name": o["name"], "target_weight": o["target_weight"],
                "target_amount": o["target_amount"], "estimated_shares": o["estimated_shares"],
                "estimated_amount": o["estimated_amount"], "blocked_reasons": o["blocked_reasons"],
            } for o in orders],
            "status": "ready" if week == 1 else "requires_prior_review",
            "notes": ["第1周可作为试仓预览；后续周次必须先完成上周复盘，不自动执行"],
        })
        remaining -= planned
    return schedule


_REGIME_EQUITY_ASSETS = {"equity", "equity_defensive", "china_growth", "global_equity", "global_growth"}


def correlation_diagnostic(closes_by_code, weights, *, min_obs=20):
    """据持仓价格历史算收缩协方差 → 有效风险来源数 / 平均相关性 / 组合年化波动（B-3 / F1-03）。

    诚实披露：周度压力数是『线性叠加(Σ权重×冲击)』、忽略相关性；本诊断把"N 只 ETF 实际相当于几个独立风险源"
    显式化（复用 strategic 收缩协方差，单一实现）。weights={code:权重}(ETF桶口径)。数据不足→available=False。
    """
    returns = {}
    for code, closes in (closes_by_code or {}).items():
        if float(weights.get(str(code), 0) or 0) <= 0:
            continue
        s = [float(x) for x in (closes or []) if x is not None and float(x) > 0]
        if len(s) < min_obs + 1:
            continue
        returns[str(code)] = [s[i] / s[i - 1] - 1.0 for i in range(1, len(s))]
    if len(returns) < 2:
        return {"available": False, "reason": "持仓价格历史不足，无法估相关性（退回仅线性压力口径）"}
    cov = strategic.shrinkage_covariance(returns, min_obs=min_obs)
    if not cov:
        return {"available": False, "reason": "观测不足，协方差退化（退回仅线性压力口径）"}
    labels, M = cov["labels"], cov["matrix"]
    w = [float(weights.get(l, 0) or 0) for l in labels]
    asset_vol = [M[i][i] ** 0.5 if M[i][i] > 0 else 0.0 for i in range(len(labels))]
    wavg_vol = sum(w[i] * asset_vol[i] for i in range(len(labels)))
    port_var = sum(w[i] * sum(M[i][j] * w[j] for j in range(len(labels))) for i in range(len(labels)))
    port_vol = port_var ** 0.5 if port_var > 0 else 0.0
    # 分散比 = 加权平均单资产波动 ÷ 组合波动（=1 完全同涨同跌、无分散；越高越分散）——直接反映相关性收益。
    div_ratio = round(wavg_vol / port_vol, 2) if port_vol > 0 else None
    rc = strategic.risk_contributions(cov, {str(k): v for k, v in weights.items()}, annualize=252.0)
    out = {
        "available": True,
        "n_holdings": len(returns),
        "avg_corr": cov.get("avg_corr"),
        "diversification_ratio": div_ratio,
        "obs": cov.get("obs"),
        "note": ("周度压力数为『线性叠加（Σ权重×冲击）』、未计相关性。分散比 = 加权平均单资产波动 ÷ 组合波动"
                 "（=1 完全同涨同跌、无分散；越高越分散）；危机中相关性升向 1、分散比趋近 1，聚集回撤可能超过线性估计。"),
    }
    if rc:
        out["effective_bets"] = rc.get("effective_bets")        # 风险贡献集中度（HHI 倒数）：风险摊在几只上
        out["portfolio_vol_annual"] = rc.get("vol")
    return out


def regime_state(holdings, per):
    """市场 regime 简化指标：权益持仓中跌破 MA200 的目标权重广度（B-3）。

    广度高 = 多数权益走弱 → 偏弱 regime（危机中相关性上升、分散打折，提示更谨慎、别追高）。纯函数。
    """
    eq_total = below = 0.0
    for h in (holdings or []):
        s = per.get(str(h.get("code"))) or {}
        if s.get("asset") in _REGIME_EQUITY_ASSETS:
            w = float(h.get("target_weight", 0) or 0)
            eq_total += w
            if s.get("trend") == "below":
                below += w
    ratio = (below / eq_total) if eq_total > 0 else 0.0
    state = "偏弱" if ratio >= 0.5 else ("转弱" if ratio >= 0.25 else "偏强")
    return {
        "equity_total_weight": round(eq_total, 4),
        "equity_below_ma200_weight": round(below, 4),
        "below_ratio": round(ratio, 4),
        "state": state,
        "stressed": bool(ratio >= 0.5),
        "note": (f"{ratio * 100:.0f}% 的权益目标权重已跌破 MA200 → 市场偏弱；危机中相关性上升、分散效果打折，本周更应保守、避免追高。"
                 if ratio >= 0.5 else f"{ratio * 100:.0f}% 的权益目标权重跌破 MA200。"),
    }


def build_preflight_checks(grade, rebal_ok, used_cache, allow_cache_trade, holdings, per, min_trade, max_weekly,
                           is_zero_position, risk_budget_breached=False, target_stress_drawdown=0, max_drawdown=0,
                           strategic_policy=None, regime=None):
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
            f"按计划满仓口径全组合压力回撤约 {target_stress_drawdown * 100:.1f}%，超过可接受回撤 {max_drawdown * 100:.1f}%"
            if risk_budget_breached else
            f"按计划满仓口径全组合压力回撤约 {target_stress_drawdown * 100:.1f}%，未超过可接受回撤 {max_drawdown * 100:.1f}%"
        ),
    })
    checks.append({
        "id": "zero_position",
        "label": "0 持仓状态",
        "status": "warn" if is_zero_position else "pass",
        "message": "当前为 0 持仓，只使用首次建仓预览，不直接执行再平衡" if is_zero_position else "非 0 持仓，可按再平衡纪律评估",
    })
    # B-2（F2-02）：用真实 target_weight 体检长期政策集中度上限（货币/国家/卫星/成长）；warn 口径——
    #   触及上限只提示、需人工确认，不硬拦（所有者拍板：警告+确认）。
    if strategic_policy:
        asset_of = {str(h.get("code")): (per.get(str(h.get("code"))) or {}).get("asset") for h in holdings}
        checks.append(strategic.live_concentration_checks(holdings, asset_of, strategic_policy))
    # B-3：市场 regime（权益跌破 MA200 广度）——偏弱时 warn（相关性上升、分散打折，宜保守），不硬拦。
    if regime is not None:
        checks.append({
            "id": "regime",
            "label": "市场状态",
            "status": "warn" if regime.get("stressed") else "pass",
            "message": (f"市场偏弱：{regime.get('below_ratio', 0) * 100:.0f}% 权益目标权重跌破 MA200 → 危机中相关性上升、分散打折，本周宜保守、避免追高"
                        if regime.get("stressed") else
                        f"市场状态 {regime.get('state') or '—'}（{regime.get('below_ratio', 0) * 100:.0f}% 权益跌破 MA200）"),
        })
    return checks


# 各资产类别的简化假设（单一事实源，战略构建也复用这两张表）：
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


def load_assumptions(strat):
    """收益/冲击假设的【单一来源】：默认=本模块两张表，strategy.yaml 的 `assumptions` 块逐键覆盖。

    返回 {shocks, returns, default_shock, default_return, meta:{asset:{source,note}}}。
    缺省（无 assumptions 块）即回退到硬编码默认，向后兼容。战略构建也只读这里、不另写一份。
    """
    shocks = dict(ASSET_SHOCKS)
    returns = dict(ASSET_EXPECTED_RETURN)
    default_shock, default_return = DEFAULT_SHOCK, DEFAULT_EXPECTED_RETURN
    meta = {}
    block = (strat or {}).get("assumptions") or {}
    defaults = block.get("defaults") or {}
    if _num_ok(defaults.get("shock")):
        default_shock = float(defaults["shock"])
    if _num_ok(defaults.get("expected_return")):
        default_return = float(defaults["expected_return"])
    for asset, cfg in (block.get("sleeves") or {}).items():
        cfg = cfg if isinstance(cfg, dict) else {}
        if _num_ok(cfg.get("shock")):
            shocks[asset] = float(cfg["shock"])
        if _num_ok(cfg.get("expected_return")):
            returns[asset] = float(cfg["expected_return"])
        m = {}
        if cfg.get("source"):
            m["source"] = str(cfg["source"])
        if cfg.get("note"):
            m["note"] = str(cfg["note"])
        if m:
            meta[asset] = m
    # §9.1 收益区间：每类给 central/conservative/optimistic。优先用 sleeve 显式值，否则按 haircut 从 central 派生。
    haircut = float(defaults["return_haircut"]) if _num_ok(defaults.get("return_haircut")) else 0.03
    returns_conservative, returns_optimistic = {}, {}
    for asset, central in returns.items():
        returns_conservative[asset] = round(central - haircut, 6)
        returns_optimistic[asset] = round(central + haircut, 6)
    for asset, cfg in (block.get("sleeves") or {}).items():
        cfg = cfg if isinstance(cfg, dict) else {}
        if _num_ok(cfg.get("return_conservative")):
            returns_conservative[asset] = float(cfg["return_conservative"])
        if _num_ok(cfg.get("return_optimistic")):
            returns_optimistic[asset] = float(cfg["return_optimistic"])
    return {"shocks": shocks, "returns": returns,
            "default_shock": default_shock, "default_return": default_return, "meta": meta,
            "returns_conservative": returns_conservative, "returns_optimistic": returns_optimistic,
            "return_haircut": haircut,
            "default_return_conservative": round(default_return - haircut, 6),
            "default_return_optimistic": round(default_return + haircut, 6)}


# §9.3 多情景压力：每情景一条完整资产冲击向量（负=损失、正=受益），不用单资产独立冲击相加。
#   数值为据史的示意档，所有者可在 strategy.yaml: stress_scenarios 覆盖并定档严重度。
DEFAULT_STRESS_SCENARIOS = [
    {"name": "全球权益危机", "shocks": {"equity": -0.35, "equity_defensive": -0.25, "china_growth": -0.45,
                                  "global_equity": -0.35, "global_growth": -0.45, "bond": -0.02, "gold": 0.05}},
    {"name": "中国权益危机", "shocks": {"equity": -0.35, "equity_defensive": -0.25, "china_growth": -0.45,
                                  "global_equity": -0.10, "global_growth": -0.12, "bond": 0.01, "gold": 0.03}},
    {"name": "美国科技重估", "shocks": {"equity": -0.08, "equity_defensive": -0.05, "china_growth": -0.15,
                                  "global_equity": -0.20, "global_growth": -0.40, "bond": 0.01, "gold": 0.0}},
    {"name": "利率急升", "shocks": {"equity": -0.10, "equity_defensive": -0.08, "china_growth": -0.20,
                              "global_equity": -0.12, "global_growth": -0.25, "bond": -0.08, "gold": -0.10}},
    {"name": "通胀冲击", "shocks": {"equity": -0.10, "equity_defensive": -0.05, "china_growth": -0.15,
                              "global_equity": -0.10, "global_growth": -0.18, "bond": -0.06, "gold": 0.15}},
    {"name": "人民币升值", "shocks": {"equity": 0.0, "equity_defensive": 0.0, "china_growth": 0.0,
                               "global_equity": -0.10, "global_growth": -0.10, "bond": 0.0, "gold": -0.08}},
    {"name": "QDII额度/溢价冲击", "shocks": {"equity": 0.0, "equity_defensive": 0.0, "china_growth": 0.0,
                                     "global_equity": -0.08, "global_growth": -0.10, "bond": 0.0, "gold": 0.0}},
]


def load_stress_scenarios(strat):
    """多情景压力(§9.3)。strategy.yaml `stress_scenarios`（list of {name, shocks}）覆盖；缺省=内置七情景。

    返回 [{name, shocks:{asset:shock}}]。
    """
    block = (strat or {}).get("stress_scenarios")
    if isinstance(block, list) and block:
        out = []
        for sc in block:
            if isinstance(sc, dict) and isinstance(sc.get("shocks"), dict):
                sh = {str(k): float(v) for k, v in sc["shocks"].items() if _num_ok(v)}
                if sh:
                    out.append({"name": str(sc.get("name") or "情景"), "shocks": sh})
        if out:
            return out
    return [{"name": s["name"], "shocks": dict(s["shocks"])} for s in DEFAULT_STRESS_SCENARIOS]


# §0C #1 历史危机情景（据真实峰谷标定，非拍脑袋）：用于【周度风险预算展示】"若 20XX 重演会怎样"。
#   标定来源：`python engine/backtest.py --stress-scenarios`（据 engine/data/idx_*.csv 种子，2026-06-08）。
#   口径：价格指数峰→谷；锚=各窗口内跌最深的权益代理，全资产用同一对日期算（捕捉债/金在权益低点的真实对冲）。
#   ⚠️ 故意【不】喂给战略构建的接受闸——把 -71% 权益塞进 construct 会逼出极端保守组合（属 §0C #3 的接受判定改造）；
#      这里只做诚实展示，让所有者看见"85% 权益桶在真实 08 级尾部约亏多少"。china_growth 用中证500 代理。
HISTORICAL_CRISIS_SCENARIOS = [
    {"name": "2008金融危机", "window": ["2008-01-15", "2008-11-04"], "anchor": "sh000905",
     "shocks": {"equity": -0.7143, "equity_defensive": -0.7143, "china_growth": -0.7242,
                "global_equity": -0.2717, "global_growth": -0.2637, "bond": 0.0691, "gold": -0.1903,
                "short_bond": 0.0345, "cash": 0.0}},
    {"name": "2015股灾", "window": ["2015-06-12", "2016-01-28"], "anchor": "sh000905",
     "shocks": {"equity": -0.4651, "equity_defensive": -0.4651, "china_growth": -0.5435,
                "global_equity": -0.0959, "global_growth": -0.1078, "bond": 0.0401, "gold": -0.0586,
                "short_bond": 0.02, "cash": 0.0}},
    {"name": "2018贸易战去杠杆", "window": ["2018-01-08", "2018-10-18"], "anchor": "sh000905",
     "shocks": {"equity": -0.2682, "equity_defensive": -0.2682, "china_growth": -0.3766,
                "global_equity": 0.0044, "global_growth": 0.0458, "bond": 0.0382, "gold": -0.0663,
                "short_bond": 0.0191, "cash": 0.0}},
    {"name": "2020疫情闪崩", "window": ["2020-02-19", "2020-03-23"], "anchor": "spx",
     "shocks": {"equity": -0.1286, "equity_defensive": -0.1286, "china_growth": -0.1079,
                "global_equity": -0.3392, "global_growth": -0.3012, "bond": 0.013, "gold": -0.0492,
                "short_bond": 0.0065, "cash": 0.0}},
    {"name": "2022加息回调", "window": ["2021-12-27", "2022-12-28"], "anchor": "ixic",
     "shocks": {"equity": -0.213, "equity_defensive": -0.213, "china_growth": -0.194,
                "global_equity": -0.2104, "global_growth": -0.3565, "bond": 0.037, "gold": 0.0025,
                "short_bond": 0.0185, "cash": 0.0}},
]


def load_historical_scenarios(strat):
    """历史危机情景（§0C #1）。strategy.yaml `historical_stress_scenarios` 覆盖；缺省=内置标定档。

    返回 [{name, window?, anchor?, shocks:{asset:shock}}]。
    """
    block = (strat or {}).get("historical_stress_scenarios")
    if isinstance(block, list) and block:
        out = []
        for sc in block:
            if isinstance(sc, dict) and isinstance(sc.get("shocks"), dict):
                sh = {str(k): float(v) for k, v in sc["shocks"].items() if _num_ok(v)}
                if sh:
                    out.append({"name": str(sc.get("name") or "情景"), "shocks": sh,
                                "window": sc.get("window"), "anchor": sc.get("anchor")})
        if out:
            return out
    return [dict(s) for s in HISTORICAL_CRISIS_SCENARIOS]


def estimate_stress_scenarios(holdings, universe, scenarios, default_shock=None):
    """多情景压力（§0C #1）：每情景算 ETF 桶【净】损失（同情景内对冲资产的受益抵损 → 体现真实分散）。

    每情景 loss = Σ 权重×冲击；ETF 桶回撤 = max(0, -loss)。返回 (按回撤降序的列表, 最坏情景|None)。
    纯函数、同输入同输出；不预测，仅"若该情景重演的目标组合损益"。
    """
    default_shock = DEFAULT_SHOCK if default_shock is None else default_shock
    results = []
    for sc in (scenarios or []):
        shocks = sc.get("shocks") or {}
        net = 0.0
        contributions = []
        for h in holdings:
            code = str(h.get("code"))
            tw = float(h.get("target_weight", 0) or 0)
            asset = (universe.get(code) or {}).get("asset")
            shock = shocks.get(asset, default_shock)
            net += tw * shock
            contributions.append({"code": code, "name": h.get("name", code), "asset": asset,
                                  "target_weight": round(tw, 4), "shock": round(shock, 4),
                                  "contribution": round(tw * shock, 4)})
        results.append({
            "name": sc.get("name") or "情景", "window": sc.get("window"), "anchor": sc.get("anchor"),
            "etf_drawdown": round(max(0.0, -net), 4), "net": round(net, 4),
            "contributions": contributions,
        })
    results.sort(key=lambda r: r["etf_drawdown"], reverse=True)
    return results, (results[0] if results else None)


# §0C #4 趋势过滤回撤保护（据长面板"趋势过滤 vs 静态"标定，非拍脑袋）：
#   量化"权益跌破 MA200 不动手会多扛多少回撤"。来源：`python engine/backtest.py --trend-benefit`（2026-06-08）。
#   ⚠️ 样本内、线上不自动执行——只把回测里趋势过滤的好处明牌给所有者，由人确认减仓。
TREND_PROTECTION_BENEFIT = {
    "static_maxdd": -0.4227, "trend_maxdd": -0.2114, "delta_pp": 21.1,
    "trend_cagr": 0.1174, "static_cagr": 0.1147, "years": 21.1, "start": "2005-12-20", "end": "2026-06-05",
}


def load_trend_protection(strat):
    """趋势过滤回撤保护标定（§0C #4）。strategy.yaml `trend_protection_benefit` 覆盖；缺省=内置档。"""
    block = (strat or {}).get("trend_protection_benefit")
    if isinstance(block, dict) and _num_ok(block.get("delta_pp")):
        return {**TREND_PROTECTION_BENEFIT, **{k: v for k, v in block.items() if _num_ok(v) or isinstance(v, str)}}
    return dict(TREND_PROTECTION_BENEFIT)


def build_trend_derisk(per, holdings, universe, mkt_vals, equity_assets, ma_days, look, *, min_trade, benefit,
                       discipline_blockers=None):
    """§0C #4：权益跌破 MA200 → 具体减仓建议（移到债券/防御），带回测量化的回撤差。人确认、不自动下单。

    纯函数。返回 [{code,name,asset,suggest:'derisk',derisk_amount,reserve_code,reserve_name,actionable,blocked_reasons,...}]。
    减仓金额=该品种当前市值（与回测"跌破即移出全部到债券"一致）；reserve=universe 里的 asset:bond。
    M1（2026-06-10 审查）：discipline_blockers 传入「价不可信」类纪律闸（数据过旧/缓存）——金额基于不可信价格时
    本建议同样不可执行，不得绕过再平衡区的同一道闸（风险预算超限不传入：减仓恰是减险，不拦）。
    """
    reserve_code = next((str(h["code"]) for h in holdings
                         if (universe.get(str(h["code"])) or {}).get("asset") == "bond"), None)
    reserve_name = (universe.get(reserve_code) or {}).get("name") if reserve_code else None
    out = []
    for c, s in per.items():
        if not (isinstance(s, dict) and s.get("asset") in equity_assets and s.get("trend") == "below"):
            continue
        value = float(mkt_vals.get(c, 0) or 0)
        amount = round(value, 0)
        blocked = list(discipline_blockers or [])
        if reserve_code is None:
            blocked.append("universe 无国债/防御标的可移入")
        elif reserve_code == c:
            blocked.append("该品种即防御标的，无需移仓")
        if amount < min_trade:
            blocked.append(f"持仓市值 {amount:.0f} 低于最小交易门槛 {min_trade:.0f} 元")
        out.append({
            "code": c, "name": s.get("name", c), "asset": s.get("asset"),
            "momentum": s.get(f"momentum_{look}d"), f"ma{ma_days}": s.get(f"ma{ma_days}"), "last": s.get("last"),
            "suggest": "derisk", "reserve_code": reserve_code, "reserve_name": reserve_name,
            "derisk_amount": amount, "actionable": not blocked, "blocked_reasons": blocked,
        })
    return out


def resolve_policy_number(profile, key, default, *, lo=None, hi=None):
    """Track C §5.2 权威「零值/缺失/非法」规则——绝不用 `v or default` 吞掉合法 0。

    返回 (value, status)：
      - 字段缺失 / None        → (default, "defaulted")
      - 合法数值（含 0）        → (v,       "ok")
      - 布尔/非数 / 越界        → (default, "invalid")   ← 不静默修正，交调用方记诊断
    """
    if key not in profile or profile.get(key) is None:
        return float(default), "defaulted"
    v = profile.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return float(default), "invalid"
    v = float(v)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return float(default), "invalid"
    return v, "ok"


def estimate_target_stress_drawdown(holdings, universe, shocks=None, default_shock=None):
    """按目标权重做简化压力测试；用于风险预算校准，不是预测。

    shocks/default_shock 缺省回退到模块表（保持旧调用与测试不变）；传入时用注入的假设（WS4 单一来源）。
    """
    shocks = ASSET_SHOCKS if shocks is None else shocks
    default_shock = DEFAULT_SHOCK if default_shock is None else default_shock
    contributions = []
    total = 0.0
    for h in holdings:
        code = str(h.get("code"))
        tw = float(h.get("target_weight", 0) or 0)
        asset = (universe.get(code) or {}).get("asset")
        shock = shocks.get(asset, default_shock)
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


# ── 积木式预期收益（building-block）：把"冻结假设"换成"锚在今天的利率与估值上的前瞻预期" ──
#   债券  = 当前国债到期收益率(YTM)            —— 起始收益率是债券长期回报最好的单一预测器（高置信·可测）
#   A股权益 = sleeve 中性估值锚 + 估值回归       —— 中性锚沿用 sleeve 假设(=PE 在历史中位时的回报)；
#            (PE 向历史中位回归的年化幅度)         估值回归用 per[code].valuation 的 pe vs pe_median（随估值呼吸）
#   QDII权益 = 美债YTM + 风险溢价(ERP)         —— 第3步：随美债利率呼吸；成长不假设跑赢核心(压近因偏误的纳指)。
#            （美股估值CAPE取不到→不假装锚定，显式打"估值未建模·当前偏高→偏乐观"旗标）
#   黄金     = 暂留 judgment                   —— 无现金流、本就锚不了(硬按近期涨幅锚=look-ahead陷阱)，诚实标低置信
#   分工：回测只管风险(波动/回撤/相关性)，收益走这套前瞻积木——不外推过去，锚在现在。
#   用途：① 周报「目标可行性」展示；② 逐只 expected/expected_conservative 已**驱动**「构建模型组合」的
#         权重选择（strategic.construct_strategic_portfolio 的 returns_by_code，替代冻结假设表）。
BB_REVERSION_YEARS = 10        # 估值向历史中位回归的摊销年限（CMA 惯用 7~10 年；非 20 年——估值不会用20年才回归）
BB_VAL_ADJ_CAP = 0.05          # 单只估值回归对年化的最大加减（防极端 PE 读数把估计放大）
BB_BOND_YTM_TENOR = "5年"      # 默认取 5 年期国债 YTM（511010=5年期国债ETF）；可在 strategy.yaml.expected_return 覆盖
BB_US_YTM_TENOR = "10年"       # QDII 权益的无风险锚：美债期限点（2年/5年/10年/30年）
BB_DEFAULT_ERP = 0.03          # QDII 权益默认风险溢价（成长与核心同值——不假设成长跑赢；偏保守以部分补偿未建模的高估值）
BB_YTM_CONSERVATIVE_HAIRCUT = 0.005   # 高置信YTM腿(债券)的保守折扣：远小于股票——起始YTM几乎就是持有到久期的回报，
                                      # 不确定性主要来自再投资/价格，对它套用股票级 3% 折扣不诚实。可在 strategy.yaml.expected_return.ytm_conservative_haircut 覆盖。
BB_BOND_ASSETS = ("bond",)
BB_VAL_REVERSION_ASSETS = VALUATION_APPLICABLE_ASSETS   # equity / equity_defensive / china_growth
BB_QDII_EQUITY_ASSETS = ("global_equity", "global_growth")   # 标普500 / 纳指（QDII，按美债+ERP锚）
BB_CONF_ZH = {"high": "高（可测）", "medium": "中（利率/估值锚）", "low": "低（judgment）"}


def valuation_reversion(current_pe, anchor_pe, years=BB_REVERSION_YEARS, cap=BB_VAL_ADJ_CAP):
    """估值回归对年化收益的贡献：PE 在 `years` 年内回到 anchor(历史中位) 的年化幅度。

    现贵(current>anchor)→负(逆风)；现便宜(current<anchor)→正(顺风)；中性→0。
    数据缺失/非法→0（不编数）。结果夹在 ±cap。纯函数、同输入同输出。
    """
    try:
        c, a, y = float(current_pe), float(anchor_pe), float(years)
    except (TypeError, ValueError):
        return 0.0
    if not (c > 0 and a > 0 and y > 0):
        return 0.0
    adj = (a / c) ** (1.0 / y) - 1.0
    return round(max(-cap, min(cap, adj)), 6)


YTM_STALE_LIMIT_DAYS = 30   # M7：YTM 缓存超过此天数 → 不再作锚（降级 assumption/低置信），与估值缓存同规约


def _ytm_cache_fallback(cache_path, tenor):
    """YTM 缓存回退（M7，2026-06-10 审查）：此前回退不限龄——半年前的缓存 YTM 仍标 available、
    债券腿仍 confidence=high 驱动构建排序。现限 {YTM_STALE_LIMIT_DAYS} 天并透出 stale_days；
    超限/无法判龄 → 返回 None（调用方降级 assumption/低置信，fail-closed）。"""
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("tenor") != tenor or not _num_ok(c.get("value"), lo=-1, hi=1):
            return None
        ref = str(c.get("fetched_at") or c.get("as_of") or "")[:10]
        try:
            stale_days = (date.today() - datetime.strptime(ref, "%Y-%m-%d").date()).days
        except Exception:  # noqa: BLE001
            return None
        if stale_days > YTM_STALE_LIMIT_DAYS:
            return None
        return float(c["value"]), {"available": True, "source": "cache", "value": float(c["value"]),
                                   "tenor": tenor, "as_of": c.get("as_of"), "stale_days": stale_days}
    except Exception:  # noqa: BLE001
        return None


def fetch_bond_ytm(tenor=BB_BOND_YTM_TENOR, retries=2, fallback=None, latest_session=None):
    """当前中债国债收益率曲线指定期限点位 → 债券桶前瞻收益锚(YTM)。失败回退缓存→assumption。

    返回 (ytm|None, status)。status={available, source('live'/'cache'/'assumption'), tenor, as_of, value}。
    缓存跳过：今天已拉过(fetched_at==today、同 tenor) → 用缓存、跳过重下。
    """
    cache_path = os.path.join(CACHE_DIR, "bond_ytm.json")
    today = date.today()
    if latest_session is not None and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                c = json.load(f)
            if c.get("fetched_at") == str(today) and c.get("tenor") == tenor and _num_ok(c.get("value"), lo=-1, hi=1):
                return float(c["value"]), {"available": True, "source": "cache",
                                           "value": float(c["value"]), "tenor": tenor, "as_of": c.get("as_of")}
        except Exception:  # noqa: BLE001
            pass
    err = None
    for _ in range(retries):
        try:
            start = (today - timedelta(days=21)).strftime("%Y%m%d")
            df = ak.bond_china_yield(start_date=start, end_date=today.strftime("%Y%m%d"))
            if df is not None and not df.empty and "曲线名称" in df.columns and tenor in df.columns:
                g = df[df["曲线名称"] == "中债国债收益率曲线"].dropna(subset=[tenor])
                if not g.empty:
                    row = g.iloc[-1]
                    ytm = round(float(row[tenor]) / 100.0, 6)   # 百分数→小数（1.45 → 0.0145）
                    as_of = str(pd.to_datetime(row["日期"]).date())
                    res = {"value": ytm, "tenor": tenor, "as_of": as_of}
                    try:
                        os.makedirs(CACHE_DIR, exist_ok=True)
                        with open(cache_path, "w", encoding="utf-8") as f:
                            json.dump({**res, "fetched_at": str(today)}, f, ensure_ascii=False)
                    except Exception:  # noqa: BLE001
                        pass
                    record_fetch_health("国债收益率", True)
                    return ytm, {"available": True, "source": "live", **res}
            err = "bad_response"
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.0)
    record_fetch_health("国债收益率", False, err)
    fb = _ytm_cache_fallback(cache_path, tenor)   # 回退缓存（限 30 天新鲜度，M7）
    if fb:
        return fb
    return fallback, {"available": False, "source": "assumption", "tenor": tenor, "value": fallback}


def fetch_us_treasury_yield(tenor=BB_US_YTM_TENOR, retries=2, latest_session=None):
    """当前美债收益率指定期限点位 → QDII 权益的无风险锚。实时→缓存(当天跳过)→None。

    返回 (yield|None, status)。status={available, source('live'/'cache'/None), tenor, as_of, value}。
    """
    cache_path = os.path.join(CACHE_DIR, "us_treasury_yield.json")
    col = f"美国国债收益率{tenor}"
    today = date.today()
    if latest_session is not None and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                c = json.load(f)
            if c.get("fetched_at") == str(today) and c.get("tenor") == tenor and _num_ok(c.get("value"), lo=-1, hi=1):
                return float(c["value"]), {"available": True, "source": "cache",
                                           "value": float(c["value"]), "tenor": tenor, "as_of": c.get("as_of")}
        except Exception:  # noqa: BLE001
            pass
    err = None
    for _ in range(retries):
        try:
            start = (today - timedelta(days=21)).strftime("%Y%m%d")
            df = ak.bond_zh_us_rate(start_date=start)
            if df is not None and not df.empty and col in df.columns:
                g = df.dropna(subset=[col])
                if not g.empty:
                    row = g.iloc[-1]
                    yld = round(float(row[col]) / 100.0, 6)   # 百分数→小数（4.56 → 0.0456）
                    as_of = str(pd.to_datetime(row["日期"]).date())
                    res = {"value": yld, "tenor": tenor, "as_of": as_of}
                    try:
                        os.makedirs(CACHE_DIR, exist_ok=True)
                        with open(cache_path, "w", encoding="utf-8") as f:
                            json.dump({**res, "fetched_at": str(today)}, f, ensure_ascii=False)
                    except Exception:  # noqa: BLE001
                        pass
                    record_fetch_health("美债收益率", True)
                    return yld, {"available": True, "source": "live", **res}
            err = "bad_response"
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.0)
    record_fetch_health("美债收益率", False, err)
    fb = _ytm_cache_fallback(cache_path, tenor)   # 回退缓存（限 30 天新鲜度，M7）
    if fb:
        return fb
    return None, {"available": False, "source": None, "tenor": tenor, "value": None}


def building_block_returns(holdings, universe, per, assumptions, bond_ytm=None, bond_ytm_status=None,
                           *, reversion_years=BB_REVERSION_YEARS, val_cap=BB_VAL_ADJ_CAP,
                           us_ytm=None, us_ytm_status=None, erp=None,
                           ytm_haircut=BB_YTM_CONSERVATIVE_HAIRCUT):
    """每只持仓的前瞻预期年化 = 积木拼出；返回 {blend, frozen_blend, blend_conservative, blocks:[...], ...}。

    债券→国债YTM；A股权益→中性锚 + 估值回归；QDII权益→美债YTM + 风险溢价(ERP，成长不假设跑赢核心，
    且标"美股估值未建模·偏乐观"旗标)；黄金等→judgment 假设(无现金流·难锚定，诚实低置信)。
    每块另给 `expected_conservative`（驱动构建优化器主排序键 gap=目标−保守）：高置信YTM腿套小折扣
    `ytm_haircut`，中/低置信腿把 sleeve 的保守折扣(returns−returns_conservative)平移到锚定中枢——
    债券不确定性远小于股票，不对 1.45% 的YTM 硬扣 3%。
    bond_ytm/us_ytm 由外部 fetch 注入，保持本函数纯。同输入同输出、可测、可复现。
    """
    returns = (assumptions or {}).get("returns") or {}
    default_return = (assumptions or {}).get("default_return", DEFAULT_EXPECTED_RETURN)
    returns_conservative = (assumptions or {}).get("returns_conservative") or {}
    haircut = (assumptions or {}).get("return_haircut", 0.03)   # sleeve 保守折扣缺失时的回退
    ytm_failed = bool(bond_ytm_status and bond_ytm_status.get("source") == "assumption")
    erp = erp or {}
    blocks, blend, frozen_blend, blend_conservative = [], 0.0, 0.0, 0.0
    for h in holdings:
        code = str(h.get("code"))
        tw = float(h.get("target_weight", 0) or 0)
        meta = universe.get(code) or {}
        asset = meta.get("asset")
        anchor = float(returns.get(asset, default_return))   # 冻结假设 = "估值中性时"的回报锚
        frozen_blend += tw * anchor
        sig = (per.get(code) or {}) if isinstance(per, dict) else {}
        val = sig.get("valuation") or {}
        block = {"code": code, "name": h.get("name", meta.get("name", code)),
                 "asset": asset, "weight": round(tw, 4), "anchor": round(anchor, 6),
                 "valuation_adj": 0.0, "ytm": None, "expected": round(anchor, 6),
                 "basis": "假设(judgment)", "confidence": "low"}
        if asset in BB_BOND_ASSETS and bond_ytm is not None and not ytm_failed:
            block.update(expected=round(float(bond_ytm), 6), ytm=round(float(bond_ytm), 6),
                         basis="当前国债YTM", confidence="high")
        elif asset in BB_BOND_ASSETS:
            block.update(basis="假设(YTM取数失败)", confidence="low")
        elif asset in BB_VAL_REVERSION_ASSETS:
            adj = valuation_reversion(val.get("pe"), val.get("pe_median"), reversion_years, val_cap)
            has_anchor = val.get("pe_median") is not None and val.get("pe") is not None
            block.update(valuation_adj=adj, expected=round(anchor + adj, 6),
                         basis=("中性锚+估值回归" if has_anchor else "中性锚(估值数据缺)"),
                         confidence=("medium" if has_anchor else "low"))
        elif asset in BB_QDII_EQUITY_ASSETS and us_ytm is not None:
            premium = float(erp.get(asset, BB_DEFAULT_ERP))
            block.update(expected=round(float(us_ytm) + premium, 6), us_rf=round(float(us_ytm), 6),
                         erp=round(premium, 6), basis="美债YTM+风险溢价", confidence="medium",
                         valuation_caveat="美股估值(CAPE)未建模·当前偏高→此数偏乐观")
        elif asset == "gold":
            block.update(basis="judgment(无现金流·难锚定)", confidence="low")
        # 保守锚定：高置信YTM腿小折扣；其余把 sleeve 折扣(returns−returns_conservative)平移到锚定中枢。
        spread = ytm_haircut if block["confidence"] == "high" else \
            round(anchor - returns_conservative.get(asset, round(anchor - haircut, 6)), 6)
        block["expected_conservative"] = round(block["expected"] - spread, 6)
        blend += tw * block["expected"]
        blend_conservative += tw * block["expected_conservative"]
        blocks.append(block)
    return {"blend": round(blend, 6), "frozen_blend": round(frozen_blend, 6),
            "blend_conservative": round(blend_conservative, 6), "blocks": blocks,
            "reversion_years": reversion_years,
            "bond_ytm": (round(float(bond_ytm), 6) if (bond_ytm is not None and not ytm_failed) else None),
            "bond_ytm_status": bond_ytm_status,
            "us_ytm": (round(float(us_ytm), 6) if us_ytm is not None else None),
            "us_ytm_status": us_ytm_status}


def expected_etf_return(holdings, universe, returns=None, default_return=None):
    """按目标权重 × 各 sleeve 假设年化，估 ETF 桶现实预期年化（非承诺，仅目标可行性刻度）。

    returns/default_return 缺省回退到模块表（保持旧调用与测试不变）；传入时用注入的假设（WS4 单一来源）。
    """
    returns = ASSET_EXPECTED_RETURN if returns is None else returns
    default_return = DEFAULT_EXPECTED_RETURN if default_return is None else default_return
    total = 0.0
    for h in holdings:
        code = str(h.get("code"))
        tw = float(h.get("target_weight", 0) or 0)
        asset = (universe.get(code) or {}).get("asset")
        total += tw * returns.get(asset, default_return)
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


# ── WS1：本周每只持仓 ETF 的「加仓/减仓/不动」理由（后端确定性纯函数，可测试、可复现、归档可重渲染）──

def _momentum_bucket(m):
    if m is None:
        return None
    if m >= 0.05:
        return "偏强"
    if m <= -0.05:
        return "偏弱"
    return "中性"


def _signal_momentum(signal):
    k = next((k for k in signal if isinstance(k, str) and k.startswith("momentum_")), None)
    return signal.get(k) if k else None


def valuation_state(signal):
    """估值三态（+无）：cheap/neutral/rich（有分位）| na（不适用）| missing（缺失·非中性）| None（无估值字段）。"""
    if signal.get("valuation"):
        return (signal["valuation"] or {}).get("tag")
    if signal.get("valuation_na"):
        return "na"
    if signal.get("valuation_accumulating"):
        return "accumulating"      # 自建分位积累中：只有 PE 水平、无分位 → 不触发 cheap/rich，绝不软化
    if "valuation_missing" in signal:
        return "missing"
    return None


def _signal_qualifiers(signal):
    """趋势/动量/估值三态的人话限定语（确定性、可复用）。估值严格区分三态，缺失绝不写'中性'。"""
    parts = []
    trend = signal.get("trend")
    if trend == "above":
        parts.append("价在 MA200 上方")
    elif trend == "below":
        parts.append("已跌破 MA200（趋势转弱，危机保险信号）")
    mb = _momentum_bucket(_signal_momentum(signal))
    if mb:
        parts.append(f"动量{mb}")
    vs = valuation_state(signal)
    if vs in ("cheap", "neutral", "rich"):
        pct = ((signal.get("valuation") or {}).get("percentile") or 0) * 100
        parts.append(f"估值分位{pct:.0f}%（{ {'cheap': '偏便宜', 'neutral': '估值中性', 'rich': '偏贵'}[vs] }）")
    elif vs == "na":
        parts.append("估值分位不适用（QDII/黄金/债券类）")
    elif vs == "missing":
        parts.append("估值数据缺失(非中性)")
    return parts


def explain_rebalance_action(row, signal, *, abs_thr_pp, rel_thr, min_trade, max_weekly):
    """为单只持仓 ETF 的 加仓/减仓/不动 生成人话理由 + 结构化 reason_factors。

    优先级：数据错误 > 被拦截 > 触发(add/trim) > 不动(hold)。error 行绝不使用买卖措辞。
    返回 (reason_str, reason_factors)。row=actionable_rebalance 行；signal=per[code]。纯函数、同输入同输出。
    """
    signal = signal or {}
    suggest = row.get("suggest")
    triggered = bool(row.get("triggered"))
    actionable = bool(row.get("actionable"))
    dev = float(row.get("deviation_pp") or 0)
    amount = row.get("approx_amount") or 0
    blockers = list(row.get("blocked_reasons") or [])
    vs = valuation_state(signal)
    factors = {
        "deviation_pp": round(dev, 2), "threshold_pp": abs_thr_pp, "rel_threshold": rel_thr,
        "suggest": suggest, "triggered": triggered, "actionable": actionable,
        "trend": signal.get("trend"), "momentum_bucket": _momentum_bucket(_signal_momentum(signal)),
        "valuation_state": vs, "blockers": blockers, "exec_quality": "none", "valuation_decel": False,
    }
    if signal.get("error"):
        factors["state"] = "no_data"
        return f"数据不足/拉取失败，本周不评估（{signal.get('error')}）", factors
    quals = _signal_qualifiers(signal)
    qual_txt = ("；" + "、".join(quals)) if quals else ""
    verb = {"add": "加仓", "trim": "减仓"}.get(suggest, "")
    if triggered and not actionable:
        why = "；".join(blockers) if blockers else "未通过执行门槛"
        return f"原信号建议{verb}（偏离 {dev:+.1f}pp），但被拦截：{why}{qual_txt}", factors
    if triggered and suggest in ("add", "trim"):
        base = f"偏离目标 {dev:+.1f}pp（超过阈值 {abs_thr_pp:.0f}pp 或相对 {rel_thr:.0%}）→ 建议{verb}约 ¥{amount:,.0f}"
        extra = ""
        if suggest == "add" and vs == "rich":
            pct = ((signal.get("valuation") or {}).get("percentile") or 0) * 100
            extra = f"；估值分位偏高（{pct:.0f}%），建议缓建/小额、分批靠近目标"
            factors["valuation_decel"] = True
        return base + qual_txt + extra, factors
    return f"未超过 5/25 阈值（偏离 {dev:+.1f}pp），维持当前仓位{qual_txt}", factors


def decelerate_add(row, signal, risk_profile):
    """WS5：对 rich 估值的触发加仓做有界软化（给出更小的【建议执行规模】）。

    只缩不放、仅 add、仅 valuation==rich；估值 na/missing/neutral/cheap 绝不软化（需真实分位）。
    就地给 row 加 action_mode='缓建' / soften_amount；**不改 approx_amount/deviation_pp、不翻转 actionable、不加 blocked**
    （它是元数据，不是闸门，不会破坏一笔合法再平衡）。返回 row。
    """
    if row.get("suggest") != "add" or not row.get("triggered"):
        return row
    if valuation_state(signal) != "rich":
        return row
    pct = (signal.get("valuation") or {}).get("percentile") or 0
    base, extreme = {"进取": (0.50, 0.33), "平衡": (0.40, 0.25), "保守": (0.30, 0.20)}.get(risk_profile, (0.40, 0.25))
    factor = extreme if pct >= 0.90 else base
    row["action_mode"] = "缓建"
    # L3：缓建金额也要整手化——approx_amount 已整手，按比例缩后折回整手（至少 1 手、不超过原金额），
    # 否则给出 0.x 手的不可执行数。lot_value=一手金额（price×100）。
    approx = float(row.get("approx_amount") or 0)
    raw = approx * factor
    lv = float(row.get("lot_value") or 0)
    if lv > 0 and approx > 0:
        lots = max(1, round(raw / lv))
        row["soften_amount"] = int(min(lots * lv, approx))
    else:
        row["soften_amount"] = round(raw)
    return row


def compute_rebalance_rows(holdings, mkt_vals, total, prices, *, abs_thr, rel_thr, rebal_ok, lot_size=LOT_SIZE):
    """每只持仓的再平衡触发 + 整手化（纯函数）：5/25 偏离触发；金额折整手；卖出按持仓整手封顶。

    返回 rebal 行列表（deviation_pp / triggered / suggest / approx_amount(整手) / lot_shares / lot_value / last）。
    不含执行门槛（数据/频率/熔断/最小金额/单周上限）——那些由 gate_rebalance_rows 施加，便于各自单测。
    """
    rows = []
    for h in holdings:
        code = str(h["code"])
        tw = float(h.get("target_weight", 0) or 0)
        cw = (mkt_vals.get(code, 0) / total) if total > 0 else 0.0
        dev = cw - tw
        triggered = (rebal_ok and total > 0
                     and (abs(dev) >= abs_thr or (tw > 0 and abs(dev) / tw >= rel_thr)))
        suggest = ("trim" if dev > 0 else "add") if triggered else "hold"
        raw_amount = abs(dev) * total if triggered else 0.0
        price = prices.get(code)
        # 整手化（A 方案）：把建议金额折到整手；不足一手 → lot_shares=0 → 后续诚实压制。
        lot_shares, lot_amount, lot_value = lots_for_amount(raw_amount, price, lot_size)
        # 卖出不能超过实际持仓（按整手封顶）。
        if triggered and suggest == "trim" and price and price > 0:
            held_lots = int(float(h.get("shares", 0) or 0)) // lot_size
            if lot_shares > held_lots * lot_size:
                lot_shares = held_lots * lot_size
                lot_amount = float(lot_shares) * price
        rows.append({
            "code": code, "name": h.get("name", code),
            "target_weight": round(tw, 4), "current_weight": round(cw, 4),
            "deviation_pp": round(dev * 100, 2), "triggered": bool(triggered),
            "suggest": suggest,
            "approx_amount": round(lot_amount, 0) if triggered else 0,
            "approx_amount_raw": round(raw_amount, 0) if triggered else 0,
            "lot_shares": int(lot_shares) if triggered else 0,
            "lot_value": round(lot_value, 0) if (triggered and lot_value) else None,
            "last": round(price, 4) if (price and price > 0) else None,
        })
    return rows


def gate_rebalance_rows(rebal, *, rebalance_blockers, freq_block_reason, freq_gated,
                        breaker_thr, min_trade, max_weekly, cash=None, add_only_blockers=None):
    """对再平衡行施加执行门槛（纯函数、确定性）：未触发 / 纪律拦截 / 频率闸（偏离达熔断阈值可跨越）/
    不足一手 / 最小金额 / 单周累计上限 / 现金充足（cash 给定时）。返回带 actionable + blocked_reasons 的行。

    单周上限按列表顺序累计已放行金额（前面放行的占额会挤掉后面的）。不调用 explain/decelerate（留给调用方）。
    F3-01：cash 给定时，可执行加仓合计不得超过「可用现金 + 本周可执行减仓回款」，超额加仓拦下并提示先卖出。
    M2：rebalance_blockers 双向拦（价不可信类）；add_only_blockers 只拦 suggest=="add"（组合超风险预算类——
    组合太险时减仓恰是理性动作，不拦）。
    """
    out = []
    weekly_used = 0.0
    for r in rebal:
        rr = dict(r)
        reasons = []
        triggered = bool(r.get("triggered"))
        breaker_hit = abs(float(r.get("deviation_pp") or 0)) >= breaker_thr * 100 - 1e-9
        if not triggered:
            reasons.append("未触发再平衡")
        if rebalance_blockers:
            reasons.extend(rebalance_blockers)
        if add_only_blockers and r.get("suggest") == "add":
            reasons.extend(add_only_blockers)
        if freq_block_reason and triggered and not breaker_hit:
            reasons.append(freq_block_reason)
        if triggered and breaker_hit and freq_gated:
            rr["circuit_breaker"] = True   # 已超熔断阈值，跨频率强制放行
        lot_ok = int(r.get("lot_shares") or 0) > 0
        if triggered and not lot_ok:
            lv = r.get("lot_value")
            reasons.append(
                f"不足一手：最小交易单位 100 份≈¥{lv:,.0f}，本次偏离对应金额小于一手，本周不动手"
                if lv else "不足一手（最小交易单位 100 份），本周不动手")
        if triggered and lot_ok and r["approx_amount"] < min_trade:
            reasons.append(f"金额低于最小交易门槛 {min_trade:.0f} 元")
        if triggered and lot_ok and max_weekly > 0 and weekly_used + r["approx_amount"] > max_weekly:
            reasons.append(f"超过单周交易上限 {max_weekly:.0f} 元")
        allowed = triggered and not reasons
        if allowed:
            weekly_used += r["approx_amount"]
        rr["actionable"] = bool(allowed)
        rr["blocked_reasons"] = reasons
        out.append(rr)
    # F3-01：可执行加仓不得超过「可用现金 + 本周可执行减仓回款」（按列表顺序，前面的加仓先占额）。
    #   减仓需先卖出释放资金的依赖在此显式体现：超额的加仓被拦，提示「需先卖出」，而非给出买不起的清单。
    if cash is not None:
        available = float(cash) + sum(float(o.get("approx_amount") or 0) for o in out
                                      if o.get("actionable") and o.get("suggest") == "trim")
        add_used = 0.0
        for o in out:
            if o.get("actionable") and o.get("suggest") == "add":
                amt = float(o.get("approx_amount") or 0)
                if add_used + amt > available + 1e-6:
                    o["actionable"] = False
                    o["blocked_reasons"] = list(o.get("blocked_reasons") or []) + [
                        f"现金不足：可用 ¥{available:,.0f}（含本周可执行减仓回款），需先卖出释放资金再加仓"]
                else:
                    add_used += amt
    return out


# 再平衡频率 → 跨交易日调仓批次的最短间隔天数。
# 同一自然日内可分多次成交，统一视为一个调仓批次；间隔从下一天开始计算。
REBAL_FREQ_DAYS = {"weekly": 0, "biweekly": 13, "monthly": 28, "quarterly": 84}
REBAL_FREQ_ZH = {"weekly": "每周", "biweekly": "每两周", "monthly": "每月", "quarterly": "每季"}


def latest_execution_date(repo_root):
    """journal/executions 里最近一笔成交的日期（按文件名 YYYY-MM-DD_ 前缀解析）；无则 None。纯读取、不联网。"""
    if not repo_root:
        return None
    d = os.path.join(repo_root, "journal", "executions")
    if not os.path.isdir(d):
        return None
    best = None
    for fn in os.listdir(d):
        if not fn.endswith(".json") or len(fn) < 10:
            continue
        try:
            dt = datetime.strptime(fn[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if best is None or dt > best:
            best = dt
    return best


def frequency_gate_state(check_freq, last_exec_date, today):
    """再平衡频率闸状态（纯函数）：返回 (min_gap_days, days_since, freq_gated)。

    weekly→min_gap 0（不额外限制）；其它档要求跨日后的新调仓批次距上次成交 ≥ min_gap 天。
    days_since==0 表示同一自然日内继续成交，属于同一批次，不触发频率闸。
    无上次成交记录→不闸（days_since=None）。
    """
    min_gap = REBAL_FREQ_DAYS.get(str(check_freq).lower(), 0)
    days_since = (today - last_exec_date).days if last_exec_date else None
    gated = bool(min_gap > 0 and days_since is not None and 0 < days_since < min_gap)
    return min_gap, days_since, gated


def warm_caches(strategy_path=None, portfolio_path=None):
    """静默预热全部数据缓存（行情/估值/收益率），不算信号、不写 signals.json。

    目的：把"决策时现拉"变成"决策时读定稿缓存"——驾驶舱启动时后台跑一次，
    网络抖动就不会撞上生成周报的时刻。各取数器自带缓存跳过：数据已定稿则零网络请求。
    任何一步失败都吞掉（健康账本 fetch_health.json 会记下）；返回摘要 dict 供日志/排查。"""
    summary = {"prices_total": 0, "prices_refreshed": 0, "valuations": 0, "ytm": 0}
    try:
        repo_root = find_repo_root(HERE)
        strategy_path = strategy_path or (os.path.join(repo_root, "strategy.yaml") if repo_root else None)
        portfolio_path = portfolio_path or (os.path.join(repo_root, "portfolio.yaml") if repo_root else None)
        if not strategy_path or not os.path.exists(strategy_path):
            return summary
        # 用底层 load_yaml 而非 load_config_yaml：后者解析失败会 die() 整个进程，
        # 而预热是尽力而为的后台动作——配置坏了就跳过，留给正式生成信号时给出友好报错
        strat = load_yaml(strategy_path) or {}
        port = (load_yaml(portfolio_path) or {}) \
            if portfolio_path and os.path.exists(portfolio_path) else {}
    except Exception:  # noqa: BLE001
        return summary
    latest_session = _latest_completed_session()
    uni = strat.get("universe") or []
    codes = []
    for it in (port.get("holdings") or []) + uni + (strat.get("watchlist") or []):
        c = str((it or {}).get("code") or "")
        if c and c not in codes:
            codes.append(c)
    summary["prices_total"] = len(codes)
    stale = [c for c in codes if not _is_cache_current(c, latest_session)]
    if stale:
        prefetch_westock(stale)
        for c in stale:
            try:
                df, src = fetch_hist(c, latest_session=latest_session)
                if df is not None and src in ("westock", "live"):
                    summary["prices_refreshed"] += 1
            except Exception:  # noqa: BLE001
                pass
    F = strat.get("factors") or {}
    fv = F.get("valuation") or {}
    if fv.get("enabled", True):
        try:
            vyears = float(fv.get("lookback_years") or 5)
        except Exception:  # noqa: BLE001
            vyears = 5.0
        for u in uni:
            try:
                if u.get("index") or u.get("valuation_proxy"):
                    v, _st = fetch_valuation_pct(u.get("index") or u["valuation_proxy"], vyears,
                                                 latest_session=latest_session)
                elif u.get("valuation_csindex"):
                    v, _st = fetch_valuation_csindex(u["valuation_csindex"], vyears,
                                                     index_name=u.get("name"), latest_session=latest_session)
                else:
                    continue
                if v:
                    summary["valuations"] += 1
            except Exception:  # noqa: BLE001
                pass
    for fn in (fetch_bond_ytm, fetch_us_treasury_yield):
        try:
            _val, st = fn(latest_session=latest_session)
            if st.get("available"):
                summary["ytm"] += 1
        except Exception:  # noqa: BLE001
            pass
    return summary


def main():
    ap = argparse.ArgumentParser(description="周度信号引擎")
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--portfolio", default=None)
    ap.add_argument("--out", default=os.path.join(HERE, "signals.json"))
    ap.add_argument("--warm-cache", action="store_true",
                    help="只预热数据缓存（行情/估值/收益率），不生成信号——盘后/启动时静默刷新用")
    args = ap.parse_args()
    if args.warm_cache:
        print(json.dumps({"ok": True, **warm_caches(args.strategy, args.portfolio)}, ensure_ascii=False))
        return

    repo_root = find_repo_root(HERE)
    strategy_path = args.strategy or (os.path.join(repo_root, "strategy.yaml") if repo_root else None)
    portfolio_path = args.portfolio or (os.path.join(repo_root, "portfolio.yaml") if repo_root else None)
    if not strategy_path or not os.path.exists(strategy_path):
        die("找不到 strategy.yaml，请用 --strategy 指定路径")
    if not portfolio_path or not os.path.exists(portfolio_path):
        die("找不到 portfolio.yaml，请用 --portfolio 指定路径")

    strat = load_config_yaml(strategy_path, "strategy.yaml")
    port = load_config_yaml(portfolio_path, "portfolio.yaml")
    investor_profile = load_investor_profile(repo_root)

    errs = validate_strategy(strat) + validate_config(port, strat)
    if errs:
        die("配置校验未通过，请先修正 strategy.yaml / portfolio.yaml：\n  - " + "\n  - ".join(errs))
    report_progress("读取配置与校验", step=1, total=7)

    assumptions = load_assumptions(strat)   # WS4：收益/冲击假设单一来源（含 strategy.yaml 覆盖）
    F = strat["factors"]
    uni = {str(u["code"]): u for u in strat["universe"]}
    ma_days = int(F["trend_filter"]["ma_days"])
    look = int(F["momentum"]["lookback_days"])
    vyears = float(F["valuation"]["lookback_years"])
    cheap = float(F["valuation"]["cheap_pct"])
    rich = float(F["valuation"]["rich_pct"])
    abs_thr = float(F["rebalance"]["abs_threshold_pp"]) / 100.0
    rel_thr = float(F["rebalance"]["rel_threshold"])
    check_freq = str((F["rebalance"]).get("check_frequency", "weekly")).lower()
    if check_freq not in REBAL_FREQ_DAYS:
        check_freq = "weekly"
    breaker_thr = float((F["rebalance"]).get("circuit_breaker_pp", 15)) / 100.0
    RC = strat.get("risk_controls") or {}
    min_trade = float(RC.get("min_trade_amount", 0) or 0)
    max_weekly = float(RC.get("max_weekly_trade_amount", 0) or 0)
    allow_cache_trade = bool(RC.get("allow_trade_with_cache", False))

    holdings = port.get("holdings", []) or []
    watchlist = strat.get("watchlist") or []
    cash = float(port.get("cash", 0) or 0)
    today = date.today()

    # #1 缓存跳过：缓存已达最近已收盘交易日的 code 数据已定稿 → 不必再拉；只对"落后"的 code 跑 npx 批量预取
    latest_session = _latest_completed_session()
    all_codes = [str(h.get("code")) for h in holdings] + [str(w.get("code")) for w in watchlist]
    stale_codes = [c for c in all_codes if not _is_cache_current(c, latest_session)]
    report_progress("批量预取行情", f"{len(stale_codes)} 只待更新" if stale_codes else "缓存已是最新，跳过拉取",
                    step=2, total=7)
    prefetch_westock(stale_codes)          # 全部已定稿 → stale_codes 空 → 跳过 npx 子进程

    def build_signal(item, fallback=None):
        """生成单只 ETF 的展示信号；不包含仓位/交易动作。"""
        code = str(item["code"])
        meta = fallback or item
        name = item.get("name") or meta.get("name") or code
        df, src = fetch_hist(code, latest_session=latest_session)
        if df is None or len(df) < ma_days + 5:
            sig = {
                "name": name,
                "asset": meta.get("asset"),
                "role": item.get("role"),
                "note": item.get("note"),
                "error": "数据不足或拉取失败",
            }
            return code, sig, None, None, None, None
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
            v, vst = fetch_valuation_pct(meta["index"], vyears, latest_session=latest_session)
            if v:
                sig["valuation"] = {**v, "tag": _val_tag(v["percentile"], cheap, rich)}
            else:
                sig["valuation_missing"] = vst
        elif F["valuation"]["enabled"] and meta.get("valuation_proxy"):
            # 精确指数无长历史 PE 源（创业板指）→ 用强相关代理指数（创业板50）算分位，显式标"代理·近似"
            v, vst = fetch_valuation_pct(meta["valuation_proxy"], vyears, latest_session=latest_session)
            if v:
                sig["valuation"] = {**v, "tag": _val_tag(v["percentile"], cheap, rich),
                                    "proxy": meta["valuation_proxy"]}
            else:
                sig["valuation_missing"] = vst
        elif F["valuation"]["enabled"] and meta.get("valuation_csindex"):
            # csindex 官方按日自建累积（科创50/红利低波）；历史不足 → 只给 PE 水平、不冒充分位
            v, vst = fetch_valuation_csindex(meta["valuation_csindex"], vyears, index_name=name,
                                             latest_session=latest_session)
            if v and v.get("percentile") is not None:
                sig["valuation"] = {**v, "tag": _val_tag(v["percentile"], cheap, rich)}
            elif v:
                sig["valuation_accumulating"] = v
            else:
                sig["valuation_missing"] = vst
        else:
            # A股权益但尚未接入可用估值源 → 如实标缺失，绝不当中性
            vst = {"available": False, "source": None, "reason": "index_not_configured"}
            sig["valuation_missing"] = vst
        prov = {"source": src, "as_of": str(as_of), "stale_days": (today - as_of).days}
        return code, sig, last, prov, vst, close.tolist()

    def as_of_summary_from(provenance_map):
        as_ofs = sorted(p["as_of"] for p in provenance_map.values())
        if not as_ofs:
            return None, None, "无"
        as_of_min = as_ofs[0]
        as_of_max = as_ofs[-1]
        summary = as_of_min if as_of_min == as_of_max else f"{as_of_min} 至 {as_of_max}"
        return as_of_min, as_of_max, summary

    # #2 并行：每只 ETF 的取数(日线+估值)都是 I/O 等待 → 并行后墙钟≈最慢的一只，而非逐只相加。
    #   线程安全：build_signal 只读共享态(_WESTOCK_HIST/config)，写的是各自独立的缓存文件，无竞争。
    per, prices, provenance, valuation_status, closes_by_code = {}, {}, {}, {}, {}
    report_progress("逐只取数与信号", f"持仓 {len(holdings)} 只", step=3, total=7)
    _sig_done = {"n": 0}
    _sig_lock = threading.Lock()

    def _build_hold_signal(h):
        res = build_signal(h, uni.get(str(h["code"]), {}))
        with _sig_lock:
            _sig_done["n"] += 1
            n = _sig_done["n"]
        report_progress("逐只取数与信号", f"持仓 {n}/{len(holdings)} 只完成", step=3, total=7)
        return res

    with ThreadPoolExecutor(max_workers=min(8, len(holdings) or 1)) as ex:
        hold_results = list(ex.map(_build_hold_signal, holdings))
    for sig_code, sig, last, prov, vst, closes in hold_results:
        if last is not None:
            prices[sig_code] = last
            provenance[sig_code] = prov
        if closes:
            closes_by_code[sig_code] = closes
        if vst is not None:
            valuation_status[sig_code] = vst
        per[sig_code] = sig

    missing = [str(h["code"]) for h in holdings if str(h["code"]) not in prices]
    grade, rebal_ok, max_stale = grade_data(missing, provenance)
    used_cache = any(p["source"] == "cache" for p in provenance.values())
    as_of_min, as_of_max, as_of_summary = as_of_summary_from(provenance)

    watch_signals, watch_prices, watch_provenance = {}, {}, {}
    report_progress("观察池取数", f"{len(watchlist)} 只", step=4, total=7)
    with ThreadPoolExecutor(max_workers=min(8, len(watchlist) or 1)) as ex:
        watch_results = list(ex.map(build_signal, watchlist))
    for code, sig, last, prov, vst, _ in watch_results:
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
    target_stress_drawdown, stress_contributions = estimate_target_stress_drawdown(
        holdings, uni, assumptions["shocks"], assumptions["default_shock"])
    max_acceptable_drawdown, _mdd_status = resolve_policy_number(
        investor_profile, "max_acceptable_drawdown", 0.15, lo=0.0, hi=0.80)   # Track C §5.2：合法 0% 保留
    # 全组合口径：场外稳健桶按 0 冲击纳入分母，压力回撤折算到整个组合（稳健桶是安全垫）。
    stable_outside = float(investor_profile.get("stable_assets_outside", 0) or 0)
    whole_portfolio_value = total + stable_outside
    whole_portfolio_stress_drawdown = whole_portfolio_stress(target_stress_drawdown, total, stable_outside)
    # 风险预算闸门按"全组合"压力回撤评估，而非只看 ETF 桶（否则稳健桶的缓冲被忽略）。
    # B-1（F2-01）：基准用"计划满仓"(planned_etf_capital)而非当前实投——少额真金期实投极小，
    #   当前口径会把全组合尾部显示成接近 0、让硬闸几乎永不触发；决策相关的是"把计划资金投进去后的尾部"。
    #   无 planned 值时退回当前口径（行为不变）。
    planned_etf = float(investor_profile.get("planned_etf_capital", 0) or 0)
    risk_basis_value = planned_etf if planned_etf > 0 else total
    whole_portfolio_stress_at_planned = whole_portfolio_stress(target_stress_drawdown, risk_basis_value, stable_outside)
    risk_budget_breached = whole_portfolio_stress_at_planned > max_acceptable_drawdown

    # §0C #1 多情景历史压力：用据真实峰谷标定的危机向量算"若 20XX 重演"的最坏回撤（仅诚实展示、不改硬闸）。
    historical_scenarios = load_historical_scenarios(strat)
    scenario_results, worst_scenario = estimate_stress_scenarios(
        holdings, uni, historical_scenarios, assumptions["default_shock"])
    worst_etf_drawdown = worst_scenario["etf_drawdown"] if worst_scenario else target_stress_drawdown
    whole_worst_scenario_drawdown = whole_portfolio_stress(worst_etf_drawdown, total, stable_outside)
    # 决策相关口径：按"计划满仓"(planned_etf_capital)折算，而非当前实投——少额真金期实投极小，
    #   当前口径会把尾部显示成接近 0、误导"该不该把计划资金投进去"。两个口径都给。
    whole_worst_at_planned = (whole_portfolio_stress(worst_etf_drawdown, planned_etf, stable_outside)
                              if planned_etf > 0 else whole_worst_scenario_drawdown)
    scenario_budget_breached = whole_worst_at_planned > max_acceptable_drawdown
    worst_scenario_note = (
        f"最坏历史情景「{worst_scenario['name']}」重演 → ETF 桶约 −{worst_etf_drawdown * 100:.0f}%；"
        f"按计划满仓折算全组合约 −{whole_worst_at_planned * 100:.1f}%，"
        f"{'击穿' if scenario_budget_breached else '未击穿'}可接受回撤 {max_acceptable_drawdown * 100:.0f}%"
        "（当前实投占比小，故当前口径尾部更小；此处按计划满仓给决策相关值）"
    ) if worst_scenario else None

    rebal = compute_rebalance_rows(holdings, mkt_vals, total, prices,
                                   abs_thr=abs_thr, rel_thr=rel_thr, rebal_ok=rebal_ok)

    # 闸门分两类（M1/M2，2026-06-10 审查）：
    #   price_blockers「价不可信」（数据过旧/缓存）→ 双向拦：买卖金额都基于不可信价格，都不能执行；
    #   risk_breach_msg「组合超风险预算」→ 只拦加仓：组合太险时理性动作恰是减仓，拦 trim 是方向性错误。
    price_blockers = []
    if not rebal_ok:
        price_blockers.append("数据质量不足，禁止交易动作")
    if used_cache and not allow_cache_trade:
        price_blockers.append("行情包含缓存，risk_controls 不允许据此交易")
    risk_breach_msg = None
    if risk_budget_breached:
        risk_breach_msg = (
            f"按计划满仓口径全组合压力回撤约 {whole_portfolio_stress_at_planned * 100:.1f}%，超过可接受回撤 "
            f"{max_acceptable_drawdown * 100:.1f}%（超预算只拦加仓；减仓不受此限）"
        )
    discipline_blockers = price_blockers + ([risk_breach_msg] if risk_breach_msg else [])
    rebalance_blockers = list(price_blockers)
    if first_funding_eligible:
        rebalance_blockers.append("0持仓账户使用首次建仓预览，不直接执行再平衡")
    # 再平衡频率闸：同一自然日多次成交视为同一批次；跨日后，低频档（双周/月/季）
    # 要求距上次成交满 min_gap_days 才开启新一轮再平衡；
    # 但任一品种偏离 ≥ circuit_breaker_pp 时（崩盘级漂移）无视频率强制放行（仍受数据/金额/单周上限约束）。
    last_exec_date = latest_execution_date(repo_root)
    min_gap_days, days_since_rebal, freq_gated = frequency_gate_state(check_freq, last_exec_date, today)
    freq_block_reason = (
        f"未到再平衡周期（{REBAL_FREQ_ZH[check_freq]}）：距上次成交 {days_since_rebal} 天 < {min_gap_days} 天，"
        "未达熔断阈值的偏离本次不动手"
    ) if freq_gated else None

    actionable_rebalance = gate_rebalance_rows(
        rebal, rebalance_blockers=rebalance_blockers, freq_block_reason=freq_block_reason,
        freq_gated=freq_gated, breaker_thr=breaker_thr, min_trade=min_trade, max_weekly=max_weekly,
        cash=cash, add_only_blockers=[risk_breach_msg] if risk_breach_msg else None)
    for rr in actionable_rebalance:
        reason_str, reason_factors = explain_rebalance_action(
            rr, per.get(str(rr["code"]), {}),
            abs_thr_pp=abs_thr * 100, rel_thr=rel_thr, min_trade=min_trade, max_weekly=max_weekly)
        rr["action_reason"] = reason_str
        rr["reason_factors"] = reason_factors
        decelerate_add(rr, per.get(str(rr["code"]), {}), strat.get("risk_profile"))

    first_deploy = 0.0
    first_orders = []
    first_actual = 0.0
    first_pre_gate_actual = 0.0
    if first_funding_eligible:
        # 固定单周上限（非现金百分比）：可投 = min(现金, 单周上限)；上限<=0 视为不限速。
        # 资金分批到账期由"到账节奏"本身做平滑，故不再额外按现金% 二次节流（详见 DEPLOYMENT_REDESIGN.md）。
        cap = max_weekly if max_weekly > 0 else cash
        first_deploy = min(cash, cap)
        first_orders, first_actual = first_funding_orders(
            holdings, prices, first_deploy, current_values=None, min_trade=min_trade)
        first_pre_gate_actual = first_actual
        if discipline_blockers:
            # 价不可信 / 超风险预算 → 首次建仓同样不执行（首次建仓本质是加仓）。
            first_actual = 0.0
            for o in first_orders:
                o["actionable"] = False
                o["blocked_reasons"] = list(discipline_blockers) + o["blocked_reasons"]
    first_funding_plan = {
        "is_zero_position": bool(is_zero_position),
        "eligible": bool(first_funding_eligible),
        "cash": round(cash, 2),
        "weekly_cap": round(max_weekly, 0),
        "planned_deploy_amount": round(first_deploy, 0),
        "pre_gate_estimated_deploy_amount": round(first_pre_gate_actual, 0),
        "estimated_deploy_amount": round(first_actual, 0),
        "blocked_deploy_amount": round(max(first_pre_gate_actual - first_actual, 0), 0),
        "estimated_unallocated": round(max(first_deploy - first_actual, 0), 0),
        "remaining_cash_after_execution": round(max(cash - first_actual, 0), 0),
        "orders": first_orders,
        "notes": [
            "仅用于首次试仓预览，不自动下单",
            "观察池不参与首次建仓",
            "份额按 100 份一手粗略估算，实际以下单页面为准",
        ],
    }
    first_funding_plan["schedule"] = build_first_funding_schedule(
        holdings, prices, cash, max_weekly, min_trade
    ) if first_funding_eligible else []
    reconcile_first_funding_plan(first_funding_plan)

    target_annual_return, _tar_status = resolve_policy_number(
        investor_profile, "target_annual_return", 0.05, lo=0.0, hi=0.30)       # Track C §5.2：合法 0% 保留
    etf_expected_return = expected_etf_return(
        holdings, uni, assumptions["returns"], assumptions["default_return"])  # 冻结假设口径（保留作对比）
    # 积木式前瞻预期：债券=当前国债YTM、A股权益=中性锚+估值回归（锚在今天的利率/估值，会"呼吸"）。
    er_cfg = (strat.get("expected_return") or {})
    bb_tenor = er_cfg.get("bond_ytm_tenor") or BB_BOND_YTM_TENOR
    bb_years = (er_cfg.get("valuation_reversion_years")
                if _num_ok(er_cfg.get("valuation_reversion_years"), lo=1, hi=50) else BB_REVERSION_YEARS)
    bb_cap = er_cfg.get("valuation_adj_cap") if _num_ok(er_cfg.get("valuation_adj_cap"), lo=0, hi=1) else BB_VAL_ADJ_CAP
    bond_fallback = assumptions["returns"].get("bond", assumptions["default_return"])
    report_progress("收益锚定取数", "国债/美债 YTM 与估值锚", step=5, total=7)
    bond_ytm, bond_ytm_status = fetch_bond_ytm(bb_tenor, fallback=bond_fallback, latest_session=latest_session)
    # 第3步：QDII 权益 = 美债YTM + ERP（随美债利率呼吸）。美债取数失败→QDII 回退 sleeve 假设。
    us_tenor = er_cfg.get("us_ytm_tenor") or BB_US_YTM_TENOR
    bb_erp = er_cfg.get("equity_risk_premium") if isinstance(er_cfg.get("equity_risk_premium"), dict) else {}
    bb_ytm_hc = er_cfg.get("ytm_conservative_haircut") if _num_ok(er_cfg.get("ytm_conservative_haircut"), lo=0, hi=1) else BB_YTM_CONSERVATIVE_HAIRCUT
    us_ytm, us_ytm_status = fetch_us_treasury_yield(us_tenor, latest_session=latest_session)
    anchored = building_block_returns(holdings, uni, per, assumptions, bond_ytm, bond_ytm_status,
                                      reversion_years=bb_years, val_cap=bb_cap,
                                      us_ytm=us_ytm, us_ytm_status=us_ytm_status, erp=bb_erp,
                                      ytm_haircut=bb_ytm_hc)
    # B-3：相关性诊断 + 市场 regime（诚实披露，不改硬闸）。weights 用目标权重(与线性压力同口径)。
    target_weights = {str(h["code"]): float(h.get("target_weight", 0) or 0) for h in holdings}
    correlation_diag = correlation_diagnostic(closes_by_code, target_weights)
    regime = regime_state(holdings, per)
    risk_budget = {
        "target_annual_return": target_annual_return,
        "target_annual_profit": round(total * target_annual_return, 2),       # 针对 ETF 桶
        "expected_etf_return": round(etf_expected_return, 4),                  # 冻结假设口径（对比用·非承诺）
        "expected_target_gap": round(target_annual_return - etf_expected_return, 4),
        # 积木式锚定口径（前瞻：债券YTM + A股估值回归）——这才是相对可信、随今天利率/估值呼吸的数；
        # 同口径已驱动「构建模型组合」的权重选择（见 app._run_construct → strategic.construct_strategic_portfolio）。
        "expected_return_anchored": round(anchored["blend"], 4),
        "expected_return_anchored_conservative": round(anchored["blend_conservative"], 4),  # 保守锚定（构建主排序键同口径）
        "expected_return_frozen": round(anchored["frozen_blend"], 4),          # == expected_etf_return（同源校验）
        "expected_target_gap_anchored": round(target_annual_return - anchored["blend"], 4),
        "expected_return_blocks": anchored["blocks"],                          # 逐只：anchor/估值回归/YTM/expected/出处/置信
        "bond_ytm": anchored["bond_ytm_status"],                              # {value, tenor, as_of, source}
        "us_ytm": anchored["us_ytm_status"],                                  # QDII 权益的美债无风险锚
        "expected_return_reversion_years": anchored["reversion_years"],
        "max_acceptable_drawdown": max_acceptable_drawdown,                    # 全组合口径
        "max_acceptable_loss": round(whole_portfolio_value * max_acceptable_drawdown, 2),
        # Track C §5.2：策略输入解析状态（ok/defaulted/invalid）——合法 0% 不再被默认值吞掉
        "policy_inputs": {"max_acceptable_drawdown": _mdd_status, "target_annual_return": _tar_status},
        # ETF 桶自身口径（保留，标注；勿与全组合混淆）
        "target_portfolio_stress_drawdown": round(target_stress_drawdown, 4),
        "target_portfolio_stress_loss": round(total * target_stress_drawdown, 2),
        "etf_portfolio_value": round(total, 2),
        # 全组合口径（含场外稳健桶安全垫）
        "stable_assets_outside": round(stable_outside, 2),
        "whole_portfolio_value": round(whole_portfolio_value, 2),
        "whole_portfolio_stress_drawdown": round(whole_portfolio_stress_drawdown, 4),
        "whole_portfolio_stress_loss": round(whole_portfolio_value * whole_portfolio_stress_drawdown, 2),
        # B-1：硬闸实际评估基准（计划满仓口径）——breached 据此判定，而非当前实投
        "whole_portfolio_stress_drawdown_at_planned": round(whole_portfolio_stress_at_planned, 4),
        "risk_budget_basis": "planned" if planned_etf > 0 else "current",
        "stress_contributions": stress_contributions,
        "breached": bool(risk_budget_breached),
        # §0C #1 历史危机多情景（据真实峰谷标定，仅诚实展示尾部，不改硬闸 `breached`）
        "historical_scenarios": scenario_results,
        "worst_scenario": (worst_scenario or {}).get("name"),
        "worst_scenario_window": (worst_scenario or {}).get("window"),
        "worst_scenario_etf_drawdown": round(worst_etf_drawdown, 4),
        "whole_portfolio_worst_scenario_drawdown": round(whole_worst_scenario_drawdown, 4),
        "whole_portfolio_worst_scenario_loss": round(whole_portfolio_value * whole_worst_scenario_drawdown, 2),
        "whole_portfolio_worst_scenario_drawdown_at_planned": round(whole_worst_at_planned, 4),
        "scenario_breached": bool(scenario_budget_breached),   # 按计划满仓口径
        "worst_scenario_note": worst_scenario_note,
        # B-3（F1-03）：相关性诊断（有效风险来源数/平均相关性/组合波动）+ 市场 regime（权益跌破 MA200 广度）
        "correlation": correlation_diag,
        "regime": regime,
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
        "allow_trade_with_cache": allow_cache_trade,
        "trade_allowed": not discipline_blockers,
        "blocked_reasons": discipline_blockers,
        "rebalance_blocked_reasons": rebalance_blockers,
        "check_frequency": check_freq,
        "rebalance_min_gap_days": min_gap_days,
        "days_since_last_rebalance": days_since_rebal,
        "frequency_gated": bool(freq_gated),
        "circuit_breaker_pp": round(breaker_thr * 100, 2),
        "preflight_checks": build_preflight_checks(
            grade, rebal_ok, used_cache, allow_cache_trade, holdings, per,
            min_trade, max_weekly, is_zero_position,
            risk_budget_breached, whole_portfolio_stress_at_planned, max_acceptable_drawdown,
            strategic_policy=strat.get("strategic_policy"), regime=regime,
        ),
    }

    equity_assets = ("equity", "equity_defensive", "global_equity", "global_growth", "china_growth")
    eq = [(c, s.get(f"momentum_{look}d")) for c, s in per.items()
          if isinstance(s, dict) and s.get("asset") in equity_assets
          and s.get(f"momentum_{look}d") is not None]
    eq.sort(key=lambda x: x[1], reverse=True)
    momentum_rank = [{"code": c, "name": per[c]["name"], "momentum": v} for c, v in eq]

    # 危机保险（§0C #4 升级）：权益跌破 MA200 → 不再只是提醒，而是**具体减仓建议**（移到债券）+ 回测量化的
    #   回撤差（"不动手会多扛约 X% 回撤"）。仍是建议、人确认、不自动下单——把回测里趋势过滤的好处明牌给所有者。
    trend_protection = load_trend_protection(strat)
    trend_alerts = build_trend_derisk(per, holdings, uni, mkt_vals, equity_assets, ma_days, look,
                                      min_trade=min_trade, benefit=trend_protection,
                                      discipline_blockers=price_blockers)

    # ── Track B Phase A：影子战术建议（只读、绝不进入 actionable_rebalance）──
    tactical_shadow = None
    try:
        tcfg = tactical.load_tactical_config(strat)
        if tcfg.get("reserve_asset") and tcfg.get("mode") in ("shadow", "advisory"):
            stable_o = float(investor_profile.get("stable_assets_outside", 0) or 0)
            planned = float(investor_profile.get("planned_etf_capital", 0) or 0)
            etf_share = planned / (planned + stable_o) if planned > 0 and (planned + stable_o) > 0 else 1.0
            tassets = []
            for h in holdings:
                code = str(h["code"])
                if code not in closes_by_code:
                    continue
                asset = (uni.get(code) or {}).get("asset")
                s = per.get(code) or {}
                tassets.append({
                    "code": code, "asset": asset,
                    "strategic_weight": float(h.get("target_weight", 0) or 0),
                    "closes": closes_by_code[code],
                    "valuation_percentile": (s.get("valuation") or {}).get("percentile"),
                    "valuation_status": valuation_status.get(code),
                    "provenance": provenance.get(code),
                    "shock": assumptions["shocks"].get(asset, assumptions["default_shock"]),
                })
            if len(tassets) >= 2:
                try:
                    import reports as _reports
                    prior_states = _reports.prior_tactical_states()
                except Exception:  # noqa: BLE001
                    prior_states = {}
                tactical_shadow = tactical.compute_shadow(
                    tassets, strat.get("risk_profile") or "平衡", tcfg["reserve_asset"],
                    etf_share=etf_share, max_whole_stress=max_acceptable_drawdown,
                    cfg=tcfg, prior_states=prior_states)
                fp_src = "|".join(f"{a['code']}:{(per.get(a['code']) or {}).get('last')}:"
                                  f"{(per.get(a['code']) or {}).get('as_of')}" for a in tassets)
                tactical_shadow["input_fingerprint"] = "sha256:" + hashlib.sha256(
                    (fp_src + "|model=tactical-v1").encode("utf-8")).hexdigest()[:16]
                tactical_shadow["enabled"] = bool(tcfg.get("enabled"))
                cur_w = {c: (mkt_vals.get(c, 0) / total if total > 0 else 0) for c in mkt_vals}
                tactical_shadow["actions"] = tactical.tactical_actions(
                    tactical_shadow, cur_w, total, min_trade=min_trade, max_weekly=max_weekly,
                    abs_thr_pp=tcfg["actions"]["tactical_abs_threshold_pp"],
                    rel_thr=tcfg["actions"]["tactical_rel_threshold"],
                    struct_abs_pp=abs_thr * 100, struct_rel=rel_thr,
                    strategic_weights={a["code"]: a["strategic_weight"] for a in tassets})
                tactical_shadow["note"] = (
                    "战术动作已接入调仓（advisory）。" if tcfg.get("mode") == "advisory"
                    else "影子战术建议：只读、不构成本周可执行动作；通过 §13.5 验收前不接入调仓。")
    except Exception as _e:  # noqa: BLE001
        tactical_shadow = {"error": f"影子战术计算失败：{_e}"}

    snapshot_material = {
        code: {k: v for k, v in sig.items() if k != "source"}
        for code, sig in sorted(per.items())
    }
    snapshot_payload = json.dumps(
        {"signal_as_of": as_of_summary, "signals": snapshot_material},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    snapshot_id = "sha256:" + hashlib.sha256(snapshot_payload.encode("utf-8")).hexdigest()[:16]

    out = {
        "generated_for": str(today),
        "signal_as_of": as_of_summary,
        "execution_checked_at": None,
        "snapshot_id": snapshot_id,
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
        # A（状态指纹）：记录本次信号据以计算的真实持仓（现金 + 每只份额），供前端比对 portfolio.yaml；
        #   成交后若未重算，二者将不一致 → 前端标"本周信号基于旧持仓"，避免把过期数当现状读。
        "holdings_basis": {
            "cash": round(cash, 2),
            "shares": {str(h["code"]): float(h.get("shares", 0) or 0) for h in holdings},
        },
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
        "trend_protection": trend_protection,   # §0C #4 回测量化的回撤保护（趋势过滤 vs 静态）
        "tactical": tactical_shadow,
        "params": {
            "ma_days": ma_days, "momentum_lookback": look,
            "rebalance_abs_pp": abs_thr * 100, "rebalance_rel": rel_thr,
            "rebalance_check_frequency": check_freq, "rebalance_circuit_breaker_pp": round(breaker_thr * 100, 2),
            "stale_limit_days": STALE_LIMIT_DAYS,
        },
    }
    report_progress("汇总与落盘", step=6, total=7)
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
        line = f"{s['name']}({c}){'[缓存]' if s.get('source') == 'cache' else ('[腾讯]' if s.get('source') == 'westock' else '')}: " + (
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
                if r.get("action_reason"):
                    print(f"      理由：{r['action_reason']}")
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
        dpp = trend_protection.get("delta_pp")
        print(f"⚠️ 危机保险·趋势减仓建议（已跌破 MA{ma_days}）—— 趋势转弱，建议移到 {trend_alerts[0].get('reserve_name') or '债券'}：")
        for a in trend_alerts:
            tag = f"约 ¥{a['derisk_amount']:,.0f} → {a.get('reserve_name') or '债券'}" if a.get("actionable") \
                else "；".join(a.get("blocked_reasons") or [])
            print(f"   减 {a['name']}({a['code']}) {tag}")
        if dpp:
            print(f"   依据：历史上趋势过滤把最大回撤从 {abs(trend_protection['static_maxdd'])*100:.0f}% 降到 "
                  f"{abs(trend_protection['trend_maxdd'])*100:.0f}%（不动手约多扛 {dpp:.0f}pp 回撤）。样本内、需你确认，不自动下单。")
    if first_funding_plan["eligible"]:
        print("-" * 54)
        print(f"首次建仓预览：计划投入 ¥{first_funding_plan['planned_deploy_amount']:,.0f}"
              f"（单周上限 ¥{max_weekly:,.0f}）｜估算可成交 ¥{first_funding_plan['estimated_deploy_amount']:,.0f}")
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
            line = f"{s['name']}({c}){'[缓存]' if s.get('source') == 'cache' else ('[腾讯]' if s.get('source') == 'westock' else '')}: " + (
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
