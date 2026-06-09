#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────
# 【单一事实源 / SINGLE SOURCE OF TRUTH】 本地 Web 驾驶舱（UI 层）。
# 不重写任何策略逻辑：编辑配置后仍调用 engine/signals.py、engine/backtest.py。
#   启动： python3 engine/app.py   →   打开 http://127.0.0.1:5057
# ─────────────────────────────────────────────────────────────────────────
"""投资周报驾驶舱：网页上编辑持仓/风险偏好、一键生成本周信号、跑回测，不必手改 yaml。"""
import hashlib
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time


def _check_deps():
    """启动前检查依赖，缺失时给出清晰的安装提示，而不是丢一个 ImportError 堆栈。"""
    need = [("flask", "flask"), ("yaml", "pyyaml"), ("pandas", "pandas"), ("akshare", "akshare")]
    missing = []
    for mod, pip_name in need:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print("[启动失败] 缺少依赖：" + "、".join(missing), file=sys.stderr)
        print("请先安装：pip install -r engine/requirements.txt", file=sys.stderr)
        print("（或单独安装：pip install " + " ".join(missing) + "）", file=sys.stderr)
        sys.exit(1)


_check_deps()

from flask import Flask, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORTFOLIO = os.path.join(ROOT, "portfolio.yaml")
STRATEGY = os.path.join(ROOT, "strategy.yaml")
INVESTOR_PROFILE = os.path.join(ROOT, "investor_profile.yaml")
WEB = os.path.join(HERE, "web")
STRATEGIC_QUALITY_CACHE = os.path.join(HERE, "cache", "strategic_quality.json")

sys.path.insert(0, HERE)
import yaml  # noqa: E402
from signals import load_assumptions, load_stress_scenarios, resolve_policy_number, validate_config, validate_strategy, latest_execution_date, DEFAULT_INVESTOR_PROFILE  # noqa: E402  复用同一套校验（ARCH-02：档案默认值单一来源）
from signals import fetch_hist, prefetch_westock  # noqa: E402
import signals  # noqa: E402  锚定口径预期收益（building_block_returns + BB_* 常量）按模块名取用
from reports import (  # noqa: E402
    apply_estimated_fees,
    archive_report, compute_holdings_draft, cycle_suggestions,
    cycle_version_status,
    delete_execution_record, executions_by_code, list_reports, load_active_cycle,
    load_cash_flows, load_executions, load_json, load_nav_series, load_report, load_validated_flags, monthly_review,
    load_strategic_applies,
    performance_summary,
    refresh_cycle_config_versions, save_cash_flow, save_cycle_decision, save_execution_record,
    save_strategic_apply,
)
from learning import save_ack, watchlist_learning  # noqa: E402
import strategic  # noqa: E402  Track C 战略层纯函数（ETF 费率解析 + §8.2 硬准入）

app = Flask(__name__, static_folder=None)

# ARCH-02：投资档案默认值的单一来源 = signals.DEFAULT_INVESTOR_PROFILE（上方已导入），
#   不再在此重复一份字面量，杜绝两份"必须同步"靠纪律而无校验的静默漂移。


def _run_engine_script(script, timeout):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, os.path.join(HERE, script)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env)


def load_yaml(p):
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_investor_profile():
    if not os.path.exists(INVESTOR_PROFILE):
        return _profile_with_derived_funding(dict(DEFAULT_INVESTOR_PROFILE))
    data = load_yaml(INVESTOR_PROFILE) or {}
    return _profile_with_derived_funding({**DEFAULT_INVESTOR_PROFILE, **data})


def _is_trading_session(now=None):
    """A 股交易时段（周一至周五 09:30–11:30、13:00–15:00，按本机=北京时间近似）。
    折溢价等**实时**数据只在盘中可靠；盘后/周末取到的折价多为陈旧数据，不应据此硬判准入。"""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:                      # 周末
        return False
    t = now.hour * 60 + now.minute
    return (570 <= t <= 690) or (780 <= t <= 900)   # 9:30–11:30 / 13:00–15:00


def _load_strategic_quality_cache(max_age=7 * 24 * 3600):
    try:
        with open(STRATEGIC_QUALITY_CACHE, encoding="utf-8") as f:
            payload = json.load(f)
        age = time.time() - float(payload.get("generated_at_epoch") or 0)
        if age > max_age:
            return {}, "stale"
        return payload.get("items") or {}, "cached"
    except Exception:  # noqa: BLE001
        return {}, "missing"


def _save_strategic_quality_cache(items):
    try:
        os.makedirs(os.path.dirname(STRATEGIC_QUALITY_CACHE), exist_ok=True)
        payload = {"generated_at_epoch": time.time(), "items": items}
        with open(STRATEGIC_QUALITY_CACHE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def _num(v):
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return v  # 交给校验器报错


def _write_portfolio(port):
    lines = ["# 由 Web 驾驶舱生成；也可手动编辑。target_weight 合计需 = 1.0。",
             f"cash: {port['cash']}", "", "holdings:"]
    for h in port["holdings"]:
        lines.append(f'  - {{code: "{h["code"]}", name: "{h["name"]}", '
                     f'shares: {h["shares"]}, target_weight: {h["target_weight"]}}}')
    tmp = PORTFOLIO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, PORTFOLIO)


def _portfolio_with_target_allocation(port, strat, allocation):
    """Apply model target weights while preserving actual shares and known instruments."""
    existing = {str(h.get("code")): h for h in (port.get("holdings") or [])}
    names = {str(u.get("code")): u.get("name") or str(u.get("code")) for u in (strat.get("universe") or [])}
    codes = list(existing)
    codes.extend(code for code in allocation if code not in existing)
    holdings = []
    for code in codes:
        old = existing.get(code) or {}
        holdings.append({
            "code": code,
            "name": old.get("name") or names.get(code) or code,
            "shares": old.get("shares", 0),
            "target_weight": round(float(allocation.get(code, 0.0)), 4),
        })
    return {"cash": port.get("cash", 0), "holdings": holdings}


# §small_capital_guardrails #2：单产品目标权重一次跳变超过此阈值需显式二次确认（防一次重配静默搬大额）。
LARGE_MOVE_THRESHOLD = 0.15


def _large_target_moves(current_weights, allocation, threshold=LARGE_MOVE_THRESHOLD):
    """列出 当前→构建 目标权重单产品跳变超过 threshold 的项（用于二次确认闸）。"""
    moves = []
    for code in sorted(set(current_weights) | set(allocation)):
        cur = float(current_weights.get(code, 0.0) or 0.0)
        new = float(allocation.get(code, 0.0) or 0.0)
        if abs(new - cur) >= threshold - 1e-9:
            moves.append({"code": code, "current": round(cur, 4),
                          "constructed": round(new, 4), "delta": round(new - cur, 4)})
    return moves


def _apply_constructed_allocation(port, strat, snap):
    allocation = snap.get("instrument_allocation") or {}
    if snap.get("validation_status") != "passed" or not allocation:
        return False, snap.get("constraint_diagnostics") or ["strategic construct is not applicable"], {}
    new_port = _portfolio_with_target_allocation(port, strat, allocation)
    errs = validate_config(new_port, strat)
    if errs:
        return False, errs, {}
    _write_portfolio(new_port)
    return True, [], allocation


def _profile_with_derived_funding(profile):
    """Derive reserve bucket and ETF cap from total assets when total_assets is provided."""
    profile = dict(profile or {})
    total = float(profile.get("total_assets") or 0)
    if total <= 0:
        return profile
    gap = max(0.0, float(profile.get("unemployment_monthly_expense") or 0)
              - float(profile.get("unemployment_minimum_monthly_income") or 0))
    months = max(0.0, float(profile.get("unemployment_runway_years") or 0)) * 12.0
    months += max(0.0, float(profile.get("post_stress_reserve_months") or 0))
    reserve = gap * months
    profile["stable_assets_outside"] = round(min(total, reserve), 2)
    profile["planned_etf_capital"] = round(max(0.0, total - reserve), 2)
    return profile


def validate_investor_profile(profile):
    errs = []
    if profile.get("experience_level") not in ("beginner", "intermediate", "advanced"):
        errs.append("experience_level 须为 beginner/intermediate/advanced")
    for key, label, lo, hi in (
        ("target_annual_return", "目标年化", 0, 0.30),
        ("max_acceptable_drawdown", "最大可接受回撤", 0, 0.80),
    ):
        v = profile.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < lo or v > hi:
            errs.append(f"{label} 须在 {lo:.0%}~{hi:.0%} 之间")
    horizon = profile.get("horizon_years")
    if not isinstance(horizon, (int, float)) or isinstance(horizon, bool) or horizon < 1 or horizon > 50:
        errs.append("投资年限须在 1~50 年之间")
    for key, label in (
        ("emergency_cash_kept_outside", "场外应急现金"),
        ("monthly_contribution", "每月追加资金"),
        ("total_assets", "总资金"),
        ("stable_assets_outside", "场外稳健桶"),
        ("planned_etf_capital", "ETF 风险桶目标上限"),
        ("unemployment_monthly_expense", "失业期每月支出"),
        ("unemployment_minimum_monthly_income", "失业期最低月收入"),
        ("unemployment_runway_years", "失业保障年限"),
        ("post_stress_reserve_months", "压力期末保留月数"),
    ):
        v = profile.get(key, 0)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            errs.append(f"{label} 须为 ≥0 的数字")
    sy = profile.get("stable_assets_yield", 0.025)
    if not isinstance(sy, (int, float)) or isinstance(sy, bool) or sy < 0 or sy > 0.30:
        errs.append("稳健桶假设年化 须在 0%~30% 之间")
    return errs


def _write_investor_profile(profile):
    lines = [
        "# 个人投资目标与风险承受能力；由 Web 驾驶舱生成，也可手动编辑。",
        "# 这是个人信息，默认不入库。收益目标不是承诺，只作为风险校准刻度。",
        f"target_annual_return: {profile['target_annual_return']}",
        f"horizon_years: {profile['horizon_years']}",
        f"max_acceptable_drawdown: {profile['max_acceptable_drawdown']}",
        f"experience_level: {profile['experience_level']}",
        f"emergency_cash_kept_outside: {profile['emergency_cash_kept_outside']}",
        f"monthly_contribution: {profile['monthly_contribution']}",
        f"total_assets: {profile.get('total_assets', 0)}",
        f"stable_assets_outside: {profile.get('stable_assets_outside', 0)}",
        f"stable_assets_yield: {profile.get('stable_assets_yield', 0.025)}",
        f"planned_etf_capital: {profile.get('planned_etf_capital', 0)}",
        f"unemployment_monthly_expense: {profile.get('unemployment_monthly_expense', 6000)}",
        f"unemployment_minimum_monthly_income: {profile.get('unemployment_minimum_monthly_income', 0)}",
        f"unemployment_runway_years: {profile.get('unemployment_runway_years', 5)}",
        f"post_stress_reserve_months: {profile.get('post_stress_reserve_months', 12)}",
    ]
    with open(INVESTOR_PROFILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _set_risk_profile(val):
    with open(STRATEGY, encoding="utf-8") as f:
        txt = f.read()
    if re.search(r"(?m)^risk_profile:.*$", txt):
        txt = re.sub(r"(?m)^risk_profile:.*$", f"risk_profile: {val}", txt)
    else:
        txt = f"risk_profile: {val}\n" + txt
    with open(STRATEGY, "w", encoding="utf-8") as f:
        f.write(txt)


def _set_check_frequency(val):
    """正则改写 strategy.yaml 的 rebalance.check_frequency（保留注释，跟 _set_risk_profile 同套路）。"""
    with open(STRATEGY, encoding="utf-8") as f:
        txt = f.read()
    if re.search(r"(?m)^(\s*check_frequency:\s*)\S+(.*)$", txt):
        # 只替换值 token，保留行尾注释（group(2)）
        txt = re.sub(r"(?m)^(\s*check_frequency:\s*)\S+(.*)$", lambda m: f"{m.group(1)}{val}{m.group(2)}", txt)
    else:  # 兜底：插到 rebalance 块的 rel_threshold 行后
        txt = re.sub(r"(?m)^(\s*rel_threshold:.*)$",
                     lambda m: f"{m.group(1)}\n    check_frequency: {val}", txt, count=1)
    with open(STRATEGY, "w", encoding="utf-8") as f:
        f.write(txt)


def _replacement_candidates(strat):
    """Return same-asset instruments that could be introduced into each strategic role."""
    universe = {str(x.get("code")): x for x in (strat.get("universe") or [])}
    watchlist = {str(x.get("code")): x for x in (strat.get("watchlist") or [])}
    roles = ((strat.get("strategic_policy") or {}).get("roles") or {})
    out = []
    for role, cfg in roles.items():
        members = {str(code) for code in (cfg.get("members") or [])}
        assets = {universe.get(code, {}).get("asset") for code in members}
        assets.discard(None)
        for source, pool in (("universe", universe), ("watchlist", watchlist)):
            for code, item in pool.items():
                if code in members or item.get("asset") not in assets:
                    continue
                out.append({"role": role, "code": code, "name": item.get("name") or code,
                            "asset": item.get("asset"), "source": source})
    return out


def _introduce_strategic_role_member(role, code):
    """Promote a known candidate and add it to a strategic role while preserving YAML comments."""
    strat = load_yaml(STRATEGY)
    roles = ((strat.get("strategic_policy") or {}).get("roles") or {})
    if role not in roles:
        return False, f"unknown strategic role: {role}"
    candidates = {(row["role"], row["code"]): row for row in _replacement_candidates(strat)}
    candidate = candidates.get((role, code))
    if not candidate:
        return False, "candidate is not a same-asset replacement for this role"
    quality, _status = _load_strategic_quality_cache()
    admitted = ((quality.get(code) or {}).get("admission") or {}).get("admitted")
    if admitted is not True:
        return False, "candidate must pass basic admission in the latest ETF review before introduction"
    with open(STRATEGY, encoding="utf-8") as f:
        text = f.read()

    if candidate["source"] == "watchlist":
        watch = re.search(r"(?m)^watchlist:\s*(?:#.*)?$", text)
        start = re.search(rf"(?m)^  - code:\s*[\"']?{re.escape(code)}[\"']?\s*$", text)
        if not watch or not start or start.start() < watch.start():
            return False, "watchlist candidate block not found"
        tail = text[start.end():]
        next_item = re.search(r"(?m)^(?:  - code:|[A-Za-z_][A-Za-z0-9_]*:)", tail)
        end = start.end() + (next_item.start() if next_item else len(tail))
        block = text[start.start():end].rstrip() + "\n"
        text = text[:start.start()] + text[end:]
        watch = re.search(r"(?m)^watchlist:\s*(?:#.*)?$", text)
        text = text[:watch.start()] + block + "\n" + text[watch.start():]

    role_line = re.compile(
        rf"(?m)^(\s{{4}}{re.escape(role)}:\s*\{{[^\n]*members:\s*\[)([^\]]*)(\][^\n]*\}})\s*$")
    match = role_line.search(text)
    if not match:
        return False, "role must use the supported inline members format"
    members = match.group(2).strip()
    members = f'{members}, "{code}"' if members else f'"{code}"'
    text = text[:match.start()] + match.group(1) + members + match.group(3) + text[match.end():]
    text = re.sub(r"(?m)^(\s{2}policy_version:\s*)(\d+)\s*$",
                  lambda m: m.group(1) + str(int(m.group(2)) + 1), text, count=1)
    try:
        updated = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return False, f"updated strategy is invalid YAML: {exc}"
    errs = validate_strategy(updated)
    if errs:
        return False, "updated strategy failed validation: " + "；".join(errs)
    tmp = STRATEGY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, STRATEGY)
    return True, None


