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
    ):
        v = profile.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            errs.append(f"{label} 须为 ≥0 的数字")
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

        metrics = _spot_row_metrics(snap, code) or {}
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
        if premium is None and sensitive:
            warnings.append("折溢价数据不可用（货币/QDII 等尤其要留意溢价，下单前请在行情软件确认）")

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
        metrics = _spot_row_metrics(snap, code) or {}
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
    investor_profile = {
        "target_annual_return": _num(profile_body.get("target_annual_return", DEFAULT_INVESTOR_PROFILE["target_annual_return"])),
        "horizon_years": _num(profile_body.get("horizon_years", DEFAULT_INVESTOR_PROFILE["horizon_years"])),
        "max_acceptable_drawdown": _num(profile_body.get("max_acceptable_drawdown", DEFAULT_INVESTOR_PROFILE["max_acceptable_drawdown"])),
        "experience_level": profile_body.get("experience_level", DEFAULT_INVESTOR_PROFILE["experience_level"]),
        "emergency_cash_kept_outside": _num(profile_body.get("emergency_cash_kept_outside", DEFAULT_INVESTOR_PROFILE["emergency_cash_kept_outside"])),
        "monthly_contribution": _num(profile_body.get("monthly_contribution", DEFAULT_INVESTOR_PROFILE["monthly_contribution"])),
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
    holdings = port.get("holdings") or []
    universe = {str(u["code"]): u for u in strat.get("universe", [])}
    asset_of = {code: u.get("asset") for code, u in universe.items()}
    max_dd = float(profile.get("max_acceptable_drawdown") or 0.15)
    target_return = float(profile.get("target_annual_return") or 0.05)
    experience = str(profile.get("experience_level") or "beginner")
    horizon = float(profile.get("horizon_years") or 5)
    emergency = float(profile.get("emergency_cash_kept_outside") or 0)
    monthly = float(profile.get("monthly_contribution") or 0)

    if max_dd <= 0.10 or experience == "beginner":
        weights = {"bond": 0.55, "equity": 0.18, "equity_defensive": 0.14, "gold": 0.08, "other_equity": 0.05}
        template = "新手/低回撤模板"
    elif max_dd <= 0.15:
        weights = {"bond": 0.45, "equity": 0.22, "equity_defensive": 0.15, "gold": 0.10, "other_equity": 0.08}
        template = "平衡模板"
    else:
        weights = {"bond": 0.35, "equity": 0.28, "equity_defensive": 0.16, "gold": 0.10, "other_equity": 0.11}
        template = "进取模板"

    reasons = [f"基础模板：{template}；最大可接受回撤 {max_dd:.0%}。"]
    if target_return >= 0.06 and max_dd <= 0.15:
        reasons.append("目标年化较高但回撤预算有限，本次不硬提高权益，避免目标和风险错配。")
    if horizon < 3:
        weights["bond"] += 0.08
        weights["equity"] = max(0, weights["equity"] - 0.04)
        weights["other_equity"] = max(0, weights["other_equity"] - 0.04)
        reasons.append("投资年限较短，降低权益、提高债券。")
    if emergency <= 0:
        weights["bond"] += 0.05
        weights["equity"] = max(0, weights["equity"] - 0.02)
        weights["other_equity"] = max(0, weights["other_equity"] - 0.03)
        reasons.append("未记录场外应急现金，组合内保留更高防守资产。")
    if monthly > 0 and horizon >= 5 and max_dd >= 0.15:
        weights["bond"] = max(0, weights["bond"] - 0.03)
        weights["equity"] += 0.02
        weights["other_equity"] += 0.01
        reasons.append("存在月度追加资金且投资期较长，允许略提高权益。")

    def build_rows(asset_weights):
        rows = []
        equity_codes = [str(h.get("code")) for h in holdings if asset_of.get(str(h.get("code"))) == "equity"]
        for h in holdings:
            code = str(h.get("code"))
            asset = asset_of.get(code)
            w = 0.0
            if asset == "bond":
                w = asset_weights.get("bond", 0)
            elif asset == "equity_defensive":
                w = asset_weights.get("equity_defensive", 0)
            elif asset == "gold":
                w = asset_weights.get("gold", 0)
            elif asset == "equity":
                if equity_codes and code == equity_codes[0]:
                    w = asset_weights.get("equity", 0)
                else:
                    w = asset_weights.get("other_equity", 0) / max(1, len(equity_codes) - 1)
            rows.append({**h, "target_weight": w})
        total = sum(float(x.get("target_weight") or 0) for x in rows) or 1
        for x in rows:
            x["target_weight"] = float(x.get("target_weight") or 0) / total
        rounded = [round(x["target_weight"], 2) for x in rows]
        residual = round(1 - sum(rounded), 2)
        if rounded:
            idx = next((i for i, x in enumerate(rows) if asset_of.get(str(x.get("code"))) == "bond"), 0)
            rounded[idx] = round(rounded[idx] + residual, 2)
        for x, w in zip(rows, rounded):
            x["target_weight"] = max(0, w)
        return rows

    rows = build_rows(weights)
    stress, contribs = estimate_target_stress_drawdown(rows, universe)
    reductions = 0
    while stress > max_dd and reductions < 10:
        weights["bond"] += 0.03
        weights["equity"] = max(0, weights["equity"] - 0.015)
        weights["other_equity"] = max(0, weights["other_equity"] - 0.01)
        weights["equity_defensive"] = max(0, weights["equity_defensive"] - 0.005)
        rows = build_rows(weights)
        stress, contribs = estimate_target_stress_drawdown(rows, universe)
        reductions += 1
    if reductions:
        reasons.append(f"压力回撤超过预算，已自动降低权益 {reductions} 次，目标压力回撤约 {stress:.1%}。")

    current = {str(h.get("code")): float(h.get("target_weight") or 0) for h in holdings}
    items = []
    for h in rows:
        code = str(h.get("code"))
        suggested = float(h.get("target_weight") or 0)
        items.append({
            "code": code,
            "name": h.get("name", code),
            "asset": asset_of.get(code),
            "current_weight": round(current.get(code, 0), 4),
            "suggested_weight": round(suggested, 4),
            "delta": round(suggested - current.get(code, 0), 4),
        })
    return {
        "items": items,
        "stress_drawdown": round(stress, 4),
        "stress_contributions": contribs,
        "reasons": reasons,
        "warnings": ["建议权重不会自动生效；点击“应用建议权重”后才会写入本地组合配置。"],
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
    codes = request.args.get("codes")
    if codes:
        selected = [(c.strip(), holdings.get(c.strip(), c.strip())) for c in codes.split(",") if c.strip()]
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
    codes = request.args.get("codes")
    if codes:
        selected = [(c.strip(), holdings.get(c.strip()) or watch.get(c.strip()) or c.strip())
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
