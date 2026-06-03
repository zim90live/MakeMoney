#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared report archive helpers for the weekly briefing workflow."""
import argparse
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPORTS_DIR = os.path.join(ROOT, "reports")
EXECUTIONS_DIR = os.path.join(ROOT, "journal", "executions")


def configure_console_encoding():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


configure_console_encoding()


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _now_id():
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def safe_name(s):
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", str(s)).strip("_") or "item"


def report_summary(signals):
    actions = signals.get("actionable_rebalance") or []
    actionable = [a for a in actions if a.get("actionable")]
    first = signals.get("first_funding_plan") or {}
    first_actions = [o for o in first.get("orders", []) if o.get("actionable")]
    return {
        "generated_for": signals.get("generated_for"),
        "data_quality": signals.get("data_quality"),
        "as_of_summary": signals.get("as_of_summary"),
        "portfolio_value": signals.get("portfolio_value"),
        "cash": signals.get("cash"),
        "actionable_count": len(actionable),
        "first_funding_count": len(first_actions),
    }


def render_report_md(report):
    s = report["signals"]
    flags = report.get("flags", {}).get("flags", [])
    lines = [
        f"# 投资周报 · {s.get('generated_for')}",
        "",
        f"- 数据质量：{s.get('data_quality')}，行情截至：{s.get('as_of_summary')}",
        f"- 组合总值：¥{s.get('portfolio_value', 0):,.0f}，现金：¥{s.get('cash', 0):,.0f}",
        f"- 再平衡允许：{'是' if s.get('rebalance_allowed') else '否'}",
        "",
        "## 持仓池信号",
    ]
    for code, item in (s.get("signals") or {}).items():
        if item.get("error"):
            lines.append(f"- {item.get('name', code)} `{code}`：{item['error']}")
            continue
        mom_key = next((k for k in item if k.startswith("momentum_")), None)
        mom = item.get(mom_key)
        parts = ["在均线上" if item.get("trend") == "above" else "跌破均线"]
        if mom is not None:
            parts.append(f"动量 {mom * 100:+.1f}%")
        if item.get("valuation"):
            v = item["valuation"]
            parts.append(f"估值分位 {v.get('percentile', 0) * 100:.0f}%({v.get('tag')})")
        elif item.get("valuation_missing"):
            parts.append("估值缺失(非中性)")
        lines.append(f"- {item.get('name', code)} `{code}`：" + "，".join(parts))

    lines.extend(["", "## 建议动作"])
    acts = [a for a in (s.get("actionable_rebalance") or []) if a.get("actionable")]
    first_orders = [o for o in (s.get("first_funding_plan") or {}).get("orders", []) if o.get("actionable")]
    if acts:
        for a in acts:
            verb = "减仓" if a.get("suggest") == "trim" else "加仓"
            lines.append(f"- {verb} {a.get('name')} `{a.get('code')}` 约 ¥{a.get('approx_amount', 0):,.0f}")
    elif first_orders:
        for o in first_orders:
            lines.append(f"- 首次试仓 {o.get('name')} `{o.get('code')}`：约 {o.get('estimated_shares', 0):,.0f} 份，¥{o.get('estimated_amount', 0):,.0f}")
    else:
        lines.append("- 本次无可执行动作。")

    lines.extend(["", "## 风险旗标"])
    if flags:
        for f in flags:
            lines.append(f"- [{f.get('category')}·{f.get('direction')}·{f.get('confidence')}] {f.get('title')}（{f.get('source')}，{f.get('date')}）")
    else:
        lines.append("- 本周未记录重大风险旗标。")

    lines.extend(["", "## 观察池"])
    for code, item in (s.get("watchlist_signals") or {}).items():
        if item.get("error"):
            lines.append(f"- {item.get('name', code)} `{code}`：{item['error']}")
            continue
        mom_key = next((k for k in item if k.startswith("momentum_")), None)
        mom = item.get(mom_key)
        mom_txt = f"，动量 {mom * 100:+.1f}%" if mom is not None else ""
        trend_txt = "在均线上" if item.get("trend") == "above" else "跌破均线"
        lines.append(f"- {item.get('name', code)} `{code}`：{trend_txt}{mom_txt}，{item.get('role')}")
    return "\n".join(lines) + "\n"