def _market_kpis_for(code, name=None, days=260, executions=None):
    df, source = fetch_hist(code)
    if df is None or df.empty:
        return {"code": code, "name": name or code, "error": "数据不足或拉取失败"}
    df = df.tail(max(days, 260)).copy()
    close = df["close"]
    last = float(close.iloc[-1])
    base = float(close.iloc[0])
    ma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
    peak = close.cummax()
    dd = close / peak - 1
    def ret(n):
        return float(close.iloc[-1] / close.iloc[-1 - n] - 1) if len(close) > n else None
    out_rows = []
    chart_df = df.tail(days)
    chart_base = float(chart_df["close"].iloc[0])
    for _, row in chart_df.iterrows():
        out_rows.append({
            "date": str(row["date"].date()),
            "close": round(float(row["close"]), 4),
            "return_pct": round((float(row["close"]) / chart_base - 1) * 100, 2),
        })
    return {
        "code": code,
        "name": name or code,
        "source": source,
        "as_of": str(df["date"].iloc[-1].date()),
        "last": round(last, 4),
        "trend": "above" if ma200 is not None and last >= ma200 else "below",
        "ma200": round(ma200, 4) if ma200 is not None else None,
        "ret_20d": ret(20),
        "ret_60d": ret(60),
        "ret_120d": ret(120),
        "ret_250d": ret(250),
        "max_drawdown_1y": float(dd.tail(250).min()) if len(dd) >= 2 else None,
        "current_drawdown": float(dd.iloc[-1]) if len(dd) >= 2 else None,
        "series": out_rows,
        "executions": (executions or {}).get(code, []),
    }


# ---------- ETF 折溢价 / 规模（清盘风险）：实时快照 + 纯函数分类 ----------

# 对折溢价更敏感的资产：QDII（海外）、黄金、货币，溢价更容易明显偏离净值
_PREMIUM_SENSITIVE_ASSETS = {"gold", "global_equity", "global_growth", "cash"}
_SPOT = {"df": None, "ts": 0.0}


def _etf_spot_snapshot(max_age=120):
    """拉取场内 ETF 实时快照（含 IOPV / 折溢价 / 规模），进程内缓存 max_age 秒。

    取数失败返回 None（→ 折溢价/规模标注为不可用，绝不编造）。
    """
    now = time.time()
    if _SPOT["df"] is not None and (now - _SPOT["ts"]) < max_age:
        return _SPOT["df"]
    try:
        import akshare as ak  # noqa: PLC0415
        df = ak.fund_etf_spot_em()
        if df is not None and not df.empty:
            _SPOT["df"] = df
            _SPOT["ts"] = now
            return df
    except Exception:  # noqa: BLE001
        pass
    return None


def _spot_row_metrics(snap, code):
    """从快照里取某 code 的 {price, iopv, premium, market_cap}；缺数据返回 None 或字段为 None。

    premium = 最新价 / IOPV - 1（正=溢价，负=折价）；货币基金无 IOPV 时 premium=None。
    market_cap 优先用总市值，回退流通市值（近似规模，用于清盘风险）。
    """
    if snap is None:
        return None
    try:
        import pandas as pd  # noqa: PLC0415
        code_col = next((c for c in ("代码", "code") if c in snap.columns), None)
        if not code_col:
            return None
        row = snap[snap[code_col].astype(str) == str(code)]
        if row.empty:
            return None
        r = row.iloc[0]

        def num(*names):
            for n in names:
                if n in snap.columns:
                    v = pd.to_numeric(r[n], errors="coerce")
                    if not pd.isna(v):
                        return float(v)
            return None

        price = num("最新价", "price")
        iopv = num("IOPV实时估值", "IOPV", "iopv")
        market_cap = num("总市值", "流通市值")
        turnover = num("成交额", "amount")   # 快照里的"近一日成交额"，用于历史源失败时兜底流动性
        premium = (price / iopv - 1) if (price and iopv and iopv > 0) else None
        return {"price": price, "iopv": iopv, "premium": premium,
                "market_cap": market_cap, "turnover": turnover}
    except Exception:  # noqa: BLE001
        return None


def _classify_premium(premium, sensitive=False):
    """折溢价分级（纯函数）。返回 (level, message)；premium 为 None 返回 (None, None)。"""
    if premium is None:
        return None, None
    pct = premium * 100
    ap = abs(pct)
    side = "溢价" if pct > 0 else "折价"
    hi, mid = (1.5, 0.5) if sensitive else (3.0, 1.0)
    if ap >= hi:
        return "issue", f"{side} {ap:.2f}%，明显偏离净值，建议此时不要买入、先观察等回落"
    if ap >= mid:
        return "warn", f"{side} {ap:.2f}%，下单前留意，别追高溢价"
    return "ok", f"{side} {ap:.2f}%，接近净值"


def _classify_scale(market_cap):
    """规模/清盘风险分级（纯函数）。market_cap 单位元。返回 (level, message)。"""
    if market_cap is None:
        return None, None
    yi = market_cap / 1e8  # 亿元
    if yi < 0.5:
        return "issue", f"规模约 {yi:.2f} 亿元，偏小，留意清盘风险"
    if yi < 2:
        return "warn", f"规模约 {yi:.2f} 亿元，规模偏小，关注清盘风险"
    return "ok", f"规模约 {yi:.1f} 亿元"


# ── westock（腾讯自选股）：ETF 折溢价/规模/成交额/申购状态的首选源（批量优先）──
# 通过 `etf 代码1,代码2,...` 一次批量取（自动 Batch 模式，局部降级）；进程内缓存。
# westock `etf` 接口本身偏不稳，取不到一律返回 None（绝不编造），上层再用 akshare 快照兜底。
_WESTOCK_PKG = "westock-data-skillhub@1.0.3"
_WESTOCK_CACHE = {}        # code -> (ts, metrics|None)        单只兜底缓存
_WESTOCK_ETF_BATCH = {}    # bare_code -> (ts, row_dict|None)  批量预取缓存（_prefetch_westock_etf 填）
_PURCHASE_BLOCK_KEYS = ("不可申购", "暂停申购", "暂停", "限大额", "限制申购")


def _westock_symbol(code):
    """项目裸代码 → westock 带市场前缀代码（5/6 开头=沪市，其余=深市，如 159915→sz159915）。"""
    code = str(code)
    return ("sh" if code[:1] in ("5", "6") else "sz") + code


def _run_westock(args, timeout=45):
    """运行 westock CLI（npx），返回 stdout 或 None。npx 不可用/失败/超时都返回 None。"""
    exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-y", _WESTOCK_PKG, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        return r.stdout if r.returncode == 0 and r.stdout else None
    except Exception:  # noqa: BLE001
        return None


def _parse_westock_etf(md):
    """解析 westock `etf` 单只输出里的首个明细表，返回 表头->值 dict；失败返回 None。"""
    if not md:
        return None
    lines = [ln for ln in md.splitlines() if ln.strip().startswith("|")]
    for i in range(len(lines) - 2):
        sep = lines[i + 1].replace("|", "").replace(" ", "")
        if sep and set(sep) <= set("-"):
            header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            data = [c.strip() for c in lines[i + 2].strip().strip("|").split("|")]
            if "code" in header and len(header) == len(data):
                return dict(zip(header, data))
    return None


def _parse_westock_etf_batch(md):
    """解析批量 `etf 代码1,代码2,...` 输出（每行一只）为 {bare_code: 表头->值 dict}。失败返回 {}。"""
    if not md:
        return {}
    lines = [ln for ln in md.splitlines() if ln.strip().startswith("|")]
    for i in range(len(lines) - 1):
        sep = lines[i + 1].replace("|", "").replace(" ", "")
        if not (sep and set(sep) <= set("-")):
            continue
        header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        keycol = "code" if "code" in header else ("symbol" if "symbol" in header else None)
        if not keycol:
            continue
        ki = header.index(keycol)
        out = {}
        for ln in lines[i + 2:]:
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) != len(header):
                continue
            raw = cells[ki]
            bare = raw[2:] if raw[:2].lower() in ("sh", "sz") else raw
            out[bare] = dict(zip(header, cells))
        if out:
            return out
    return {}


def _etf_row_to_metrics(row):
    """westock etf 明细行 -> {premium, market_cap, turnover, purchase_status, last_price, iopv, ...}；行空返回 None。"""
    if not row:
        return None

    def num(k):
        try:
            return float(row.get(k))
        except (TypeError, ValueError):
            return None
    close, nav = num("closePrice"), num("nav")
    # 多周期收益与最大回撤（westock 已以百分比给出，如 -12.0 = -12%；None 表示无数据）
    def pct(k):
        v = num(k)
        return round(v, 2) if v is not None else None
    returns = {k: pct(wk) for k, wk in [
        ("ytd", "ytdReturn"), ("r1m", "return1M"), ("r3m", "return3M"),
        ("r6m", "return6M"),  ("r1y", "return1Y"), ("r3y", "return3Y"),
        ("mdd1y", "maxDrawdown1Y"), ("mdd3y", "maxDrawdown3Y"),
    ]}
    returns = {k: v for k, v in returns.items() if v is not None} or None
    return {
        "premium": (close / nav - 1) if (close and nav and nav > 0) else None,
        "market_cap": num("totalMV"),
        "turnover": num("turnoverValue"),
        "purchase_status": (row.get("purchaseStatus") or "").strip() or None,
        "establish_date": (row.get("establishDate") or "")[:10] or None,
        "last_price": close,
        "iopv": nav,
        "returns": returns,
    }


def _prefetch_westock_etf(codes, max_age=300):
    """一次批量取 westock `etf` 详情，填 _WESTOCK_ETF_BATCH（bare->行 dict）。
    缺失/失败记 None（避免逐只重复批量）；随后 _westock_etf_metrics 会优先命中本缓存。"""
    now = time.time()
    todo = []
    for c in codes:
        c = str(c)
        cached = _WESTOCK_ETF_BATCH.get(c)
        if not (cached and now - cached[0] < max_age):
            todo.append(c)
    if not todo:
        return
    syms = ",".join(_westock_symbol(c) for c in todo)
    parsed = _parse_westock_etf_batch(_run_westock(["etf", syms], timeout=90))
    for c in todo:
        _WESTOCK_ETF_BATCH[c] = (now, parsed.get(c))


def _westock_covers_all(codes, max_age=300):
    """批量 etf 是否已覆盖所有 code 的折溢价+规模——是则上层可跳过慢的 akshare 快照。"""
    now = time.time()
    for c in codes:
        b = _WESTOCK_ETF_BATCH.get(str(c))
        m = _etf_row_to_metrics(b[1]) if (b and now - b[0] < max_age and b[1]) else None
        if not m or m.get("premium") is None or m.get("market_cap") is None:
            return False
    return True


