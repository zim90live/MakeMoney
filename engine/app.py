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
from signals import estimate_target_stress_drawdown, validate_config, validate_strategy  # noqa: E402  复用同一套校验
from signals import fetch_hist  # noqa: E402
from reports import (  # noqa: E402
    archive_report, compute_holdings_draft, current_suggestions, executions_by_code,
    list_reports, load_executions, load_report, monthly_review, save_execution_record,
)
from learning import save_ack, watchlist_learning  # noqa: E402

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
    with open(PORTFOLIO, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


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


# ── westock（腾讯自选股）兜底：akshare 快照缺折溢价/规模时，用其 `etf` 详情补 ──
# 仅作兜底（akshare 取不到时才调），结果进程内缓存；取数失败一律返回 None，绝不编造。
_WESTOCK_PKG = "westock-data-skillhub@1.0.3"
_WESTOCK_CACHE = {}  # code -> (ts, dict|None)
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
    """解析 westock `etf` 输出里的首个明细表，返回 表头->值 dict；失败返回 None。"""
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


def _westock_etf_metrics(code, max_age=300):
    """用 westock `etf` 详情取 {premium, market_cap, turnover, purchase_status, ...}；缓存 max_age 秒。"""
    now = time.time()
    cached = _WESTOCK_CACHE.get(code)
    if cached and now - cached[0] < max_age:
        return cached[1]
    row = _parse_westock_etf(_run_westock(["etf", _westock_symbol(code)]))
    out = None
    if row:
        def num(k):
            try:
                return float(row.get(k))
            except (TypeError, ValueError):
                return None
        close, nav = num("closePrice"), num("nav")
        out = {
            "premium": (close / nav - 1) if (close and nav and nav > 0) else None,
            "market_cap": num("totalMV"),
            "turnover": num("turnoverValue"),
            "purchase_status": (row.get("purchaseStatus") or "").strip() or None,
            "establish_date": (row.get("establishDate") or "")[:10] or None,
            "last_price": close,
            "iopv": nav,
        }
    _WESTOCK_CACHE[code] = (now, out)
    return out


def _quality_metrics(code, snap, sensitive):
    """折溢价/规模/成交额：先 akshare 快照，缺则用 westock `etf` 兜底。返回 (metrics, extra)。"""
    m = dict(_spot_row_metrics(snap, code) or {})
    extra = {"premium_source": "akshare" if m.get("premium") is not None else None,
             "scale_source": "akshare" if m.get("market_cap") is not None else None,
             "purchase_status": None, "fallback": False}
    if m.get("premium") is None or m.get("market_cap") is None:
        ws = _westock_etf_metrics(code)
        if ws:
            extra["fallback"] = True
            extra["purchase_status"] = ws.get("purchase_status")
            if m.get("premium") is None and ws.get("premium") is not None:
                m["premium"] = ws["premium"]
                m.setdefault("iopv", ws.get("iopv"))
                if m.get("price") is None:
                    m["price"] = ws.get("last_price")
                extra["premium_source"] = "westock"
            if m.get("market_cap") is None and ws.get("market_cap") is not None:
                m["market_cap"] = ws["market_cap"]
                extra["scale_source"] = "westock"
            if m.get("turnover") is None and ws.get("turnover") is not None:
                m["turnover"] = ws["turnover"]
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


def _etf_quality_for(code, name=None, snap=None, sensitive=False):
    """ETF 产品质量检查。数据源缺字段时只提示不足，不把未知当通过。"""
    try:
        import akshare as ak  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        d = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="")
        if d is None or d.empty:
            return {"code": code, "name": name or code, "status": "数据不足", "issues": ["无法获取 ETF 历史数据"]}
        date_col = "日期" if "日期" in d.columns else "date"
        amount_col = next((c for c in ("成交额", "amount") if c in d.columns), None)
        dates = pd.to_datetime(d[date_col], errors="coerce").dropna()
        if dates.empty:
            return {"code": code, "name": name or code, "status": "数据不足", "issues": ["无法识别历史日期"]}
        listed_days = int((dates.max() - dates.min()).days)
        history_years = listed_days / 365.25
        avg_turnover_20d = None
        issues, warnings = [], []
        if amount_col:
            amt = pd.to_numeric(d[amount_col], errors="coerce").dropna().tail(20)
            if not amt.empty:
                avg_turnover_20d = float(amt.mean())
                if avg_turnover_20d < 10_000_000:
                    issues.append("近20日平均成交额低于 1000 万元，流动性偏弱")
                elif avg_turnover_20d < 50_000_000:
                    warnings.append("近20日平均成交额低于 5000 万元，下单前需关注盘口")
            else:
                warnings.append("成交额字段为空，无法判断流动性")
        else:
            warnings.append("数据源未返回成交额，无法判断流动性")
        if history_years < 1:
            issues.append("上市不足 1 年，历史样本太短")
        elif history_years < 3:
            warnings.append("上市不足 3 年，历史样本偏短")

        metrics, qextra = _quality_metrics(code, snap, sensitive)
        premium = metrics.get("premium")
        market_cap = metrics.get("market_cap")
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
            warnings.append("规模/折溢价由 westock(腾讯自选股)兜底补全（akshare 快照缺失时）")

        status = "不足" if issues else ("关注" if warnings else "通过")
        return {
            "code": code,
            "name": name or code,
            "status": status,
            "history_years": round(history_years, 1),
            "avg_turnover_20d": round(avg_turnover_20d, 0) if avg_turnover_20d is not None else None,
            "turnover_1d": round(metrics.get("turnover"), 0) if metrics.get("turnover") is not None else None,
            "premium_pct": round(premium * 100, 2) if premium is not None else None,
            "iopv": metrics.get("iopv"),
            "last_price": metrics.get("price"),
            "market_cap": round(market_cap, 0) if market_cap is not None else None,
            "purchase_status": qextra.get("purchase_status"),
            "premium_source": qextra.get("premium_source"),
            "scale_source": qextra.get("scale_source"),
            "as_of": str(dates.max().date()),
            "issues": issues,
            "warnings": warnings,
        }
    except Exception as e:  # noqa: BLE001
        df, source = fetch_hist(code)
        if df is None or df.empty:
            return {"code": code, "name": name or code, "status": "数据不足", "issues": [f"质量检查失败：{e}"]}
        dates = df["date"]
        history_years = (dates.max() - dates.min()).days / 365.25
        issues, warnings = [], []
        metrics, qextra = _quality_metrics(code, snap, sensitive)
        premium = metrics.get("premium")
        market_cap = metrics.get("market_cap")
        turnover_1d = metrics.get("turnover")
        # 成交额历史源（东财日线）失败：用快照"近一日成交额"兜底，而不是直接报"未知"
        if turnover_1d is not None:
            warnings.append("成交额历史源暂不可用（多为东财接口波动），已用快照“近一日成交额”评估流动性")
            if turnover_1d < 10_000_000:
                issues.append("近一日成交额低于 1000 万元，流动性偏弱")
            elif turnover_1d < 50_000_000:
                warnings.append("近一日成交额低于 5000 万元，下单前需关注盘口")
        else:
            warnings.append("成交额暂不可用，本次仅据曲线/折溢价/规模判断；可点“刷新”重试")
        if history_years < 3:
            warnings.append("历史样本偏短")
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
        if qextra.get("fallback"):
            warnings.append("规模/折溢价由 westock(腾讯自选股)兜底补全（akshare 快照缺失时）")
        return {
            "code": code,
            "name": name or code,
            "status": "不足" if issues else "关注",
            "history_years": round(history_years, 1),
            "avg_turnover_20d": None,
            "turnover_1d": round(turnover_1d, 0) if turnover_1d is not None else None,
            "premium_pct": round(premium * 100, 2) if premium is not None else None,
            "iopv": metrics.get("iopv"),
            "last_price": metrics.get("price"),
            "market_cap": round(market_cap, 0) if market_cap is not None else None,
            "purchase_status": qextra.get("purchase_status"),
            "premium_source": qextra.get("premium_source"),
            "scale_source": qextra.get("scale_source"),
            "as_of": str(dates.max().date()),
            "issues": issues,
            "warnings": warnings,
        }


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

    max_dd = float(profile.get("max_acceptable_drawdown") or 0.15)      # 全组合口径
    target_return = float(profile.get("target_annual_return") or 0.05)  # 针对 ETF 风险桶
    experience = str(profile.get("experience_level") or "beginner")
    horizon = float(profile.get("horizon_years") or 5)
    stable = float(profile.get("stable_assets_outside") or 0)
    planned_etf = float(profile.get("planned_etf_capital") or 0)

    # 缓冲比例：ETF 桶在全组合中的占比。planned_etf 缺省时退化为"无缓冲"(=1)。
    etf_share = planned_etf / (planned_etf + stable) if planned_etf > 0 and (planned_etf + stable) > 0 else 1.0
    # 全组合回撤预算折算到 ETF 桶：whole_dd = etf_dd * etf_share → etf_dd_budget = max_dd / etf_share。
    etf_dd_budget = min(max_dd / etf_share if etf_share > 0 else max_dd, 0.40)  # 0.40 理智上限，再厚缓冲也不满仓权益

    # 各 sleeve 假设：(假设年化, 压力冲击)。冲击与 estimate_target_stress_drawdown 对齐。
    SLEEVE = {
        "bond": (0.030, -0.03), "equity": (0.070, -0.30), "equity_defensive": (0.055, -0.20),
        "gold": (0.020, -0.15), "global_equity": (0.080, -0.30), "global_growth": (0.100, -0.40),
        "china_growth": (0.090, -0.40),
    }
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
    rounded = [round(r["target_weight"], 2) for r in rows]
    residual = round(1 - sum(rounded), 2)
    if rounded:
        # 残差并到当前最大权重那一项：它足够大、能吸收 ±0.0x 残差而不会变负，保证合计恰为 1.0。
        # （早先并到债券，在债券=0、残差为负时会被随后的 max(0,..) 吞掉，导致合计 1.01。）
        idx = max(range(len(rounded)), key=lambda i: rounded[i])
        rounded[idx] = round(rounded[idx] + residual, 2)
    for r, w in zip(rows, rounded):
        r["target_weight"] = max(0.0, w)

    stress, contribs = estimate_target_stress_drawdown(rows, uni_dict)
    whole_stress = stress * etf_share

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
    } for r in rows]
    return {
        "items": items,
        "stress_drawdown": round(stress, 4),                          # ETF 桶口径（兼容旧字段）
        "etf_stress_drawdown": round(stress, 4),
        "whole_portfolio_stress_drawdown": round(whole_stress, 4),
        "etf_share": round(etf_share, 4),
        "etf_drawdown_budget": round(etf_dd_budget, 4),
        "expected_etf_return": round(expected_return, 4),
        "target_annual_return": round(target_return, 4),
        "suggested_equity_total": round(best_e, 4),
        "stress_contributions": contribs,
        "reasons": reasons,
        "warnings": ["建议权重不会自动生效；点击“应用建议权重”后才会写入本地组合配置。",
                     "新增全球/成长品种波动更大，QDII 有溢价/汇率/额度风险；建议分批小额起步。"],
    }