def archive_report(signals_path=None, flags_path=None):
    signals_path = signals_path or os.path.join(HERE, "signals.json")
    flags_path = flags_path or os.path.join(HERE, "flags.json")
    signals = load_json(signals_path)
    if not signals:
        raise FileNotFoundError(f"找不到信号文件：{signals_path}")
    flags = load_json(flags_path, {"flags": []})
    report_id = _now_id()
    report_dir = os.path.join(REPORTS_DIR, report_id)
    os.makedirs(report_dir, exist_ok=True)
    report = {
        "id": report_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "summary": report_summary(signals),
        "signals": signals,
        "flags": flags,
    }
    with open(os.path.join(report_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(report_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(render_report_md(report))
    return report


def list_reports():
    if not os.path.exists(REPORTS_DIR):
        return []
    rows = []
    for name in sorted(os.listdir(REPORTS_DIR), reverse=True):
        p = os.path.join(REPORTS_DIR, name, "report.json")
        report = load_json(p)
        if report:
            rows.append({"id": report.get("id", name), **(report.get("summary") or {})})
    return rows


def load_report(report_id):
    return load_json(os.path.join(REPORTS_DIR, safe_name(report_id), "report.json"))


def current_suggestions():
    s = load_json(os.path.join(HERE, "signals.json"), {})
    suggestions = []
    for a in s.get("actionable_rebalance") or []:
        if a.get("actionable"):
            suggestions.append({
                "source": "rebalance",
                "code": a.get("code"),
                "name": a.get("name"),
                "side": "sell" if a.get("suggest") == "trim" else "buy",
                "suggested_amount": a.get("approx_amount", 0),
                "suggested_shares": None,
            })
    for o in (s.get("first_funding_plan") or {}).get("orders", []):
        if o.get("actionable"):
            suggestions.append({
                "source": "first_funding",
                "code": o.get("code"),
                "name": o.get("name"),
                "side": "buy",
                "suggested_amount": o.get("estimated_amount", 0),
                "suggested_shares": o.get("estimated_shares", 0),
            })
    return suggestions


def load_executions():
    if not os.path.exists(EXECUTIONS_DIR):
        return []
    rows = []
    for fn in sorted(os.listdir(EXECUTIONS_DIR), reverse=True):
        if fn.endswith(".json"):
            item = load_json(os.path.join(EXECUTIONS_DIR, fn))
            if item:
                rows.append(item)
    return rows


def save_execution_record(body):
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise ValueError("请至少记录一条执行结果")
    record_id = _now_id()
    record = {
        "id": record_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "report_id": body.get("report_id"),
        "note": body.get("note", ""),
        "items": items,
    }
    os.makedirs(EXECUTIONS_DIR, exist_ok=True)
    with open(os.path.join(EXECUTIONS_DIR, f"{record_id}.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def executions_by_code():
    by_code = {}
    for record in load_executions():
        dt = (record.get("created_at") or record.get("id") or "")[:10]
        for item in record.get("items") or []:
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            by_code.setdefault(code, []).append({
                "date": dt,
                "status": item.get("status", "记录"),
                "shares": item.get("shares", 0),
                "amount": item.get("amount", 0),
                "note": record.get("note", ""),
                "report_id": record.get("report_id"),
            })
    return by_code


def main():
    ap = argparse.ArgumentParser(description="归档周报，供 Web 与 /周报 共用")
    ap.add_argument("--signals", default=os.path.join(HERE, "signals.json"))
    ap.add_argument("--flags", default=os.path.join(HERE, "flags.json"))
    args = ap.parse_args()
    report = archive_report(args.signals, args.flags)
    print(json.dumps({"ok": True, "id": report["id"], **report["summary"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