def _westock_etf_metrics(code, max_age=300):
    """取 westock `etf` 指标：先批量预取缓存 → 旧单只缓存 → 单只兜底取一次。失败返回 None。"""
    now = time.time()
    code = str(code)
    b = _WESTOCK_ETF_BATCH.get(code)
    if b and now - b[0] < max_age:
        return _etf_row_to_metrics(b[1])      # 批量命中（b[1] 为 None 表示该只批量没取到）
    cached = _WESTOCK_CACHE.get(code)
    if cached and now - cached[0] < max_age:
        return cached[1]
    out = _etf_row_to_metrics(_parse_westock_etf(_run_westock(["etf", _westock_symbol(code)])))
    _WESTOCK_CACHE[code] = (now, out)
    return out


def _quality_metrics(code, snap, sensitive):
    """折溢价/规模/成交额：westock `etf`（批量）优先，akshare 快照兜底。返回 (metrics, extra)。

    extra.fallback=True 表示 westock 实时源此刻不可用、改用了 akshare 快照。
    """
    ws = _westock_etf_metrics(code) or {}
    aks = dict(_spot_row_metrics(snap, code) or {})
    m = {}
    extra = {"premium_source": None, "scale_source": None,
             "purchase_status": ws.get("purchase_status"),
             "establish_date": ws.get("establish_date"),
             "returns": ws.get("returns"),   # 多周期收益/回撤，来自 westock etf（无 akshare 对应源）
             "fallback": False}
    # 折溢价（含 iopv / price）：westock 优先
    if ws.get("premium") is not None:
        m["premium"], m["iopv"], m["price"] = ws["premium"], ws.get("iopv"), ws.get("last_price")
        extra["premium_source"] = "westock"
    elif aks.get("premium") is not None:
        m["premium"], m["iopv"] = aks["premium"], aks.get("iopv")
        extra["premium_source"] = "akshare"
        extra["fallback"] = True
    # 规模：westock 优先
    if ws.get("market_cap") is not None:
        m["market_cap"] = ws["market_cap"]
        extra["scale_source"] = "westock"
    elif aks.get("market_cap") is not None:
        m["market_cap"] = aks["market_cap"]
        extra["scale_source"] = "akshare"
        extra["fallback"] = True
    # 近一日成交额：westock 优先
    m["turnover"] = ws.get("turnover") if ws.get("turnover") is not None else aks.get("turnover")
    # price / iopv 最终兜底
    if m.get("price") is None:
        m["price"] = ws.get("last_price") if ws.get("last_price") is not None else aks.get("price")
    if m.get("iopv") is None and aks.get("iopv") is not None:
        m["iopv"] = aks.get("iopv")
    return m, extra


def _purchase_status_note(purchase_status, sensitive):
    """申购状态提示（纯函数）。不可/暂停申购：QDII 等敏感品种→issue，其它→warn。"""
    if not purchase_status:
        return None, None
    if any(k in purchase_status for k in _PURCHASE_BLOCK_KEYS):
        if sensitive:
            return "issue", f"当前申购状态：{purchase_status}——QDII 溢价此时易失控，建议先观察、不要追高"
        return "warn", f"当前申购状态：{purchase_status}"
    return None, None


def _exec_quality_decision(premium, purchase_status, sensitive):
    """纯函数：综合实时折溢价 + 申购状态，对【买入】动作给执行质量裁决。
    返回 (verdict, messages)：verdict ∈ {'block','warn','ok'}。
    - issue 档（敏感品种溢价≥1.5% / 普通≥3%，或不可/暂停申购）→ block（建议缓买）；
    - warn 档，或敏感品种实时溢价缺失 → warn（仍可执行，但提示自查，缺失≠中性放行）；
    - 其余 → ok。只用于买入；卖出不调用（溢价高反而利于卖出）。"""
    plevel, pmsg = _classify_premium(premium, sensitive)
    slevel, smsg = _purchase_status_note(purchase_status, sensitive)
    blocks = [m for lv, m in ((plevel, pmsg), (slevel, smsg)) if lv == "issue"]
    if blocks:
        return "block", blocks
    warns = [m for lv, m in ((plevel, pmsg), (slevel, smsg)) if lv == "warn"]
    if premium is None and sensitive:
        warns.append("实时溢价数据暂缺，下单前请自查溢价/申购状态")
    return ("warn", warns) if warns else ("ok", [])


_FLAG_BLOCK_CATEGORIES = ("政策风险", "流动性风险")   # 前瞻政策闸：这两类利空·actionable 旗标 → 暂缓买入


def _policy_flag_blocks(code, flags):
    """命中 code 的『利空 · actionable · 政策/流动性风险』旗标标题（前瞻政策闸，如 QDII 限购传闻）。

    real-time 申购状态闸只看"已经限购"；本闸让工具对**已查证写入的前瞻政策风险**（限购传闻等）提前反应。
    schema 已禁止 confidence=低 的旗标 actionable=true，故此处只认 actionable 即可。"""
    out = []
    for f in (flags or []):
        if (f.get("actionable") and f.get("direction") == "利空"
                and f.get("category") in _FLAG_BLOCK_CATEGORIES):
            aa = [str(x) for x in (f.get("affected_assets") or [])]
            if str(code) in aa or "ALL" in aa:
                out.append(str(f.get("title") or f.get("category")))
    return out


def _apply_execution_quality_gate(signals):
    """执行质量闸：对买入类动作（加仓 / 首次建仓）按当前实时折溢价 + 申购状态 + 前瞻政策旗标裁决。
    issue 档 / 政策风险旗标 → 降级 actionable=False 并补 blocked_reasons（移入"被拦截"区）；
    warn / 缺失 → 仍可执行，挂 exec_quality_note 提示。只改买入方向、卖出不动；就地修改并返回。
    任一步失败都吞掉（绝不阻断周报生成）。"""
    per = signals.get("signals") or {}
    add_acts = [r for r in (signals.get("actionable_rebalance") or [])
                if r.get("suggest") == "add" and r.get("actionable")]
    first_orders = [o for o in ((signals.get("first_funding_plan") or {}).get("orders") or [])
                    if o.get("actionable")]
    targets = add_acts + first_orders
    if not targets:
        return signals
    try:
        # C：旗标先机械校验 + 判新鲜度；不通过(rejected→已置空)或过旧(stale) 一律不参与拦买。
        fv = load_validated_flags(signal_generated_for=signals.get("generated_for"))
        flags = [] if fv.get("stale") else (fv.get("flags") or [])
    except Exception:  # noqa: BLE001
        flags = []
    codes = sorted({str(e.get("code")) for e in targets if e.get("code")})
    try:
        prefetch_westock(codes)
        _prefetch_westock_etf(codes)
    except Exception:  # noqa: BLE001
        pass
    try:
        snap = _etf_spot_snapshot()
    except Exception:  # noqa: BLE001
        snap = None
    gated = False
    for e in targets:
        code = str(e.get("code"))
        sensitive = (per.get(code) or {}).get("asset") in _PREMIUM_SENSITIVE_ASSETS
        try:
            metrics, qextra = _quality_metrics(code, snap, sensitive)
        except Exception:  # noqa: BLE001
            metrics, qextra = {}, {}
        verdict, msgs = _exec_quality_decision(
            metrics.get("premium"), qextra.get("purchase_status"), sensitive)
        pblocks = _policy_flag_blocks(code, flags)        # 前瞻政策闸：限购等利空政策旗标 → 强制暂缓
        if pblocks:
            verdict = "block"
            msgs = list(msgs) + ["政策风险（前瞻旗标）：" + t for t in pblocks]
        if verdict == "block":
            e["actionable"] = False
            e["blocked_reasons"] = list(e.get("blocked_reasons") or []) + msgs
            e["exec_quality"] = "blocked"
            if isinstance(e.get("reason_factors"), dict):
                e["reason_factors"]["exec_quality"] = "blocked"
            if "action_reason" in e:
                e["action_reason"] += "；执行质量降级（暂缓）：" + "；".join(msgs)
            gated = True
        elif verdict == "warn":
            e["exec_quality"] = "warn"
            e["exec_quality_note"] = "；".join(msgs)
            if isinstance(e.get("reason_factors"), dict):
                e["reason_factors"]["exec_quality"] = "warn"
            if "action_reason" in e:
                e["action_reason"] += "（执行质量提示：" + "；".join(msgs) + "）"
    if gated:
        signals["exec_quality_gated"] = True
    return signals


def _recheck_cycle_suggestions(suggestions, signals):
    """在准备执行时用快速实时源重验买入动作；不修改归档周期。

    此处刻意不调用慢速 akshare 全市场快照。westock 实时源拿不到时，敏感品种按
    “缺失≠中性”给 warn，避免打开调仓窗口等待几十秒；完整质量页仍保留慢速兜底。
    """
    suggestions = [dict(s or {}) for s in (suggestions or [])]
    buys = [s for s in suggestions if s.get("side") == "buy"]
    if not buys:
        return suggestions
    per = (signals or {}).get("signals") or {}
    codes = sorted({str(s.get("code")) for s in buys if s.get("code")})
    try:
        prefetch_westock(codes)
        _prefetch_westock_etf(codes)
    except Exception:  # noqa: BLE001
        pass
    snap = None
    for suggestion in buys:
        code = str(suggestion.get("code"))
        sensitive = (per.get(code) or {}).get("asset") in _PREMIUM_SENSITIVE_ASSETS
        try:
            metrics, extra = _quality_metrics(code, snap, sensitive)
        except Exception:  # noqa: BLE001
            metrics, extra = {}, {}
        verdict, messages = _exec_quality_decision(
            metrics.get("premium"), extra.get("purchase_status"), sensitive)
        suggestion["execution_quality"] = verdict
        suggestion["execution_quality_notes"] = messages
        if verdict == "block":
            suggestion["action_status"] = "blocked_now"
    return suggestions


def _years_since(date_str):
    """成立日(YYYY-MM-DD…) → 距今年限；缺失/非法返回 None。"""
    if not date_str:
        return None
    try:
        from datetime import date, datetime as _dt  # noqa: PLC0415
        ed = _dt.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        return max(0.0, (date.today() - ed).days / 365.25)
    except Exception:  # noqa: BLE001
        return None


def _akshare_avg_turnover_20d(code):
    """兜底：单独问 akshare 日线的近20日平均成交额（元）。失败返回 None。"""
    try:
        import akshare as ak  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        d = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="")
        if d is None or d.empty:
            return None
        col = next((c for c in ("成交额", "amount") if c in d.columns), None)
        if not col:
            return None
        amt = pd.to_numeric(d[col], errors="coerce").dropna().tail(20)
        return float(amt.mean()) if not amt.empty else None
    except Exception:  # noqa: BLE001
        return None


_FEE_CACHE = {}                 # code -> (ts, fee_dict|None)
_FEE_TTL = 7 * 24 * 3600        # 周级：费率年内基本不变


def _etf_fee(code, max_age=_FEE_TTL):
    """ETF 管理费/托管费（Track C §8.5）。ak.fund_fee_em 唯一可用源，进程内缓存周级。

    失败/缺失返回 None（绝不编造、绝不阻塞主流程）；解析交给 strategic.parse_etf_fee 纯函数。
    """
    code = str(code)
    now = time.time()
    hit = _FEE_CACHE.get(code)
    if hit and (now - hit[0]) < max_age:
        return hit[1]
    fee = None
    try:
        import akshare as ak  # noqa: PLC0415
        df = ak.fund_fee_em(symbol=code, indicator="运作费用")
        if df is not None and not df.empty:
            fee = strategic.parse_etf_fee(df.values.tolist())
    except Exception:  # noqa: BLE001
        fee = None
    _FEE_CACHE[code] = (now, fee)
    return fee


_TE_CACHE = {}                  # "code|proxy" -> (ts, dispersion|None)
_TE_TTL = 24 * 3600             # 日级（净值/指数日更）


def _etf_tracking_dispersion(code, proxy_index, max_age=_TE_TTL):
    """ETF 相对跟踪离散度（§8.4 best-effort）：ETF 累计净值收益 vs 代理指数(价格)收益的年化差值 std。

    无 proxy_index（QDII/黄金）或取数失败 → None（不输出伪精确）。进程内缓存日级。
    注：代理指数为价格指数（未含分红）→ 含分红缺口，仅作横向排序、非绝对 TE（strategic.tracking_dispersion 已注明）。
    """
    if not proxy_index:
        return None
    key = f"{code}|{proxy_index}"
    now = time.time()
    hit = _TE_CACHE.get(key)
    if hit and (now - hit[0]) < max_age:
        return hit[1]
    val = None
    try:
        import akshare as ak  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        nav = ak.fund_etf_fund_info_em(code)
        idx = ak.stock_zh_index_daily(symbol=proxy_index)
        navc = next((c for c in ("累计净值", "accumulated_nav") if c in nav.columns), None)
        navd = next((c for c in ("净值日期", "date") if c in nav.columns), None)
        if navc and navd and idx is not None and "close" in idx.columns:
            n = nav[[navd, navc]].copy()
            n[navd] = pd.to_datetime(n[navd])
            n = n.set_index(navd)[navc].astype(float).sort_index()
            i = idx.copy()
            i["date"] = pd.to_datetime(i["date"])
            i = i.set_index("date")["close"].astype(float).sort_index()
            df = pd.concat([n.rename("etf"), i.rename("idx")], axis=1).dropna().tail(378)  # 近 ~1.5 年
            etf_ret = df["etf"].pct_change().dropna().tolist()
            idx_ret = df["idx"].pct_change().dropna().tolist()
            val = strategic.tracking_dispersion(etf_ret, idx_ret)
    except Exception:  # noqa: BLE001
        val = None
    _TE_CACHE[key] = (now, val)
    return val


