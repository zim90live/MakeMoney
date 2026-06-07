#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────
# 【单一事实源 / SINGLE SOURCE OF TRUTH】 本地 Web 驾驶舱（UI 层）。
# 不重写任何策略逻辑：编辑配置后仍调用 engine/signals.py、engine/backtest.py。
#   启动： python3 engine/app.py   →   打开 http://127.0.0.1:5057
# ─────────────────────────────────────────────────────────────────────────
"""投资周报驾驶舱：网页上编辑持仓/风险偏好、一键生成本周信号、跑回测，不必手改 yaml。"""
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

sys.path.insert(0, HERE)
import yaml  # noqa: E402
from signals import estimate_target_stress_drawdown, expected_etf_return, load_assumptions, load_stress_scenarios, resolve_policy_number, validate_config, validate_strategy  # noqa: E402  复用同一套校验
from signals import fetch_hist, prefetch_westock  # noqa: E402
from reports import (  # noqa: E402
    archive_report, compute_holdings_draft, cycle_suggestions,
    cycle_version_status,
    delete_execution_record, executions_by_code, list_reports, load_active_cycle,
    load_executions, load_nav_series, load_report, monthly_review,
    performance_summary,
    refresh_cycle_config_versions, save_cycle_decision, save_execution_record,
)
from learning import save_ack, watchlist_learning  # noqa: E402
import strategic  # noqa: E402  Track C 战略层纯函数（ETF 费率解析 + §8.2 硬准入）

app = Flask(__name__, static_folder=None)

DEFAULT_INVESTOR_PROFILE = {
    "target_annual_return": 0.05,
    "horizon_years": 5,
    "max_acceptable_drawdown": 0.15,
    "experience_level": "beginner",
    "emergency_cash_kept_outside": 0,
    "monthly_contribution": 0,
    "stable_assets_outside": 0,     # 场外稳健桶（活期/固收/定存）：让算法知道有这笔缓冲，做全组合口径
    "stable_assets_yield": 0.025,   # 稳健桶假设年化（仅用于混合收益展示）
    "planned_etf_capital": 0,       # ETF 风险桶目标上限：用于缓冲比例与目标权重测算（0=不启用缓冲，按 ETF 桶自身回撤预算）
}


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
        return dict(DEFAULT_INVESTOR_PROFILE)
    data = load_yaml(INVESTOR_PROFILE) or {}
    return {**DEFAULT_INVESTOR_PROFILE, **data}


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
        ("stable_assets_outside", "场外稳健桶"),
        ("planned_etf_capital", "ETF 风险桶目标上限"),
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
        f"stable_assets_outside: {profile.get('stable_assets_outside', 0)}",
        f"stable_assets_yield: {profile.get('stable_assets_yield', 0.025)}",
        f"planned_etf_capital: {profile.get('planned_etf_capital', 0)}",
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


