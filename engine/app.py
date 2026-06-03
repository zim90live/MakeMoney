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

from flask import Flask, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORTFOLIO = os.path.join(ROOT, "portfolio.yaml")
STRATEGY = os.path.join(ROOT, "strategy.yaml")
WEB = os.path.join(HERE, "web")

sys.path.insert(0, HERE)
import yaml  # noqa: E402
from signals import validate_config, validate_strategy  # noqa: E402  复用同一套校验
from signals import fetch_hist  # noqa: E402
from reports import (  # noqa: E402
    archive_report, current_suggestions, executions_by_code, list_reports,
    load_executions, load_report, save_execution_record,
)

app = Flask(__name__, static_folder=None)


def _run_engine_script(script, timeout):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, os.path.join(HERE, script)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env)


def load_yaml(p):
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


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


@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/api/config")
def get_config():
    port, strat = load_yaml(PORTFOLIO), load_yaml(STRATEGY)
    return jsonify({
        "cash": port.get("cash", 0),
        "holdings": port.get("holdings", []),
        "risk_profile": strat.get("risk_profile", "平衡"),
        "risk_controls": strat.get("risk_controls", {}),
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
    strat = load_yaml(STRATEGY)
    strat["risk_profile"] = risk
    errs = validate_strategy(strat) + validate_config(port, strat)
    if errs:
        return jsonify({"ok": False, "errors": errs}), 400
    _write_portfolio(port)
    _set_risk_profile(risk)
    return jsonify({"ok": True})


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5057"))   # 避开 macOS 默认占用的 5000(AirPlay)
    print(f"投资周报驾驶舱已启动 → http://127.0.0.1:{port}  （Ctrl+C 退出）")
    app.run(host="127.0.0.1", port=port, debug=False)