_HOLD_CACHE = {}                # code -> (ts, {stock: weight}|None)
_HOLD_TTL = 30 * 24 * 3600      # 月级（成分股季频披露、滞后~2月）


def _etf_holdings(code, max_age=_HOLD_TTL):
    """ETF 成分股持仓 {股票代码: 占净值比例(小数)}（§7.3，季频，取最新一期）。

    债/黄金/无成分 → None；QDII 返回美股代码（与 A股 自然不交集）。进程内缓存月级。
    """
    code = str(code)
    now = time.time()
    hit = _HOLD_CACHE.get(code)
    if hit and (now - hit[0]) < max_age:
        return hit[1]
    res = None
    try:
        import akshare as ak  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        yr = time.localtime().tm_year
        df = None
        for y in (yr, yr - 1):                       # 年初当季未披露 → 退一年
            d = ak.fund_portfolio_hold_em(symbol=code, date=str(y))
            if d is not None and not d.empty:
                df = d
                break
        if df is not None:
            ccol = next((c for c in ("股票代码", "code") if c in df.columns), None)
            wcol = next((c for c in ("占净值比例", "weight") if c in df.columns), None)
            qcol = next((c for c in df.columns if "季度" in str(c)), None)
            if ccol and wcol:
                if qcol:                              # 多季度 → 只取最新一期（首行所属季度）
                    df = df[df[qcol] == df[qcol].iloc[0]]
                res = {}
                for _, row in df.iterrows():
                    wv = pd.to_numeric(row[wcol], errors="coerce")
                    if pd.notna(wv):
                        res[str(row[ccol])] = float(wv) / 100.0
                res = res or None
    except Exception:  # noqa: BLE001
        res = None
    _HOLD_CACHE[code] = (now, res)
    return res


_ETF_SPOT_TTL = 12 * 3600
_ETF_SPOT_MEM = {}              # {"ts":, "rows": {code: {name, turnover}}}


def _etf_spot_list(max_age=_ETF_SPOT_TTL):
    """全市场 ETF 清单 {code: {name, turnover}}，用于同类发现。fund_etf_spot_em 偏慢(~30s/14页)
    → 进程内 + 文件缓存(当日)。失败回退已有文件/空（绝不编造）。"""
    now = time.time()
    if _ETF_SPOT_MEM.get("rows") and (now - _ETF_SPOT_MEM.get("ts", 0)) < max_age:
        return _ETF_SPOT_MEM["rows"]
    path = os.path.join(HERE, "cache", "etf_spot_list.json")
    today = str(datetime.date.today())
    try:
        if os.path.exists(path):
            cached = load_json(path) or {}
            if cached.get("date") == today and cached.get("rows"):
                _ETF_SPOT_MEM.update(ts=now, rows=cached["rows"])
                return cached["rows"]
    except Exception:  # noqa: BLE001
        pass
    rows = {}
    try:
        import akshare as ak  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        df = ak.fund_etf_spot_em()
        for _, r in df.iterrows():
            c = str(r.get("代码") or "").strip()
            if c:
                tv = pd.to_numeric(r.get("成交额"), errors="coerce")
                rows[c] = {"name": str(r.get("名称") or c), "turnover": float(tv) if pd.notna(tv) else None}
    except Exception:  # noqa: BLE001
        rows = {}
    if rows:
        _ETF_SPOT_MEM.update(ts=now, rows=rows)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"date": today, "rows": rows}, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        return rows
    try:                                      # live 失败 → 回退旧文件（哪怕过期，发现用途可接受）
        return (load_json(path) or {}).get("rows") or {}
    except Exception:  # noqa: BLE001
        return {}


def _name_matches_peer(name, kw):
    """名称是否为关键词 kw 的同类：含子串『kwETF』且其前一字符不是数字
    （排除『300红利低波ETF』这类"数字+kw"实为不同指数的）。"""
    if not kw:
        return False
    idx = str(name).find(str(kw) + "ETF")
    if idx < 0:
        return False
    return not (idx > 0 and str(name)[idx - 1].isdigit())


def _peer_match_keyword(item):
    """同类匹配关键词：优先 config 的 peer_match，否则从名称推断（『沪深300ETF』→『沪深300』）。"""
    kw = (item or {}).get("peer_match")
    if kw:
        return str(kw)
    head = str((item or {}).get("name") or "").split("ETF")[0].strip()
    return head or None


def _etf_peers(code, strat, limit=6):
    """同类 ETF 发现（自动匹配·需人工确认）：用『<指数关键词>ETF』精确子串在全市场找同跟踪指数的 ETF，
    列出 费率+流动性 供横向比较、按费率升序。**仅作研究发现**——满意的加进 watchlist 后走正式准入闭环，
    本接口绝不触发任何交易/引入动作。"""
    code = str(code)
    uni = {str(u["code"]): u for u in (strat.get("universe") or [])}
    item = uni.get(code) or {}
    kw = _peer_match_keyword(item)
    if not kw:
        return {"keyword": None, "peers": [], "count": 0, "note": "无法从名称推断同类关键词"}
    spot = _etf_spot_list()
    matched = {c: v for c, v in spot.items() if _name_matches_peer(v.get("name", ""), kw)}
    if code in spot:
        matched[code] = spot[code]            # incumbent 永远纳入对比
    ranked = sorted(matched.items(), key=lambda cv: (cv[1].get("turnover") or 0), reverse=True)
    top = dict(ranked[:max(limit, 1)])
    if code in spot:
        top[code] = spot[code]
    rows = []
    for c, v in top.items():
        fee = _etf_fee(c)
        rows.append({"code": c, "name": v.get("name"),
                     "fee": (fee or {}).get("expense_ratio") if fee else None,
                     "turnover": v.get("turnover"), "is_incumbent": c == code})
    rows.sort(key=lambda r: (r["fee"] is None, r["fee"] if r["fee"] is not None else 9.0))
    return {"keyword": kw, "count": len(matched), "peers": rows,
            "spot_available": bool(spot),
            "note": "自动匹配·需人工确认；满意的加进 watchlist 后走正式准入比较"}


def _etf_quality_for(code, name=None, snap=None, sensitive=False,
                     planned_position=None, planned_single_trade=None, proxy_index=None,
                     realtime_reliable=True):
    """ETF 产品质量检查。行情/成交额优先 westock（批量 kline 自带 amount）、缺则 akshare 兜底；
    缺字段只提示不足，不把未知当通过。

    Track C Phase B：附带 ETF 费率（_etf_fee）与 §8.2 硬准入裁决（strategic.hard_admission）；
    planned_position/planned_single_trade 由调用方按计划资金规模算好传入（None 则跳过对应核算）。
    """
    try:
        import pandas as pd  # noqa: PLC0415
        df, source = fetch_hist(code)           # westock(带 amount) → 东财 → 新浪 → 缓存
        if df is None or df.empty:
            return {"code": code, "name": name or code, "status": "数据不足", "issues": ["无法获取 ETF 历史数据"]}
        dates = df["date"]
        issues, warnings = [], []

        metrics, qextra = _quality_metrics(code, snap, sensitive)
        premium = metrics.get("premium")
        # 非交易时段：折溢价为陈旧数据，不可靠 → 置空、按"待复核数据缺失"处理（不硬判准入），避免周末/盘后误报折价超限。
        if not realtime_reliable and premium is not None:
            premium = None
            warnings.append("折溢价为非交易时段数据、不可靠，已跳过该项（请在交易时段重新审视）")
        market_cap = metrics.get("market_cap")
        turnover_1d = metrics.get("turnover")
        # 上市年限：用 etf 详情成立日（westock kline 仅 ~1.3 年窗口，不能当上市年限）；缺则未知、不臆断
        history_years = _years_since(qextra.get("establish_date"))

        # 近20日平均成交额：优先 westock 批量 kline 自带的 amount；缺则单独问 akshare；再缺退到近一日成交额
        avg_turnover_20d = None
        if "amount" in df.columns:
            amt = pd.to_numeric(df["amount"], errors="coerce").dropna().tail(20)
            if not amt.empty:
                avg_turnover_20d = float(amt.mean())
        if avg_turnover_20d is None:
            avg_turnover_20d = _akshare_avg_turnover_20d(code)
        if avg_turnover_20d is not None:
            if avg_turnover_20d < 10_000_000:
                issues.append("近20日平均成交额低于 1000 万元，流动性偏弱")
            elif avg_turnover_20d < 50_000_000:
                warnings.append("近20日平均成交额低于 5000 万元，下单前需关注盘口")
        elif turnover_1d is not None:
            warnings.append("20日成交额暂不可用，已用“近一日成交额”评估流动性")
            if turnover_1d < 10_000_000:
                issues.append("近一日成交额低于 1000 万元，流动性偏弱")
            elif turnover_1d < 50_000_000:
                warnings.append("近一日成交额低于 5000 万元，下单前需关注盘口")
        else:
            warnings.append("成交额暂不可用，本次仅据曲线/折溢价/规模判断；可点“刷新”重试")

        if history_years is not None:
            if history_years < 1:
                issues.append("上市不足 1 年，历史样本太短")
            elif history_years < 3:
                warnings.append("上市不足 3 年，历史样本偏短")

        plevel, pmsg = _classify_premium(premium, sensitive)
        if plevel == "issue":
            issues.append("折溢价：" + pmsg)
        elif plevel == "warn":
            warnings.append("折溢价：" + pmsg)
        slevel, smsg = _classify_scale(market_cap)
        if slevel == "issue":
            issues.append(smsg)
        elif slevel == "warn":
            warnings.append(smsg)
        pslevel, psmsg = _purchase_status_note(qextra.get("purchase_status"), sensitive)
        if pslevel == "issue":
            issues.append(psmsg)
        elif pslevel == "warn":
            warnings.append(psmsg)
        if premium is None and sensitive:
            warnings.append("折溢价数据不可用（货币/QDII 等尤其要留意溢价，下单前请在行情软件确认）")
        if qextra.get("fallback"):
            warnings.append("折溢价/规模来自 akshare 快照（westock 实时源暂不可用时兜底）")

        # Track C Phase B §8.2/§8.3/§8.5：费率 + 硬准入 + 产品分（吃上面已取好的数；缺关键字段=降资格不 fail-open）
        fee = _etf_fee(code)
        cand = {"market_cap": market_cap, "avg_turnover_20d": avg_turnover_20d,
                "premium": premium, "purchase_status": qextra.get("purchase_status"),
                "listed_years": history_years, "fee": fee,
                # §8.4 best-effort：仅当传入 proxy_index（战略审视入口）时才取，普通质量页不拖慢
                "tracking_dispersion": _etf_tracking_dispersion(code, proxy_index) if proxy_index else None}
        admission = strategic.hard_admission(
            cand, planned_single_trade=planned_single_trade, planned_position=planned_position)
        score = strategic.product_score(cand)

        status = "不足" if issues else ("关注" if warnings else "通过")
        return {
            "code": code,
            "name": name or code,
            "status": status,
            "fee": fee,
            "admission": admission,
            "score": score,
            "history_years": round(history_years, 1) if history_years is not None else None,
            "avg_turnover_20d": round(avg_turnover_20d, 0) if avg_turnover_20d is not None else None,
            "turnover_1d": round(turnover_1d, 0) if turnover_1d is not None else None,
            "premium_pct": round(premium * 100, 2) if premium is not None else None,
            "iopv": metrics.get("iopv"),
            "last_price": metrics.get("price"),
            "market_cap": round(market_cap, 0) if market_cap is not None else None,
            "purchase_status": qextra.get("purchase_status"),
            "premium_source": qextra.get("premium_source"),
            "scale_source": qextra.get("scale_source"),
            "price_source": source,
            "as_of": str(dates.max().date()),
            "returns": qextra.get("returns"),   # westock 多周期收益/回撤（%，可 None）
            "issues": issues,
            "warnings": warnings,
        }
    except Exception as e:  # noqa: BLE001
        return {"code": code, "name": name or code, "status": "数据不足", "issues": [f"质量检查失败：{e}"]}