def _apply_execution_quality_gate(signals):
    """执行质量闸：对买入类动作（加仓 / 首次建仓）按当前实时折溢价 + 申购状态裁决。
    issue 档 → 降级 actionable=False 并补 blocked_reasons（移入"被拦截"区）；
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


def _etf_quality_for(code, name=None, snap=None, sensitive=False,
                     planned_position=None, planned_single_trade=None, proxy_index=None):
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
        "stable_assets_outside": _num(profile_body.get("stable_assets_outside", cur.get("stable_assets_outside", 0))),
        "stable_assets_yield": _num(profile_body.get("stable_assets_yield", cur.get("stable_assets_yield", 0.025))),
        "planned_etf_capital": _num(profile_body.get("planned_etf_capital", cur.get("planned_etf_capital", 0))),
    }
    strat = load_yaml(STRATEGY)
    strat["risk_profile"] = risk
    errs = validate_strategy(strat) + validate_config(port, strat) + validate_investor_profile(investor_profile)
    if errs:
        return jsonify({"ok": False, "errors": errs}), 400
    _write_portfolio(port)
    _write_investor_profile(investor_profile)
    _set_risk_profile(risk)
    return jsonify({"ok": True})


# §10.4 确定性投影：权威实现在 strategic.py（Track C 唯一来源），此处别名复用。
_deterministic_projection = strategic._deterministic_projection


def _strategic_metrics(items, uni_dict, asm, etf_share):
    """从 items(含 suggested_weight) 重算全部风险/收益指标（纯函数）。任何修改权重后都必须重算。"""
    rows = [{"code": it["code"], "name": it.get("name", it["code"]),
             "target_weight": float(it.get("suggested_weight") or 0)} for it in items]
    stress, contribs = estimate_target_stress_drawdown(rows, uni_dict, asm["shocks"], asm["default_shock"])
    exp = expected_etf_return(rows, uni_dict, asm["returns"], asm["default_return"])
    return {"stress": stress, "contribs": contribs, "whole_stress": stress * etf_share, "expected_return": exp}


def _validate_strategic(items, whole_stress, max_dd, policy_status=None):
    """Track C §10.12/§16.1：对最终权重校验硬约束 → (validation_status, constraint_diagnostics)。

    取整/政策闸/产品替换后均须调用，绝不展示修改前的指标。容差吸收 1pp 量化噪声但抓住真实越界。
    """
    diags = []
    wsum = sum(float(it.get("suggested_weight") or 0) for it in items)
    if abs(wsum - 1.0) > 1e-3:
        diags.append(f"建议权重合计 {wsum:.3f} ≠ 1.0")
    if any(float(it.get("suggested_weight") or 0) < -1e-9 for it in items):
        diags.append("存在负权重")
    if whole_stress > max_dd + 5e-3:
        diags.append(f"全组合压力回撤约 {whole_stress:.1%} 超过可接受回撤预算 {max_dd:.1%}")
    invalid = [k for k, v in (policy_status or {}).items() if v == "invalid"]
    if invalid:
        diags.append("非法策略输入已回退默认值，请修正：" + "、".join(invalid))
    return ("passed" if not diags else "violated"), diags


def _suggest_target_weights(port, strat, profile):
    """缓冲感知的建议权重：基于整个 universe（含未持有品种）给出目标权重。

    核心：场外稳健桶是安全垫——它让 ETF 桶可以为目标年化适度加股，
    约束条件是"全组合压力回撤 ≤ max_acceptable_drawdown"，而非"ETF 桶自身 ≤ max_dd"。
    建议永不自动生效；权重不含交易动作。
    """
    uni_list = strat.get("universe", []) or []
    uni_dict = {str(u["code"]): u for u in uni_list}
    asset_of = {str(u["code"]): u.get("asset") for u in uni_list}
    holdings_by_code = {str(h.get("code")): h for h in (port.get("holdings") or [])}

    def _name(code):
        return (uni_dict.get(code) or {}).get("name") or (holdings_by_code.get(code) or {}).get("name") or code

    # Track C §5.2：缺失→默认(标记 defaulted)；合法 0 保留；非法→默认(标记 invalid)，绝不用 `or 默认` 吞掉合法 0%。
    max_dd, _mdd_status = resolve_policy_number(profile, "max_acceptable_drawdown", 0.15, lo=0.0, hi=0.80)  # 全组合口径
    target_return, _tar_status = resolve_policy_number(profile, "target_annual_return", 0.05, lo=0.0, hi=0.30)  # ETF 风险桶
    horizon, _hor_status = resolve_policy_number(profile, "horizon_years", 5, lo=1, hi=50)
    experience = str(profile.get("experience_level") or "beginner")
    _policy_input_status = {"max_acceptable_drawdown": _mdd_status,
                            "target_annual_return": _tar_status, "horizon_years": _hor_status}
    stable = float(profile.get("stable_assets_outside") or 0)
    planned_etf = float(profile.get("planned_etf_capital") or 0)

    # 缓冲比例：ETF 桶在全组合中的占比。planned_etf 缺省时退化为"无缓冲"(=1)。
    etf_share = planned_etf / (planned_etf + stable) if planned_etf > 0 and (planned_etf + stable) > 0 else 1.0
    # 全组合回撤预算折算到 ETF 桶：whole_dd = etf_dd * etf_share → etf_dd_budget = max_dd / etf_share。
    etf_dd_budget = min(max_dd / etf_share if etf_share > 0 else max_dd, 0.40)  # 0.40 理智上限，再厚缓冲也不满仓权益

    # 各 sleeve 假设：(假设年化, 压力冲击)。WS4 单一来源——从 signals.load_assumptions 取（含 strategy.yaml 覆盖），
    # 不再本地写死一份；与 estimate_target_stress_drawdown / expected_etf_return 同源。
    _asm = load_assumptions(strat)
    SLEEVE = {a: (_asm["returns"].get(a, _asm["default_return"]), _asm["shocks"].get(a, _asm["default_shock"]))
              for a in ("bond", "equity", "equity_defensive", "gold",
                        "global_equity", "global_growth", "china_growth")}
    EQUITY_SPLIT = {  # 权益桶内部相对配比（只保留 universe 里实际存在的 sleeve）
        "equity": 0.35, "global_equity": 0.25, "global_growth": 0.15, "china_growth": 0.15, "equity_defensive": 0.10,
    }
    present = set(asset_of.values())
    eq_split = {k: v for k, v in EQUITY_SPLIT.items() if k in present}
    ssum = sum(eq_split.values()) or 1.0
    eq_split = {k: v / ssum for k, v in eq_split.items()}
    gold_w = 0.08 if "gold" in present else 0.0

    def asset_weights_for(equity_total):
        w = {a: 0.0 for a in SLEEVE}
        for a, frac in eq_split.items():
            w[a] = equity_total * frac
        w["gold"] = min(gold_w, max(0.0, 1.0 - equity_total))
        w["bond"] = max(0.0, 1.0 - equity_total - w["gold"])
        return w

    def stress_of(w):
        return abs(sum(w[a] * SLEEVE[a][1] for a in w))

    def return_of(w):
        return sum(w[a] * SLEEVE[a][0] for a in w)

    # 在 ETF 桶回撤预算内，尽量提高权益直到逼近目标年化（缓冲越厚，可提得越高）
    e_cap = {"beginner": 0.65, "intermediate": 0.85, "advanced": 0.95}.get(experience, 0.85)
    best_e, e = 0.0, 0.0
    while e <= e_cap + 1e-9:
        w = asset_weights_for(e)
        if stress_of(w) > etf_dd_budget:
            break
        best_e = e
        if return_of(w) >= target_return:
            break
        e += 0.01
    asset_w = asset_weights_for(best_e)
    expected_return = return_of(asset_w)

    # 把每个资产类别的权重分摊到该类别的具体 ETF 上
    codes_by_asset = {}
    for u in uni_list:
        codes_by_asset.setdefault(u.get("asset"), []).append(str(u["code"]))
    rows = [{"code": str(u["code"]), "name": _name(str(u["code"])),
             "target_weight": asset_w.get(u.get("asset"), 0.0) / max(1, len(codes_by_asset.get(u.get("asset"), [1])))}
            for u in uni_list]
    total_w = sum(r["target_weight"] for r in rows) or 1.0
    for r in rows:
        r["target_weight"] /= total_w
    # Track C §10.4：确定性投影（最大余数法，合计恰为 1、各项≥0、不塞最大项）。
    rounded = _deterministic_projection([r["target_weight"] for r in rows], step=0.01)
    for r, w in zip(rows, rounded):
        r["target_weight"] = w

    stress, contribs = estimate_target_stress_drawdown(rows, uni_dict, _asm["shocks"], _asm["default_shock"])
    whole_stress = stress * etf_share

    # WS2：每只 ETF / sleeve 的"为什么是这个权重"理由（确定性，复用本函数已算好的局部量；只读配置不读实时信号，不编分位）。
    _contrib_by_code = {c["code"]: c.get("contribution", 0) for c in contribs}
    _ROLE = {"bond": "压舱/缓冲", "gold": "对冲/分散", "equity": "A股核心权益", "equity_defensive": "低波防御",
             "global_equity": "全球分散", "global_growth": "成长引擎(QDII)", "china_growth": "国内成长"}

    def _item_reason(code, asset):
        parts = [f"角色：{_ROLE.get(asset, asset or '其它')}"]
        if asset in eq_split:
            parts.append(f"权益桶约 {best_e:.0%}，本类内配比 {eq_split[asset]:.0%}")
        elif asset == "bond":
            parts.append("残差压舱、承接非权益权重")
        elif asset == "gold":
            parts.append(f"固定约 {gold_w:.0%} 分散对冲")
        contrib = _contrib_by_code.get(code)
        if contrib:
            parts.append(f"压力情景贡献回撤约 {abs(contrib):.1%}")
        if asset in ("global_equity", "global_growth"):
            parts.append("QDII 有溢价/汇率/额度风险")
        if asset in ("global_growth", "china_growth"):
            parts.append("成长品种波动更大")
        er = _asm["returns"].get(asset)
        src = (_asm["meta"].get(asset) or {}).get("source")
        if er is not None:
            parts.append(f"假设年化约 {er:.1%}" + (f"（来源：{src}）" if src else "") + "，非承诺")
        return "；".join(parts)

    reasons = []
    if stable > 0:
        reasons.append(f"已知场外稳健桶约 ¥{stable:,.0f}（ETF 桶约占全组合 {etf_share:.0%}）→ "
                       f"ETF 桶回撤预算放宽到约 {etf_dd_budget:.0%}，可为目标适度加股。")
    else:
        reasons.append("未记录场外稳健桶，按 ETF 桶自身回撤预算配置（更保守）。")
    reasons.append(f"建议权益合计约 {best_e:.0%}；ETF 桶现实预期年化约 {expected_return:.1%}。")
    if expected_return < target_return - 0.005:
        reasons.append(f"即便加到回撤预算/理智上限，现实预期（约 {expected_return:.1%}）仍低于目标 {target_return:.0%}"
                       "——目标偏进取，需接受股票级波动，且非承诺。")
    reasons.append(f"目标组合压力回撤：ETF 桶约 {stress:.0%}，折算全组合约 {whole_stress:.0%}（预算 {max_dd:.0%}）。")
    if horizon < 3:
        reasons.append("注意：投资年限较短，激进配置遇回撤可能来不及恢复。")

    current = {str(h.get("code")): float(h.get("target_weight") or 0) for h in (port.get("holdings") or [])}
    items = [{
        "code": r["code"], "name": r["name"], "asset": asset_of.get(r["code"]),
        "current_weight": round(current.get(r["code"], 0), 4),
        "suggested_weight": round(float(r["target_weight"]), 4),
        "delta": round(float(r["target_weight"]) - current.get(r["code"], 0), 4),
        "reason": _item_reason(r["code"], asset_of.get(r["code"])),
    } for r in rows]
    # Track C §10.12/§16.1：取整后立即重验证全部硬约束（合计/非负/压力预算/输入合法性）。
    v_status, v_diags = _validate_strategic(items, whole_stress, max_dd, _policy_input_status)
    return {
        "items": items,
        "stress_drawdown": round(stress, 4),                          # ETF 桶口径（兼容旧字段）
        "etf_stress_drawdown": round(stress, 4),
        "whole_portfolio_stress_drawdown": round(whole_stress, 4),
        "etf_share": round(etf_share, 4),
        "etf_drawdown_budget": round(etf_dd_budget, 4),
        "expected_etf_return": round(expected_return, 4),
        "target_annual_return": round(target_return, 4),
        "max_acceptable_drawdown": round(max_dd, 4),          # Track C：供政策闸重算与门控复用
        "suggested_equity_total": round(best_e, 4),
        "stress_contributions": contribs,
        "assumptions_meta": _asm["meta"],     # WS4：每类假设的来源/备注（UI 出处展示，WS2 理由引用）
        "policy_input_status": _policy_input_status,          # Track C §5.2：ok/defaulted/invalid
        "validation_status": v_status,                        # Track C §16.4：passed/violated → 门控应用
        "constraint_diagnostics": v_diags,
        "reasons": reasons,
        "warnings": ["建议权重不会自动生效；点击“应用建议权重”后才会写入本地组合配置。",
                     "新增全球/成长品种波动更大，QDII 有溢价/汇率/额度风险；建议分批小额起步。"],
    }


def _load_flags():
    try:
        with open(os.path.join(HERE, "flags.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"flags": []}


def _policy_restricted_codes(flags_obj):
    """命中政策闸的标的 → {code: 原因}。仅「类别=政策风险 且 方向=利空 且 置信度=高」三者同时满足才算，
    避免被低置信度传闻误伤。"""
    out = {}
    for f in (flags_obj or {}).get("flags") or []:
        if (f.get("category") == "政策风险" and f.get("direction") == "利空"
                and f.get("confidence") == "高"):
            reason = f.get("title") or "高置信度政策利空"
            for code in f.get("affected_assets") or []:
                out.setdefault(str(code), reason)
    return out


def _apply_policy_gate(suggestion, flags_obj, strat=None):
    """政策闸：命中高置信度政策利空的标的，冻结其建议权重不超过当前持仓权重（=不建议加仓，
    但允许引擎自身的减配），释放出来的权重按比例分给未受限标的、合计仍≈1；标注 policy_restricted/
    policy_note 并置 policy_gated。就地修改 suggestion。无命中则原样返回（平时不打扰）。

    Track C §10.11/§16.1：改动权重后**必须重算全部指标并重新验证**，绝不沿用改前的过期风险数字。
    注：当前「按比例分给未受限标的」是过渡实现（§10.2 反模式），Phase C 将换成
    受角色约束的确定性投影；本阶段先确保重算+门控不让过期/越界指标蒙混过关。"""
    restricted = _policy_restricted_codes(flags_obj)
    items = suggestion.get("items") or []
    if not restricted or not items:
        return suggestion
    freed, touched = 0.0, []
    for it in items:
        code = str(it.get("code"))
        if code in restricted:
            cur = float(it.get("current_weight") or 0)
            sug = float(it.get("suggested_weight") or 0)
            if sug > cur:                       # 想加仓 → 冻结到当前权重
                freed += sug - cur
                it["suggested_weight"] = round(cur, 4)
                it["delta"] = 0.0
            it["policy_restricted"] = True
            it["policy_note"] = restricted[code]
            if it.get("reason"):
                it["reason"] += "；政策受限：冻结为不建议加仓"
            touched.append(it.get("name") or code)
    non = [it for it in items if str(it.get("code")) not in restricted]
    nonsum = sum(float(it.get("suggested_weight") or 0) for it in non)
    if freed > 1e-9 and non and nonsum > 1e-9:
        for it in non:
            it["suggested_weight"] = round(
                float(it["suggested_weight"]) + freed * float(it["suggested_weight"]) / nonsum, 4)
            it["delta"] = round(float(it["suggested_weight"]) - float(it.get("current_weight") or 0), 4)
        resid = round(1 - sum(float(it.get("suggested_weight") or 0) for it in items), 4)
        if abs(resid) >= 1e-9:                  # 修正四舍五入残差到最大未受限项，合计回到 1
            big = max(non, key=lambda it: float(it.get("suggested_weight") or 0))
            big["suggested_weight"] = round(float(big["suggested_weight"]) + resid, 4)
            big["delta"] = round(float(big["suggested_weight"]) - float(big.get("current_weight") or 0), 4)
    if touched:
        suggestion["policy_gated"] = True
        suggestion["policy_restricted_codes"] = sorted(restricted.keys())
        suggestion.setdefault("warnings", [])
        suggestion["warnings"].insert(
            0, "⚠️ 政策闸：" + "、".join(map(str, touched))
            + " 命中高置信度政策利空，已冻结建议权重为不超过当前（即不建议加仓），释放的权重按比例分给其它品种；可点“忽略政策限制”按原模型重算。")
        # Track C：政策闸改权重后重算全部指标 + 重新验证（消除过期风险数字）。
        asm = load_assumptions(strat or {})
        uni_dict = {str(it.get("code")): {"asset": it.get("asset")} for it in items}
        etf_share = float(suggestion.get("etf_share") or 1.0)
        max_dd = float(suggestion.get("max_acceptable_drawdown") or 0.15)
        m = _strategic_metrics(items, uni_dict, asm, etf_share)
        suggestion["stress_drawdown"] = round(m["stress"], 4)
        suggestion["etf_stress_drawdown"] = round(m["stress"], 4)
        suggestion["whole_portfolio_stress_drawdown"] = round(m["whole_stress"], 4)
        suggestion["expected_etf_return"] = round(m["expected_return"], 4)
        suggestion["stress_contributions"] = m["contribs"]
        v_status, v_diags = _validate_strategic(
            items, m["whole_stress"], max_dd, suggestion.get("policy_input_status"))
        suggestion["validation_status"] = v_status
        suggestion["constraint_diagnostics"] = v_diags
        suggestion["metrics_recomputed_after_gate"] = True
    return suggestion


@app.get("/api/portfolio/target-suggestion")
def target_suggestion():
    port, strat = load_yaml(PORTFOLIO), load_yaml(STRATEGY)
    suggestion = _suggest_target_weights(port, strat, load_investor_profile())
    if request.args.get("ignore_policy") not in ("1", "true", "yes"):
        suggestion = _apply_policy_gate(suggestion, _load_flags(), strat=strat)
    return jsonify({"ok": True, "suggestion": suggestion})


@app.get("/api/strategy-review/target-suggestion")
def strategy_review_target_suggestion():
    """低频策略审视入口；与周度执行流分离。"""
    response = target_suggestion()
    payload = response.get_json()
    payload["review"] = {"cadence": "monthly_or_quarterly", "execution_scope": "strategic"}
    return jsonify(payload)


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
    member_codes = [str(c) for rc in roles.values() for c in (rc.get("members") or [])]
    if not member_codes:
        return jsonify({"ok": False, "error": "strategy.yaml 缺 strategic_policy.roles（§18 政策书）"}), 400
    name_of = {str(u["code"]): u.get("name") for u in strat.get("universe", [])}
    asset_of = {str(u["code"]): u.get("asset") for u in strat.get("universe", [])}
    proxy_of = {str(u["code"]): u.get("proxy_index") for u in strat.get("universe", [])}
    prof = load_investor_profile()
    planned_etf = float(prof.get("planned_etf_capital") or 0) or None
    planned_single = float((strat.get("risk_controls") or {}).get("max_weekly_trade_amount") or 0) or None
    tw_of = {str(h["code"]): float(h.get("target_weight") or 0) for h in port.get("holdings", [])}
    want_te = request.args.get("te") in ("1", "true", "yes")
    want_overlap = request.args.get("overlap") in ("1", "true", "yes")
    prefetch_westock(member_codes)
    _prefetch_westock_etf(member_codes)
    snap = None if _westock_covers_all(member_codes) else _etf_spot_snapshot()
    quality = {}
    for code in member_codes:
        quality[code] = _etf_quality_for(
            code, name_of.get(code), snap=snap,
            sensitive=asset_of.get(code) in _PREMIUM_SENSITIVE_ASSETS,
            planned_position=(planned_etf * tw_of.get(code, 0.0)) if planned_etf else None,
            planned_single_trade=planned_single,
            proxy_index=proxy_of.get(code) if want_te else None)
    holdings_by_code = {code: _etf_holdings(code) for code in member_codes} if want_overlap else None
    rows = strategic.assess_incumbents(strat, port, quality, asset_of=asset_of,
                                       holdings_by_code=holdings_by_code)
    catalog = strategic.build_catalog(strat, port)
    return jsonify({"ok": True, "incumbents": rows, "catalog": catalog["roles"],
                    "tracking_computed": want_te, "overlap_computed": want_overlap,
                    "policy_version": sp.get("policy_version")})


@app.get("/api/strategic/construct")
def strategic_construct():
    """Track C §10 权威战略组合构建（**shadow**：只展示、不替代现有建议器；§16.4 需两季度影子才迁移）。

    从 strategic_policy + 假设 + 投资档案 跑 construct_strategic_portfolio，附与当前战略权重的差异。
    """
    strat, port, prof = load_yaml(STRATEGY), load_yaml(PORTFOLIO), load_investor_profile()
    sp = strat.get("strategic_policy") or {}
    if not sp.get("roles"):
        return jsonify({"ok": False, "error": "strategy.yaml 缺 strategic_policy.roles（§18 政策书）"}), 400
    asm = load_assumptions(strat)
    asset_of = {str(u["code"]): u.get("asset") for u in strat.get("universe", [])}
    name_of = {str(u["code"]): u.get("name") for u in strat.get("universe", [])}
    planned = float(prof.get("planned_etf_capital") or 0)
    stable = float(prof.get("stable_assets_outside") or 0)
    etf_share = planned / (planned + stable) if planned > 0 and (planned + stable) > 0 else 1.0
    target, _ = resolve_policy_number(prof, "target_annual_return", 0.05, lo=0, hi=0.30)
    max_dd, _ = resolve_policy_number(prof, "max_acceptable_drawdown", 0.15, lo=0, hi=0.80)
    scenarios = load_stress_scenarios(strat)
    snap = strategic.construct_strategic_portfolio(
        sp, returns=asm["returns"], shocks=asm["shocks"], target_return=target,
        default_return=asm["default_return"], default_shock=asm["default_shock"],
        asset_of=asset_of, etf_share=etf_share, max_whole_stress=max_dd,
        returns_conservative=asm["returns_conservative"], scenarios=scenarios)
    snap["scenarios_count"] = len(scenarios)
    cur = {str(h["code"]): float(h.get("target_weight") or 0) for h in port.get("holdings", [])}
    built = snap.get("instrument_allocation") or {}
    snap["comparison"] = [
        {"code": c, "name": name_of.get(c) or c, "current": round(cur.get(c, 0.0), 4),
         "constructed": round(built.get(c, 0.0), 4), "delta": round(built.get(c, 0.0) - cur.get(c, 0.0), 4)}
        for c in sorted(set(cur) | set(built))]
    snap["mode"] = "shadow"
    snap["policy_version"] = sp.get("policy_version")
    return jsonify({"ok": True, "construct": snap})


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