@app.get("/api/portfolio/target-suggestion")
def target_suggestion():
    port, strat = load_yaml(PORTFOLIO), load_yaml(STRATEGY)
    return jsonify({"ok": True, "suggestion": _suggest_target_weights(port, strat, load_investor_profile())})


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
    report = archive_report()
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


@app.get("/api/reports/<report_id>")
def report_detail(report_id):
    report = load_report(report_id)
    if not report:
        return jsonify({"ok": False, "error": "找不到周报"}), 404
    return jsonify({"ok": True, "report": report})


@app.get("/api/executions")
def executions():
    return jsonify({"ok": True, "suggestions": current_suggestions(), "executions": load_executions()})


@app.post("/api/executions")
def save_execution():
    body = request.get_json(force=True)
    try:
        record = save_execution_record(body)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "execution": record})


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
    snap = _etf_spot_snapshot()
    data = [_etf_quality_for(code, name, snap=snap,
                             sensitive=asset_of.get(str(code)) in _PREMIUM_SENSITIVE_ASSETS)
            for code, name in selected]
    return jsonify({"ok": True, "items": data, "premium_source": "live" if snap is not None else "unavailable"})


@app.get("/api/etf/spot")
def etf_spot():
    """只拉 ETF 实时快照价，供首页盘中估值使用；不跑日 K、不跑质量检查。"""
    port = load_yaml(PORTFOLIO)
    holdings = {str(h["code"]): h.get("name", str(h["code"])) for h in port.get("holdings", [])}
    codes = request.args.get("codes")
    if codes:
        selected = [(c.strip(), holdings.get(c.strip(), c.strip())) for c in codes.split(",") if c.strip()]
    else:
        selected = [(c, name) for c, name in holdings.items()]
    snap = _etf_spot_snapshot(max_age=0)
    items = []
    for code, name in selected:
        metrics = _spot_row_metrics(snap, code) or {}
        items.append({
            "code": code,
            "name": name,
            "last_price": metrics.get("price"),
            "iopv": metrics.get("iopv"),
            "premium_pct": round(metrics["premium"] * 100, 2) if metrics.get("premium") is not None else None,
            "turnover_1d": round(metrics["turnover"], 0) if metrics.get("turnover") is not None else None,
        })
    return jsonify({
        "ok": True,
        "items": items,
        "source": "live" if snap is not None else "unavailable",
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