def _data_health():
    signals_path = os.path.join(HERE, "signals.json")
    cache_dir = os.path.join(HERE, "cache")
    data_dir = os.path.join(HERE, "data")
    signals = {}
    if os.path.exists(signals_path):
        try:
            with open(signals_path, encoding="utf-8") as f:
                signals = json.load(f)
        except Exception:  # noqa: BLE001
            signals = {}
    cache_files = []
    for base in (cache_dir, data_dir):
        if not os.path.exists(base):
            continue
        for fn in sorted(os.listdir(base)):
            if fn.endswith((".csv", ".json")):
                p = os.path.join(base, fn)
                cache_files.append({
                    "file": os.path.relpath(p, ROOT),
                    "modified_at": os.path.getmtime(p),
                    "size": os.path.getsize(p),
                })
    cache_files.sort(key=lambda x: x["modified_at"], reverse=True)
    return {
        "signals_present": bool(signals),
        "generated_for": signals.get("generated_for"),
        "data_quality": signals.get("data_quality"),
        "as_of_summary": signals.get("as_of_summary"),
        "used_cache": signals.get("used_cache"),
        "stale_days_max": signals.get("stale_days_max"),
        "missing_prices": signals.get("missing_prices", []),
        "valuation_status": signals.get("valuation_status", {}),
        "cache_file_count": len(cache_files),
        "latest_cache_files": cache_files[:8],
    }


@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/web/<path:filename>")
def web_asset(filename):
    return send_from_directory(WEB, filename)


@app.post("/api/portfolio/cash")
def adjust_cash():
    """添加 / 提取 ETF 桶现金（只改可投现金余额，不是 ETF 成交、不进 TWR/MWR）。记一条 journal/cashflows。"""
    body = request.get_json(force=True) or {}
    action = body.get("action")
    try:
        amount = float(body.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "请输入有效金额"}), 400
    if action not in ("add", "withdraw") or amount <= 0:
        return jsonify({"ok": False, "error": "金额需 >0，操作为 添加(add)/提取(withdraw)"}), 400
    port = load_yaml(PORTFOLIO)
    cur = float(port.get("cash", 0) or 0)
    new_cash = round(cur + (amount if action == "add" else -amount), 2)
    if new_cash < -1e-9:
        return jsonify({"ok": False, "error": f"提取 ¥{amount:,.2f} 超过当前现金 ¥{cur:,.2f}"}), 400
    port["cash"] = new_cash
    _write_portfolio(port)
    rec = save_cash_flow(action, amount, cur, new_cash, body.get("note"))
    return jsonify({"ok": True, "cash": new_cash, "previous": cur,
                    "delta": round(new_cash - cur, 2), "record_id": rec["id"]})


@app.get("/api/portfolio/cashflows")
def cash_flows():
    return jsonify({"ok": True, "cashflows": load_cash_flows()})


@app.get("/api/config")
def get_config():
    port, strat = load_yaml(PORTFOLIO), load_yaml(STRATEGY)
    investor_profile = load_investor_profile()
    return jsonify({
        "cash": port.get("cash", 0),
        "holdings": port.get("holdings", []),
        "risk_profile": strat.get("risk_profile", "平衡"),
        "risk_controls": strat.get("risk_controls", {}),
        "investor_profile": investor_profile,
        "universe": [{"code": str(u["code"]), "asset": u.get("asset")} for u in strat.get("universe", [])],
        "watchlist": strat.get("watchlist", []),
    })


@app.post("/api/config")
def save_config():
    body = request.get_json(force=True)
    risk = body.get("risk_profile", "平衡")
    norm = [{"code": str(h.get("code", "")).strip(), "name": h.get("name", ""),
             "shares": _num(h.get("shares")), "target_weight": _num(h.get("target_weight"))}
            for h in body.get("holdings", [])]
    port = {"cash": _num(body.get("cash", 0)), "holdings": norm}
    profile_body = body.get("investor_profile") or {}
    cur = load_investor_profile()  # 现有持久化值作回退：UI 暂无这些输入框时，保存不丢失全组合字段
    investor_profile = {
        "target_annual_return": _num(profile_body.get("target_annual_return", cur["target_annual_return"])),
        "horizon_years": _num(profile_body.get("horizon_years", cur["horizon_years"])),
        "max_acceptable_drawdown": _num(profile_body.get("max_acceptable_drawdown", cur["max_acceptable_drawdown"])),
        "experience_level": profile_body.get("experience_level", cur["experience_level"]),
        "emergency_cash_kept_outside": _num(profile_body.get("emergency_cash_kept_outside", cur["emergency_cash_kept_outside"])),
        "monthly_contribution": _num(profile_body.get("monthly_contribution", cur["monthly_contribution"])),
        "total_assets": _num(profile_body.get("total_assets", cur.get("total_assets", 0))),
        "stable_assets_outside": _num(profile_body.get("stable_assets_outside", cur.get("stable_assets_outside", 0))),
        "stable_assets_yield": _num(profile_body.get("stable_assets_yield", cur.get("stable_assets_yield", 0.025))),
        "planned_etf_capital": _num(profile_body.get("planned_etf_capital", cur.get("planned_etf_capital", 0))),
        "unemployment_monthly_expense": _num(profile_body.get("unemployment_monthly_expense", cur.get("unemployment_monthly_expense", 6000))),
        "unemployment_minimum_monthly_income": _num(profile_body.get("unemployment_minimum_monthly_income", cur.get("unemployment_minimum_monthly_income", 0))),
        "unemployment_runway_years": _num(profile_body.get("unemployment_runway_years", cur.get("unemployment_runway_years", 5))),
        "post_stress_reserve_months": _num(profile_body.get("post_stress_reserve_months", cur.get("post_stress_reserve_months", 12))),
    }
    investor_profile = _profile_with_derived_funding(investor_profile)
    strat = load_yaml(STRATEGY)
    strat["risk_profile"] = risk
    errs = validate_strategy(strat) + validate_config(port, strat) + validate_investor_profile(investor_profile)
    if errs:
        return jsonify({"ok": False, "errors": errs}), 400
    _write_portfolio(port)
    _write_investor_profile(investor_profile)
    _set_risk_profile(risk)
    # 批 2（§0B 阻断项 #3，人在环）：保存设置只持久化 profile/risk/portfolio，**绝不自动重写 target_weight**。
    # 改任意战略输入后，重配走显式三步：/api/strategic/construct（看 diff）→ /api/strategic/apply（指纹+大跳变二次确认）。
    return jsonify({
        "ok": True,
        "strategic_update": {
            "applied": False,
            "manual_apply_required": True,
            "reason": "设置已保存；目标权重保持不变。如调整了战略输入，请到「战略与复盘 → 长期配置是否合理」重新构建模型组合、核对差异后手动应用。",
        },
    })


_FREQ_ZH = {"weekly": "每周", "biweekly": "每两周", "monthly": "每月", "quarterly": "每季"}
_FREQ_GAP = {"weekly": 0, "biweekly": 13, "monthly": 28, "quarterly": 84}


@app.get("/api/rebalance-policy")
def get_rebalance_policy():
    """再平衡策略现状：5/25 阈值 + 当前频率 + 熔断 + 距上次成交天数（驱动「再平衡设置」面板）。"""
    strat = load_yaml(STRATEGY)
    rb = ((strat.get("factors") or {}).get("rebalance") or {})
    rc = strat.get("risk_controls") or {}
    freq = str(rb.get("check_frequency", "weekly")).lower()
    if freq not in _FREQ_GAP:
        freq = "weekly"
    last = latest_execution_date(ROOT)
    days_since = (datetime.date.today() - last).days if last else None
    return jsonify({
        "ok": True,
        "abs_threshold_pp": float(rb.get("abs_threshold_pp", 5)),
        "rel_threshold": float(rb.get("rel_threshold", 0.25)),
        "circuit_breaker_pp": float(rb.get("circuit_breaker_pp", 15)),
        "min_trade_amount": float(rc.get("min_trade_amount", 0) or 0),
        "max_weekly_trade_amount": float(rc.get("max_weekly_trade_amount", 0) or 0),
        "check_frequency": freq,
        "min_gap_days": _FREQ_GAP[freq],
        "days_since_last_rebalance": days_since,
        "options": [{"value": k, "label": _FREQ_ZH[k], "gap_days": _FREQ_GAP[k]}
                    for k in ("weekly", "biweekly", "monthly", "quarterly")],
    })


@app.post("/api/rebalance-frequency")
def set_rebalance_frequency():
    """只改 rebalance.check_frequency（其它再平衡逻辑不动）。不触碰持仓/目标权重。"""
    body = request.get_json(force=True) or {}
    freq = str(body.get("frequency", "")).lower()
    if freq not in _FREQ_GAP:
        return jsonify({"ok": False, "error": "frequency 须为 weekly/biweekly/monthly/quarterly"}), 400
    _set_check_frequency(freq)
    errs = validate_strategy(load_yaml(STRATEGY))
    if errs:  # 不应发生；保险起见回滚信息
        return jsonify({"ok": False, "errors": errs}), 400
    return jsonify({"ok": True, "check_frequency": freq, "label": _FREQ_ZH[freq],
                    "min_gap_days": _FREQ_GAP[freq]})


# §10.4 确定性投影：权威实现在 strategic.py（Track C 唯一来源），此处别名复用。
_deterministic_projection = strategic._deterministic_projection


@app.post("/api/signals")
def run_signals():
    try:
        r = _run_engine_script("signals.py", 240)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "生成超时（数据源较慢），请稍后重试"}), 504
    sp = os.path.join(HERE, "signals.json")
    if r.returncode != 0 or not os.path.exists(sp):
        return jsonify({"ok": False, "error": (r.stderr or r.stdout or "运行失败").strip()}), 500
    with open(sp, encoding="utf-8") as f:
        signals = json.load(f)
    try:
        signals = _apply_execution_quality_gate(signals)   # QDII 溢价/申购 执行质量闸
        with open(sp, "w", encoding="utf-8") as f:          # 回写 signals.json，供数据健康与 CLI 使用
            json.dump(signals, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass  # 闸失败绝不阻断周报
    report = archive_report(signals=signals)               # 用加工后的 signals 归档，复盘与实时一致
    return jsonify({"ok": True, "signals": signals, "report": {"id": report["id"], **report["summary"]}})


@app.post("/api/backtest")
def run_backtest():
    try:
        r = _run_engine_script("backtest.py", 600)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "output": "回测超时，请稍后重试"}), 504
    return jsonify({"ok": r.returncode == 0, "output": (r.stdout or r.stderr).strip()})


@app.post("/api/backtest/json")
def run_backtest_json():
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "backtest.py"), "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "回测超时，请稍后重试"}), 504
    if r.returncode != 0:
        return jsonify({"ok": False, "error": (r.stderr or r.stdout or "回测失败").strip()}), 500
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "回测 JSON 解析失败", "output": r.stdout}), 500
    return jsonify({"ok": True, "result": data, "warnings": (r.stderr or "").strip()})


@app.get("/api/reports")
def reports():
    return jsonify({"ok": True, "reports": list_reports()})


@app.get("/api/performance")
def performance():
    """WS3：真实业绩 TWR/MWR + 沪深300 基准。基准点对齐 NAV 区间，无网络/快照不足时优雅降级。"""
    navs = load_nav_series()
    bench_pts = None
    if len(navs) >= 2:
        try:
            df, _src = fetch_hist("510300")
            if df is not None and not df.empty:
                start, end = navs[0]["as_of"], navs[-1]["as_of"]
                bench_pts = [{"date": str(d.date()), "close": float(c)}
                             for d, c in zip(df["date"], df["close"]) if start <= str(d.date()) <= end]
        except Exception:  # noqa: BLE001
            bench_pts = None
    return jsonify({"ok": True, "performance": performance_summary(bench_pts)})


@app.get("/api/reports/<report_id>")
def report_detail(report_id):
    report = load_report(report_id)
    if not report:
        return jsonify({"ok": False, "error": "找不到周报"}), 404
    return jsonify({"ok": True, "report": report})


@app.get("/api/executions")
def executions():
    cycle = load_active_cycle()
    suggestions = cycle_suggestions(cycle)
    version_status = cycle_version_status(cycle) if cycle else None
    checked = (_recheck_cycle_suggestions(suggestions, (cycle or {}).get("signals") or {})
               if request.args.get("recheck") in ("1", "true", "yes") else suggestions)
    return jsonify({
        "ok": True,
        "cycle": {
            "id": (cycle or {}).get("id"),
            "created_at": (cycle or {}).get("created_at"),
            "status": (cycle or {}).get("cycle_status", "legacy") if cycle else None,
            "version_status": version_status,
        },
        "suggestions": [s for s in checked if s.get("action_status") != "blocked_now"],
        "blocked_suggestions": [s for s in checked if s.get("action_status") == "blocked_now"],
        "decided_suggestions": cycle_suggestions(cycle, include_completed=True) if cycle else [],
        "executions": load_executions(),
    })


@app.get("/api/decision-cycle/active")
def active_decision_cycle():
    cycle = load_active_cycle()
    if not cycle:
        return jsonify({"ok": True, "cycle": None, "suggestions": [], "blocked_suggestions": []})
    checked = _recheck_cycle_suggestions(cycle_suggestions(cycle), cycle.get("signals") or {})
    version_status = cycle_version_status(cycle)
    return jsonify({
        "ok": True,
        "cycle": cycle,
        "version_status": version_status,
        "suggestions": [s for s in checked if s.get("action_status") != "blocked_now"],
        "blocked_suggestions": [s for s in checked if s.get("action_status") == "blocked_now"],
        "decided_suggestions": cycle_suggestions(cycle, include_completed=True),
    })


@app.post("/api/decision-cycle/action")
def decide_cycle_action():
    body = request.get_json(force=True) or {}
    cycle = load_active_cycle()
    if not cycle:
        return jsonify({"ok": False, "error": "当前没有活动决策周期"}), 409
    requested_cycle = str(body.get("cycle_id") or body.get("report_id") or "")
    if requested_cycle and requested_cycle != str(cycle.get("id")):
        return jsonify({"ok": False, "error": "该建议已过期，请重新载入当前决策周期"}), 409
    source = str(body.get("source") or "rebalance")
    code = str(body.get("code") or "").strip()
    side = str(body.get("side") or "buy").lower()
    status = str(body.get("status") or "")
    all_actions = {(str(s.get("source")), str(s.get("code")), str(s.get("side")))
                   for s in cycle_suggestions(cycle, include_completed=True)}
    if (source, code, side) not in all_actions:
        return jsonify({"ok": False, "error": "该动作不属于当前决策周期"}), 400
    try:
        decisions = save_cycle_decision(cycle.get("id"), source, code, side, status, body.get("reason", ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "decisions": decisions, "suggestions": cycle_suggestions(cycle)})


@app.post("/api/executions")
def save_execution():
    body = request.get_json(force=True)
    apply_estimated_fees((body or {}).get("items"))
    try:
        record = save_execution_record(body)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "execution": record})


@app.post("/api/decision-cycle/execute")
def execute_decision_cycle():
    """原子完成：校验成交 → 登记执行 → 更新持仓。失败时回滚刚写入的执行记录。"""
    body = request.get_json(force=True) or {}
    cycle = load_active_cycle()
    if not cycle:
        return jsonify({"ok": False, "error": "当前没有活动决策周期，请先生成本周信号"}), 409
    requested_cycle = str(body.get("report_id") or body.get("cycle_id") or "")
    if requested_cycle and requested_cycle != str(cycle.get("id")):
        return jsonify({"ok": False, "error": "该建议已过期，请关闭调仓窗口并重新载入当前决策周期"}), 409
    version_status = cycle_version_status(cycle)
    if version_status["status"] == "stale":
        labels = {
            "portfolio_version": "持仓配置",
            "strategy_version": "策略配置",
            "investor_profile_version": "个人档案",
        }
        changed = "、".join(labels.get(k, k) for k in version_status["changed"])
        return jsonify({"ok": False, "error": f"{changed}已在本周期生成后发生变化，请重新生成本周信号"}), 409
    items = body.get("items") or []
    pending = _recheck_cycle_suggestions(cycle_suggestions(cycle), cycle.get("signals") or {})
    allowed = {(str(s.get("code")), str(s.get("side"))): s for s in pending
               if s.get("action_status") != "blocked_now"}
    blocked = {(str(s.get("code")), str(s.get("side"))): s for s in pending
               if s.get("action_status") == "blocked_now"}
    for item in items:
        if not str(item.get("suggestion_source") or "").strip():
            continue  # 手动补录的是已发生事实，不按建议状态拦截
        key = (str(item.get("code") or ""), str(item.get("side") or "buy"))
        if key in blocked:
            notes = "；".join(blocked[key].get("execution_quality_notes") or [])
            return jsonify({"ok": False, "error": f"{key[0]} 当前执行质量不通过：{notes}"}), 409
        if key not in allowed:
            return jsonify({"ok": False, "error": f"{key[0]} 已完成、已过期或不属于当前决策周期，请重新打开调仓"}), 409
    # 缺手续费的成交项按佣金(万3/最低5元)估算，使现金扣减与台账记录一致，避免现金逐笔高估。
    apply_estimated_fees(items)
    draft = compute_holdings_draft(load_yaml(PORTFOLIO), [{"items": items}])
    if not draft.get("applied_items"):
        return jsonify({"ok": False, "error": "没有可登记的真实成交"}), 400
    current_port = load_yaml(PORTFOLIO)
    by_shares = {str(h["code"]): h["new_shares"] for h in draft.get("holdings") or []}
    holdings = [{
        "code": str(h.get("code")), "name": h.get("name", ""),
        "shares": by_shares.get(str(h.get("code")), h.get("shares", 0)),
        "target_weight": h.get("target_weight", 0),
    } for h in current_port.get("holdings") or []]
    have = {h["code"] for h in holdings}
    for h in draft.get("holdings") or []:
        code = str(h.get("code"))
        if code not in have:
            holdings.append({"code": code, "name": h.get("name", ""), "shares": h.get("new_shares", 0),
                             "target_weight": h.get("target_weight", 0)})
    new_port = {"cash": draft.get("cash_new", current_port.get("cash", 0)), "holdings": holdings}
    strat = load_yaml(STRATEGY)
    errs = validate_strategy(strat) + validate_config(new_port, strat)
    if errs:
        return jsonify({"ok": False, "error": "成交后持仓校验失败：" + "；".join(errs)}), 400
    record = None
    try:
        record = save_execution_record({
            "report_id": cycle.get("id"),
            "note": body.get("note", ""),
            "items": items,
        })
        _write_portfolio(new_port)
        refresh_cycle_config_versions(cycle)
    except Exception as exc:  # noqa: BLE001
        if record:
            delete_execution_record(record.get("id"))
        return jsonify({"ok": False, "error": f"调仓保存失败，未留下半完成记录：{exc}"}), 500
    return jsonify({"ok": True, "execution": record, "draft": draft, "cycle_id": cycle.get("id")})


@app.get("/api/market/kpis")
def market_kpis():
    strat = load_yaml(STRATEGY)
    port = load_yaml(PORTFOLIO)
    holdings = {str(h["code"]): h.get("name", str(h["code"])) for h in port.get("holdings", [])}
    uni_names = {str(u["code"]): u.get("name") for u in strat.get("universe", []) if u.get("name")}
    codes = request.args.get("codes")
    if codes:
        selected = [(c.strip(), holdings.get(c.strip()) or uni_names.get(c.strip()) or c.strip())
                    for c in codes.split(",") if c.strip()]
    else:
        selected = [(c, name) for c, name in holdings.items()]
    days = int(request.args.get("days", "180"))
    by_code = executions_by_code()
    prefetch_westock([c for c, _ in selected])      # 一次批量 kline，逐只 fetch_hist 命中缓存
    data = [_market_kpis_for(code, name, days=days, executions=by_code) for code, name in selected]
    return jsonify({"ok": True, "items": data, "watchlist": strat.get("watchlist", [])})


@app.get("/api/etf/quality")
def etf_quality():
    strat = load_yaml(STRATEGY)
    port = load_yaml(PORTFOLIO)
    holdings = {str(h["code"]): h.get("name", str(h["code"])) for h in port.get("holdings", [])}
    watch = {str(w["code"]): w.get("name", str(w["code"])) for w in strat.get("watchlist", [])}
    uni_names = {str(u["code"]): u.get("name") for u in strat.get("universe", []) if u.get("name")}
    codes = request.args.get("codes")
    if codes:
        selected = [(c.strip(), holdings.get(c.strip()) or watch.get(c.strip()) or uni_names.get(c.strip()) or c.strip())
                    for c in codes.split(",") if c.strip()]
    else:
        selected = [(c, name) for c, name in holdings.items()]
    asset_of = {str(u["code"]): u.get("asset") for u in strat.get("universe", [])}
    asset_of.update({str(w["code"]): w.get("asset") for w in strat.get("watchlist", [])})
    codes_list = [c for c, _ in selected]
    prefetch_westock(codes_list)            # 批量 kline：曲线/历史/20日成交额
    _prefetch_westock_etf(codes_list)       # 批量 etf 详情：折溢价/规模/申购状态
    snap = None if _westock_covers_all(codes_list) else _etf_spot_snapshot()  # westock 全覆盖则跳过慢快照
    # Track C §8.2：按计划资金规模动态核算硬准入的容量/流动性门槛
    prof = load_investor_profile()
    planned_etf = float(prof.get("planned_etf_capital") or 0) or None
    planned_single = float((strat.get("risk_controls") or {}).get("max_weekly_trade_amount") or 0) or None
    tw_of = {str(h["code"]): float(h.get("target_weight") or 0) for h in port.get("holdings", [])}
    data = [_etf_quality_for(code, name, snap=snap,
                             sensitive=asset_of.get(str(code)) in _PREMIUM_SENSITIVE_ASSETS,
                             planned_position=(planned_etf * tw_of.get(str(code), 0.0)) if planned_etf else None,
                             planned_single_trade=planned_single)
            for code, name in selected]
    return jsonify({"ok": True, "items": data, "premium_source": "live" if snap is not None else "unavailable"})


@app.get("/api/strategic/incumbents")
def strategic_incumbents():
    """Track C §11 incumbent 审视表：对 strategic_policy.roles 的成员逐只跑 准入+产品分，
    汇成 角色/层/权重/区间/单卫星上限/准入/产品分/处置 一览。?te=1 才算跟踪离散度（慢，默认跳过）。"""
    strat, port = load_yaml(STRATEGY), load_yaml(PORTFOLIO)
    sp = strat.get("strategic_policy") or {}
    roles = sp.get("roles") or {}
    candidates = _replacement_candidates(strat)
    member_codes = [str(c) for rc in roles.values() for c in (rc.get("members") or [])]
    review_codes = list(dict.fromkeys(member_codes + [row["code"] for row in candidates]))
    if not member_codes:
        return jsonify({"ok": False, "error": "strategy.yaml 缺 strategic_policy.roles（§18 政策书）"}), 400
    instruments = (strat.get("universe") or []) + (strat.get("watchlist") or [])
    name_of = {str(u["code"]): u.get("name") for u in instruments}
    asset_of = {str(u["code"]): u.get("asset") for u in instruments}
    proxy_of = {str(u["code"]): u.get("proxy_index") for u in instruments}
    prof = load_investor_profile()
    planned_etf = float(prof.get("planned_etf_capital") or 0) or None
    planned_single = float((strat.get("risk_controls") or {}).get("max_weekly_trade_amount") or 0) or None
    tw_of = {str(h["code"]): float(h.get("target_weight") or 0) for h in port.get("holdings", [])}
    want_te = request.args.get("te") in ("1", "true", "yes")
    want_overlap = request.args.get("overlap") in ("1", "true", "yes")
    realtime_ok = _is_trading_session()        # 非交易时段：折溢价不可靠，不据此硬判准入
    prefetch_westock(review_codes)
    _prefetch_westock_etf(review_codes)
    snap = None if _westock_covers_all(review_codes) else _etf_spot_snapshot()
    quality = {}
    for code in review_codes:
        quality[code] = _etf_quality_for(
            code, name_of.get(code), snap=snap,
            sensitive=asset_of.get(code) in _PREMIUM_SENSITIVE_ASSETS,
            planned_position=(planned_etf * tw_of.get(code, 0.0)) if planned_etf else None,
            planned_single_trade=planned_single,
            proxy_index=proxy_of.get(code) if want_te else None,
            realtime_reliable=realtime_ok)
    holdings_by_code = {code: _etf_holdings(code) for code in member_codes} if want_overlap else None
    rows = strategic.assess_incumbents(strat, port, quality, asset_of=asset_of,
                                       holdings_by_code=holdings_by_code)
    catalog = strategic.build_catalog(strat, port)
    _save_strategic_quality_cache(quality)
    for candidate in candidates:
        q = quality.get(candidate["code"]) or {}
        candidate["admitted"] = (q.get("admission") or {}).get("admitted")
        candidate["product_total"] = (q.get("score") or {}).get("total")
        candidate["product_status"] = (q.get("score") or {}).get("status")
    return jsonify({"ok": True, "incumbents": rows, "catalog": catalog["roles"],
                    "replacement_candidates": candidates,
                    "tracking_computed": want_te, "overlap_computed": want_overlap,
                    "trading_session": realtime_ok,
                    "policy_version": sp.get("policy_version")})


@app.post("/api/strategic/roles/introduce")
def strategic_role_introduce():
    body = request.get_json(force=True) or {}
    role, code = str(body.get("role") or ""), str(body.get("code") or "")
    ok, error = _introduce_strategic_role_member(role, code)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "role": role, "code": code})


@app.get("/api/etf/peers")
def etf_peers():
    """§5-3 同类 ETF 发现（自动匹配·需人工确认）：给某只持仓 ETF 列出同跟踪指数的同类，比费率/流动性。
    仅研究发现、不触发任何动作；首次拉全市场清单约 30 秒（之后当日缓存）。"""
    code = str(request.args.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "缺少 code"}), 400
    return jsonify({"ok": True, **_etf_peers(code, load_yaml(STRATEGY))})


def _ensure_signals_fresh_for_construct():
    """构建战略前确保 signals.json 当日、且含前瞻锚定段；否则自动刷新一次（与周报走同一条抓取路径，
    保持同源）。返回状态 dict 供构建端点透出。绝不抛——刷新失败也放行构建（回退冻结假设并如实标注）。

    判据（任一即刷新；三者刷新后均自愈、不抖动）：
      missing         signals.json 缺失/损坏。
      schema_outdated risk_budget 无 `bond_ytm` 键 → 早于「积木式前瞻锚定」特性的旧产物（刷新后必含此键）。
      stale_day       generated_for 早于今天（墙钟生成日；刷新后 == 今天，不会反复触发；节假日亦只刷一次/天）。
    """
    sp = os.path.join(HERE, "signals.json")
    sig = {}
    if os.path.exists(sp):
        try:
            with open(sp, encoding="utf-8") as f:
                sig = json.load(f)
        except Exception:  # noqa: BLE001
            sig = {}
    reason = None
    if not sig:
        reason = "missing"
    elif "bond_ytm" not in (sig.get("risk_budget") or {}):
        reason = "schema_outdated"
    else:
        gen = sig.get("generated_for")
        try:
            gen_d = datetime.datetime.strptime(gen, "%Y-%m-%d").date() if gen else None
        except Exception:  # noqa: BLE001
            gen_d = None
        if gen_d is None or gen_d < datetime.date.today():
            reason = "stale_day"
    if reason is None:
        return {"refreshed": False, "stale_reason": None}
    # 自动刷新一次：跑 signals.py（= /api/signals 的数据路径）+ 执行质量闸回写，但**不归档**——
    # 构建点击不应在复盘里制造周报条目；signals.json 仍与手动刷新逐字节同源。失败吞掉、放行构建。
    try:
        r = _run_engine_script("signals.py", 240)
        if r.returncode != 0 or not os.path.exists(sp):
            return {"refreshed": False, "stale_reason": reason,
                    "refresh_error": (r.stderr or r.stdout or "运行失败").strip()[:200]}
        with open(sp, encoding="utf-8") as f:
            fresh = json.load(f)
        try:
            fresh = _apply_execution_quality_gate(fresh)
            with open(sp, "w", encoding="utf-8") as f:
                json.dump(fresh, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001  闸失败绝不阻断构建
            pass
        return {"refreshed": True, "stale_reason": reason}
    except subprocess.TimeoutExpired:
        return {"refreshed": False, "stale_reason": reason, "refresh_error": "生成超时（数据源较慢）"}
    except Exception as e:  # noqa: BLE001
        return {"refreshed": False, "stale_reason": reason, "refresh_error": str(e)[:200]}


def _construct_frozen_note(sig_refresh):
    """frozen_fallback 时的人话提示：区分『刷新失败』『已自动刷新仍取不到』『本就新鲜仍缺锚』三态。"""
    err = (sig_refresh or {}).get("refresh_error")
    if err:
        return (f"前瞻锚定收益不可用：自动刷新本周信号失败（{err}）。已按冻结假设口径构建；"
                "联网后点『刷新本周信号』再重构可恢复锚定。")
    if (sig_refresh or {}).get("refreshed"):
        return ("前瞻锚定收益不可用：已自动刷新本周信号，但国债/美债 YTM 与估值仍未取到"
                "（可能离线或数据源延迟）。本次按冻结假设口径构建；联网后重新『刷新本周信号』可恢复锚定。")
    return ("前瞻锚定收益不可用（行情未取到），本次按冻结假设口径构建；刷新本周信号后重构可恢复锚定。")


def _run_construct(strat, prof):
    """跑 §10 权威构建并返回 (snap, input_fingerprint)。两个端点（construct/snapshot）共用，避免漂移。"""
    sp = strat.get("strategic_policy") or {}
    asm = load_assumptions(strat)
    scenarios = load_stress_scenarios(strat)
    universe = {str(u["code"]): u for u in strat.get("universe", [])}
    asset_of = {code: item.get("asset") for code, item in universe.items()}
    # 批3：暴露身份优先用显式 exposure_id（绝不退回 proxy_index/code）——避免红利低波被当成沪深300 等误合并。
    exposure_of = {code: item.get("exposure_id") or item.get("index") or item.get("proxy_index") or code
                   for code, item in universe.items()}
    planned = float(prof.get("planned_etf_capital") or 0)
    resilience = strategic.employment_resilience(prof)
    stable = float(resilience["risk_buffer_available"])
    etf_share = planned / (planned + stable) if planned > 0 and (planned + stable) > 0 else 1.0
    target, _ = resolve_policy_number(prof, "target_annual_return", 0.05, lo=0, hi=0.30)
    max_dd, _ = resolve_policy_number(prof, "max_acceptable_drawdown", 0.15, lo=0, hi=0.80)
    # 批3：构建用压力预算与展示用最大回撤解耦——policy.construct_stress_budget 显式设值时用它，否则默认 = max_dd。
    csb = sp.get("construct_stress_budget")
    construct_budget = float(csb) if isinstance(csb, (int, float)) and not isinstance(csb, bool) and 0 <= csb <= 0.80 else max_dd
    incumbent_weights = {str(h.get("code")): float(h.get("target_weight") or 0)
                         for h in load_yaml(PORTFOLIO).get("holdings", [])}
    quality, quality_status = _load_strategic_quality_cache()
    # §8.2 阻断项 #1：质量缓存缺失/过期，或任一角色成员没有准入记录 → fail-closed（不让 apply）。
    member_codes = [str(c) for rc in (sp.get("roles") or {}).values() for c in (rc.get("members") or [])]
    missing_records = sorted({c for c in member_codes if c not in quality})
    quality_block = quality_status in ("missing", "stale") or bool(missing_records)
    # §0C #3：给 construct 接受判定算协方差（长代理面板周频收益，读缓存种子、离线快）；失败则 None，优雅降级。
    covariance = None
    try:
        import backtest as _bt  # noqa: PLC0415  懒加载，避免 app 启动就拉 akshare
        full = _bt.build_full_panel(strat, {c: 1.0 for c in asset_of})
        if full:
            wk = full[0].resample("W").last().pct_change().dropna()
            cr = {c: wk[_bt.FULL_PROXY[c]].tolist() for c in asset_of if _bt.FULL_PROXY.get(c) in wk.columns}
            covariance = strategic.shrinkage_covariance(cr)
    except Exception:  # noqa: BLE001  协方差是增益项，缺了只是退回纯线性压力，不能挡构建
        covariance = None
    # 前瞻锚定收益（积木式）→ **驱动**构建的权重选择（替代冻结假设表）。复用最新 signals.json 的逐只估值
    # + 国债/美债YTM，按 universe 逐只算 expected/expected_conservative；缺 signals.json/取数失败 → 传 None
    # （优化器回退冻结假设）并如实标 frozen_fallback（数据诚实，绝不假装锚定）。逐只值与权重无关、可复现。
    er_cfg = strat.get("expected_return") or {}
    bb_years = er_cfg.get("valuation_reversion_years") or signals.BB_REVERSION_YEARS
    _cap = er_cfg.get("valuation_adj_cap")
    bb_cap = _cap if isinstance(_cap, (int, float)) and not isinstance(_cap, bool) and 0 <= _cap <= 1 else signals.BB_VAL_ADJ_CAP
    bb_erp = er_cfg.get("equity_risk_premium") if isinstance(er_cfg.get("equity_risk_premium"), dict) else {}
    _hc = er_cfg.get("ytm_conservative_haircut")
    bb_ytm_hc = _hc if isinstance(_hc, (int, float)) and not isinstance(_hc, bool) and 0 <= _hc <= 1 else signals.BB_YTM_CONSERVATIVE_HAIRCUT
    # 读 signals.json 取前瞻锚之前，先确保它当日且含锚定段——缺失/旧schema/跨日 → 自动刷新一次
    # （同一条抓取路径，保持同源）。刷新失败/离线则照旧回退冻结假设，note 据 sig_refresh 如实区分三态。
    sig_refresh = _ensure_signals_fresh_for_construct()
    per_val, by, uy, bond_ytm, us_ytm = {}, {}, {}, None, None
    try:
        with open(os.path.join(HERE, "signals.json"), encoding="utf-8") as f:
            _sig = json.load(f)
        per_val = _sig.get("signals") or {}
        _rb_prev = _sig.get("risk_budget") or {}
        by = _rb_prev.get("bond_ytm") or {}
        bond_ytm = by.get("value")
        uy = _rb_prev.get("us_ytm") or {}
        us_ytm = uy.get("value")
    except Exception:  # noqa: BLE001  signals.json 缺失/损坏 → 下面记 frozen_fallback，优化器用冻结假设
        pass

    def _bb(weights):
        hold = [{"code": c, "name": (universe.get(c) or {}).get("name", c), "target_weight": w}
                for c, w in (weights or {}).items() if w and w > 0]
        return signals.building_block_returns(hold, universe, per_val, asm, bond_ytm, by,
                                              reversion_years=bb_years, val_cap=bb_cap,
                                              us_ytm=us_ytm, us_ytm_status=uy, erp=bb_erp,
                                              ytm_haircut=bb_ytm_hc)

    # basis=anchored 只在**至少一腿真拿到前瞻锚**（confidence≠low：债券YTM/估值回归/美债+ERP）时成立；
    # 若 YTM 全失败、估值全缺、QDII 无美债锚（每腿都回退冻结）→ 传 None、口径如实记 frozen_fallback。
    returns_by_code = returns_cons_by_code = None
    construct_return_basis = "frozen_fallback"
    if bond_ytm is not None or us_ytm is not None or bool(per_val):
        _uni_blocks = _bb({c: 1.0 for c in universe})["blocks"]   # 权重无关，取逐只 expected/expected_conservative
        if any(b.get("confidence") != "low" for b in _uni_blocks):
            returns_by_code = {b["code"]: b["expected"] for b in _uni_blocks}
            returns_cons_by_code = {b["code"]: b["expected_conservative"] for b in _uni_blocks}
            construct_return_basis = "anchored"
    snap = strategic.construct_strategic_portfolio(
        sp, returns=asm["returns"], shocks=asm["shocks"], target_return=target,
        default_return=asm["default_return"], default_shock=asm["default_shock"],
        asset_of=asset_of, etf_share=etf_share, max_whole_stress=construct_budget,
        returns_conservative=asm["returns_conservative"], scenarios=scenarios,
        instrument_quality=quality, exposure_of=exposure_of, covariance=covariance,
        incumbent_codes=incumbent_weights, incumbent_weights=incumbent_weights,
        require_quality=quality_block,
        returns_by_code=returns_by_code, returns_conservative_by_code=returns_cons_by_code)
    snap["construct_return_basis"] = construct_return_basis
    snap["signals_refresh"] = sig_refresh   # 本次构建前是否自动刷新了 signals.json（透出，便于排查口径）
    snap["scenarios_count"] = len(scenarios)
    snap["policy_version"] = sp.get("policy_version")
    snap["product_quality_status"] = quality_status
    snap["construct_stress_budget"] = round(construct_budget, 4)   # 批3：构建用压力预算
    snap["display_max_drawdown"] = round(max_dd, 4)                # 展示用最大回撤（与上者已解耦）
    snap["employment_resilience"] = resilience
    snap["quality_gate"] = {"blocked": bool(quality_block), "status": quality_status,
                            "missing_records": missing_records}
    if quality_block:
        if snap.get("validation_status") == "passed":
            snap["validation_status"] = "blocked_quality_data"
        detail = f"质量数据状态={quality_status}"
        if missing_records:
            detail += f"；{len(missing_records)} 个成员无准入记录（{'/'.join(missing_records)}）"
        diags = list(snap.get("constraint_diagnostics") or [])
        diags.insert(0, f"质量数据不足，已禁止自动应用（fail-closed）：{detail}。请到「ETF 准入审视」刷新质量数据后重新构建。")
        snap["constraint_diagnostics"] = diags
    if not resilience["passes"]:
        snap["policy_allocation"] = {}
        snap["instrument_allocation"] = {}
        snap["metrics"] = {}
        snap["validation_status"] = "no_feasible_portfolio"
        snap["constraint_diagnostics"] = [
            f"employment reserve shortfall {resilience['shortfall']:.0f}"
        ]
    # 逐只前瞻预期拆解（展示）：复用上面已喂进优化器的同一套 `_bb`（同源 → 展示数与驱动数自洽），
    # 给「构建组合」与「当前组合」各算一遍积木式前瞻预期年化，口径与周报「目标可行性」统一。
    try:
        if snap.get("instrument_allocation"):
            anc = _bb(snap["instrument_allocation"])
            snap.setdefault("metrics", {})["expected_return_anchored"] = round(anc["blend"], 4)
            snap["metrics"]["expected_return_anchored_conservative"] = round(anc["blend_conservative"], 4)
            snap["expected_return_blocks"] = anc["blocks"]
            snap["bond_ytm"] = by
            snap["us_ytm"] = uy
            snap["expected_return_reversion_years"] = bb_years
        if incumbent_weights:
            snap["incumbent_expected_return_anchored"] = round(_bb(incumbent_weights)["blend"], 4)
    except Exception:  # noqa: BLE001  逐只拆解是展示增益，出错绝不挡构建
        pass
    if construct_return_basis == "frozen_fallback" and snap.get("instrument_allocation"):
        snap["construct_return_note"] = _construct_frozen_note(sig_refresh)
    # 「为什么这样配」结构化解释（用最终 snap + 政策区间/上限 + 限购冻结）。
    snap["rationale"] = strategic.build_construct_rationale(
        sp, snap, name_of={code: item.get("name") for code, item in universe.items()},
        quality=quality, incumbent_weights=incumbent_weights)
    quality_fp = {code: {"admitted": (row.get("admission") or {}).get("admitted"),
                         "score": (row.get("score") or {}).get("total"),
                         "coverage": (row.get("score") or {}).get("coverage")}
                  for code, row in quality.items()}
    # 节奏护栏：前瞻锚定收益按 0.5% 桶进指纹 → 利率/估值跨档才改变构建（随有意义变动呼吸、不被噪声 thrash，
    # 对标机构年度重校）；frozen_fallback 也入指纹（口径切换=输入变化）。
    fp_anchor = {c: round(round(r / 0.005) * 0.005, 4) for c, r in (returns_by_code or {}).items()}
    fp_src = json.dumps({"policy": sp, "returns": asm["returns"], "shocks": asm["shocks"],
                         "scenarios": scenarios, "target": target, "max_dd": max_dd,
                         "etf_share": round(etf_share, 4), "employment_resilience": resilience,
                         "product_quality": quality_fp,
                         "product_quality_status": quality_status,
                         "anchored_returns": fp_anchor, "return_basis": construct_return_basis},
                        sort_keys=True, ensure_ascii=False)
    fingerprint = "sha256:" + hashlib.sha256(fp_src.encode("utf-8")).hexdigest()[:16]
    return snap, fingerprint


@app.get("/api/strategic/quality-status")
def strategic_quality_status():
    """轻量探针：ETF 质量/准入缓存的新鲜度 + 角色成员覆盖（驱动战略流程步骤条的第②步状态，不跑慢的全市场审视）。"""
    strat = load_yaml(STRATEGY)
    sp = strat.get("strategic_policy") or {}
    member_codes = sorted({str(c) for rc in (sp.get("roles") or {}).values() for c in (rc.get("members") or [])})
    items, status = _load_strategic_quality_cache()
    covered = [c for c in member_codes if c in items]
    missing = [c for c in member_codes if c not in items]
    # "可用判定" = 准入有真实结论（通过，或因真实阻断不通过）；"数据缺失" = 关键数据取不到（admitted None/False 但无 blockers）
    usable = 0
    for c in member_codes:
        a = ((items.get(c) or {}).get("admission") or {})
        if a.get("admitted") is True or (a.get("admitted") is False and a.get("blockers")):
            usable += 1
    data_ok = usable >= max(1, round(len(member_codes) * 0.6)) if member_codes else False
    age_days = None
    try:
        with open(STRATEGIC_QUALITY_CACHE, encoding="utf-8") as f:
            gen = float((json.load(f) or {}).get("generated_at_epoch") or 0)
        if gen:
            age_days = round((time.time() - gen) / 86400.0, 1)
    except Exception:  # noqa: BLE001
        age_days = None
    # 既要缓存新鲜、成员齐全，又要数据真的取到（否则准入全是"数据缺失"，构建会 fail-closed）。
    fresh = status == "cached" and not missing and data_ok
    return jsonify({"ok": True, "status": status, "fresh": fresh, "age_days": age_days,
                    "data_ok": data_ok, "usable_count": usable, "trading_session": _is_trading_session(),
                    "member_count": len(member_codes), "covered_count": len(covered),
                    "missing": missing})


@app.get("/api/strategic/construct")
def strategic_construct():
    """Track C §10 权威战略组合构建；默认只展示，用户可通过 apply 端点主动确认应用。"""
    strat, port, prof = load_yaml(STRATEGY), load_yaml(PORTFOLIO), load_investor_profile()
    if not (strat.get("strategic_policy") or {}).get("roles"):
        return jsonify({"ok": False, "error": "strategy.yaml 缺 strategic_policy.roles（§18 政策书）"}), 400
    name_of = {str(u["code"]): u.get("name") for u in strat.get("universe", [])}
    snap, fingerprint = _run_construct(strat, prof)
    cur = {str(h["code"]): float(h.get("target_weight") or 0) for h in port.get("holdings", [])}
    built = snap.get("instrument_allocation") or {}
    snap["comparison"] = [
        {"code": c, "name": name_of.get(c) or c, "current": round(cur.get(c, 0.0), 4),
         "constructed": round(built.get(c, 0.0), 4), "delta": round(built.get(c, 0.0) - cur.get(c, 0.0), 4)}
        for c in sorted(set(cur) | set(built))]
    snap["mode"] = "authoritative"
    snap["input_fingerprint"] = fingerprint
    return jsonify({"ok": True, "construct": snap})


@app.post("/api/strategic/apply")
def strategic_apply():
    """用户主动确认应用权威战略构建结果（§8.2 阻断项 #4 + 少额真金护栏）。

    硬门槛：① 客户端回显其评审过的 input_fingerprint，服务端重算、不一致回 409（防应用未看过的版本）；
    ② passed + 二次配置校验；③ 单产品目标权重跳变超阈值需 confirm_large_moves 二次确认。
    """
    body = request.get_json(silent=True) or {}
    strat, port, prof = load_yaml(STRATEGY), load_yaml(PORTFOLIO), load_investor_profile()
    if not (strat.get("strategic_policy") or {}).get("roles"):
        return jsonify({"ok": False, "errors": ["strategy.yaml 缺 strategic_policy.roles"]}), 400
    snap, fingerprint = _run_construct(strat, prof)
    quality_status = snap.get("product_quality_status")
    reviewed = body.get("input_fingerprint")
    if not reviewed:
        return jsonify({"ok": False, "error": "缺少已审阅的构建指纹，请先在「模型组合」视图查看后再应用",
                        "input_fingerprint": fingerprint, "product_quality_status": quality_status}), 400
    if reviewed != fingerprint:
        return jsonify({"ok": False, "stale": True,
                        "error": "构建输入已变化（配置/行情/质量数据已更新），请重新查看最新构建结果后再应用",
                        "input_fingerprint": fingerprint, "product_quality_status": quality_status,
                        "construct": snap}), 409
    cur = {str(h["code"]): float(h.get("target_weight") or 0) for h in port.get("holdings", [])}
    built = snap.get("instrument_allocation") or {}
    moves = _large_target_moves(cur, built) if built else []
    if moves and body.get("confirm_large_moves") is not True:
        return jsonify({"ok": False, "needs_confirmation": True, "large_moves": moves,
                        "threshold": LARGE_MOVE_THRESHOLD, "input_fingerprint": fingerprint,
                        "product_quality_status": quality_status}), 409
    applied, diags, allocation = _apply_constructed_allocation(port, strat, snap)
    if not applied:
        return jsonify({"ok": False, "validation_status": snap.get("validation_status"),
                        "errors": diags, "product_quality_status": quality_status,
                        "construct": snap}), 400
    # §0B 审计痕迹：组合已落盘 → 记一条 mode=applied（fingerprint/policy/quality/old→new diff/源/时间）。
    # 审计写失败不回滚已应用的组合（组合即真相、可由 git diff 复核），但把错误透传给前端可见。
    try:
        audit = save_strategic_apply(
            fingerprint=fingerprint,
            policy_version=(strat.get("strategic_policy") or {}).get("policy_version"),
            quality_status=quality_status, old_weights=cur, new_weights=allocation,
            source="api/strategic/apply")
    except Exception as exc:  # noqa: BLE001
        audit = {"error": f"审计记录写入失败：{exc}"}
    return jsonify({"ok": True, "validation_status": snap.get("validation_status"),
                    "allocation": allocation, "input_fingerprint": fingerprint,
                    "product_quality_status": quality_status, "audit": audit})


@app.get("/api/strategic/applies")
def strategic_applies():
    """§0B 审计痕迹：列出历次 mode=applied 应用记录（最近在前）。"""
    return jsonify({"ok": True, "applies": load_strategic_applies(limit=50)})


@app.post("/api/strategic/backtest")
def strategic_backtest():
    """§12.3/§16.3 战略组合对比回测（构建 vs 当前 vs 简化基准 + 风险贡献 + 稳健性）。子进程跑、较慢。"""
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "backtest.py"), "--strategic", "--json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=600, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "战略回测超时，请稍后重试"}), 504
    if r.returncode != 0:
        return jsonify({"ok": False, "error": (r.stderr or r.stdout or "回测失败").strip()[:500]}), 500
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "回测 JSON 解析失败", "output": (r.stdout or "")[:300]}), 500
    if data.get("error"):
        return jsonify({"ok": False, "error": data["error"]}), 400
    return jsonify({"ok": True, "result": data})


@app.post("/api/strategic/evidence")
def strategic_evidence():
    """§0C #2 证据台账 + 真 walk-forward（每条'更优'主张 → 证据档 + 样本外结论）。子进程跑、较慢。"""
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "backtest.py"), "--evidence", "--json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=600, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "证据台账超时，请稍后重试"}), 504
    if r.returncode != 0:
        return jsonify({"ok": False, "error": (r.stderr or r.stdout or "运行失败").strip()[:500]}), 500
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "证据台账 JSON 解析失败", "output": (r.stdout or "")[:300]}), 500
    return jsonify({"ok": True, "result": data})


@app.get("/api/etf/spot")
def etf_spot():
    """ETF 盘中估值价，供首页浮动盈亏用。westock 优先（批量 etf 详情 + kline 最新价），akshare 快照兜底。"""
    port = load_yaml(PORTFOLIO)
    holdings = {str(h["code"]): h.get("name", str(h["code"])) for h in port.get("holdings", [])}
    codes = request.args.get("codes")
    if codes:
        selected = [(c.strip(), holdings.get(c.strip(), c.strip())) for c in codes.split(",") if c.strip()]
    else:
        selected = [(c, name) for c, name in holdings.items()]
    codes_list = [c for c, _ in selected]
    prefetch_westock(codes_list)            # 批量 kline（最新价/成交额兜底）
    _prefetch_westock_etf(codes_list)       # 批量 etf 详情（折溢价/规模/申购）
    snap = None if _westock_covers_all(codes_list) else _etf_spot_snapshot(max_age=0)  # westock 全覆盖则跳过慢快照
    items = []
    for code, name in selected:
        m, _extra = _quality_metrics(code, snap, False)   # westock 优先、akshare 兜底
        last = m.get("price")
        if last is None:                                  # 再退到 westock kline 最新收盘
            df, _src = fetch_hist(code)
            if df is not None and not df.empty:
                last = float(df["close"].iloc[-1])
        items.append({
            "code": code,
            "name": name,
            "last_price": last,
            "iopv": m.get("iopv"),
            "premium_pct": round(m["premium"] * 100, 2) if m.get("premium") is not None else None,
            "turnover_1d": round(m["turnover"], 0) if m.get("turnover") is not None else None,
        })
    has_price = any(it["last_price"] is not None for it in items)
    return jsonify({
        "ok": True,
        "items": items,
        "source": "live" if has_price else "unavailable",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })


@app.get("/api/review/monthly")
def review_monthly():
    return jsonify({"ok": True, "months": monthly_review()})


@app.get("/api/portfolio/draft")
def portfolio_draft():
    """据最近一次执行记录把当前持仓推算成"成交后草稿"，供前端核对后手动填入。不写文件。"""
    port = load_yaml(PORTFOLIO)
    recs = load_executions()          # 已按文件名倒序（最新在前）
    latest = recs[0] if recs else None
    draft = compute_holdings_draft(port, [latest] if latest else [])
    draft["based_on"] = (latest or {}).get("id") or (latest or {}).get("created_at")
    return jsonify({"ok": True, "draft": draft, "has_executions": bool(latest)})


@app.post("/api/portfolio/preview")
def portfolio_preview():
    """据“正在填写、尚未保存”的执行明细，实时推算成交后持仓。纯预览、不写文件。
    复用 reports.compute_holdings_draft，与已存草稿同一套引擎数学（单一事实源）。"""
    body = request.get_json(force=True) or {}
    items = body.get("items") or []
    port = load_yaml(PORTFOLIO)
    draft = compute_holdings_draft(port, [{"items": items}])
    return jsonify({"ok": True, "draft": draft})


@app.get("/api/watchlist/learning")
def watchlist_learning_api():
    return jsonify({"ok": True, "items": watchlist_learning(load_yaml(STRATEGY))})


@app.post("/api/watchlist/learning/ack")
def watchlist_learning_ack():
    body = request.get_json(force=True)
    code = str(body.get("code", "")).strip()
    watch_codes = {str(w.get("code")) for w in (load_yaml(STRATEGY).get("watchlist") or [])}
    if code not in watch_codes:
        return jsonify({"ok": False, "error": "该代码不在观察池中"}), 400
    try:
        rec = save_ack(code, acknowledged=bool(body.get("acknowledged", True)), notes=body.get("notes", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "ack": rec})


@app.get("/api/health/data")
def data_health():
    return jsonify({"ok": True, "health": _data_health()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5057"))   # 避开 macOS 默认占用的 5000(AirPlay)
    print("=" * 56)
    print(f"  投资周报驾驶舱  →  http://127.0.0.1:{port}")
    print("  Ctrl+C 退出 ｜ 改了代码后需重启本进程才会生效")
    print("=" * 56)
    app.run(host="127.0.0.1", port=port, debug=False)
