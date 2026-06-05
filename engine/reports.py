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


def archive_report(signals_path=None, flags_path=None, signals=None):
    # signals 传入时直接用（已含 app 层的执行质量闸等加工），否则从 signals.json 读。
    flags_path = flags_path or os.path.join(HERE, "flags.json")
    if signals is None:
        signals_path = signals_path or os.path.join(HERE, "signals.json")
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
    # 紧凑写盘（不缩进）省体积；report.md 不再落盘——前端历史周报由 report.json 重渲染，
    # render_report_md() 仍保留，供按需导出 Markdown 使用。
    with open(os.path.join(report_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)
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
    clean_items = []
    for item in items:
        item = dict(item or {})
        status = str(item.get("status") or "").strip()
        if not status or "未执行" in status or "执行" not in status:
            continue
        if status and "未执行" not in status:
            code = str(item.get("code") or "").strip()
            shares = float(item.get("shares") or 0)
            price = float(item.get("price") or 0)
            amount = float(item.get("amount") or 0)
            if shares > 0 and price > 0:
                expected = round(shares * price, 2)
                if amount <= 0:
                    item["amount"] = expected
                elif abs(amount - expected) > 1:
                    raise ValueError(
                        f"{code or '该笔'} 成交金额与 份额×成交价 不一致："
                        f"当前金额 {amount:.2f}，按 {shares:g}×{price:g} 应约 {expected:.2f}。"
                        "请按券商成交金额更正后再保存。"
                    )
        clean_items.append(item)
    if not clean_items:
        raise ValueError("没有已执行或部分执行的成交可登记")
    record_id = _now_id()
    record = {
        "id": record_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "report_id": body.get("report_id"),
        "note": body.get("note", ""),
        "items": clean_items,
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
                "price": item.get("price", 0),
                "fee": item.get("fee", 0),
                "side": item.get("side", ""),
                "reason": item.get("reason", ""),
                "suggestion_source": item.get("suggestion_source"),
                "note": record.get("note", ""),
                "report_id": record.get("report_id"),
            })
    return by_code


def _month_of(s):
    """从日期/ID 字符串里取 'YYYY-MM'；取不到返回 '未知'。"""
    s = str(s or "")
    return s[:7] if len(s) >= 7 and s[4] == "-" else "未知"


def _all_reports():
    if not os.path.exists(REPORTS_DIR):
        return []
    rows = []
    for name in sorted(os.listdir(REPORTS_DIR)):
        r = load_json(os.path.join(REPORTS_DIR, name, "report.json"))
        if r:
            rows.append(r)
    return rows


def monthly_review():
    """按月复盘：核心看『规则是否被遵守』，不是看赚亏。

    汇总每个月的：周报数、累计建议动作数、实际执行/部分/未执行笔数、
    未执行原因、计划外操作（无建议来源的执行）、数据质量问题、期末组合估值（仅作上下文）。
    返回按月份倒序的列表，每条含一个守规则结论 verdict。
    """
    months = {}

    def bucket(m):
        return months.setdefault(m, {
            "month": m,
            "reports": 0,
            "suggested_actions": 0,
            "execution_records": 0,
            "executed_items": 0,
            "partial_items": 0,
            "skipped_items": 0,
            "off_plan_items": 0,          # 执行了但没有建议来源 = 计划外操作
            "traded_without_report": 0,    # 当月有执行但没有任何周报 = 未先看周报就交易
            "data_quality_issues": 0,
            "fees_total": 0.0,
            "suggested_amount": 0.0,       # 本月周报里"可执行"建议的合计金额（计划）
            "invested_amount": 0.0,        # 本月实际成交金额（执行）
            "skip_reason_counts": {},
            "portfolio_value_end": None,
            "_pv_date": None,
        })

    for r in _all_reports():
        summ = r.get("summary") or {}
        gen = summ.get("generated_for") or r.get("created_at") or r.get("id")
        b = bucket(_month_of(gen))
        b["reports"] += 1
        b["suggested_actions"] += int(summ.get("actionable_count") or 0) + int(summ.get("first_funding_count") or 0)
        sig = r.get("signals") or {}
        for a in sig.get("actionable_rebalance") or []:
            if a.get("actionable"):
                b["suggested_amount"] += float(a.get("approx_amount") or 0)
        for o in (sig.get("first_funding_plan") or {}).get("orders", []):
            if o.get("actionable"):
                b["suggested_amount"] += float(o.get("estimated_amount") or 0)
        if summ.get("data_quality") not in ("完整", "缓存可用", None):
            b["data_quality_issues"] += 1
        pv = summ.get("portfolio_value")
        if pv is not None and (b["_pv_date"] is None or str(gen) >= b["_pv_date"]):
            b["portfolio_value_end"] = pv
            b["_pv_date"] = str(gen)

    for rec in load_executions():
        dt = rec.get("created_at") or rec.get("id") or ""
        b = bucket(_month_of(dt))
        b["execution_records"] += 1
        for it in rec.get("items") or []:
            status = str(it.get("status") or "").strip()
            b["fees_total"] += float(it.get("fee") or 0)
            if "未执行" in status or (not status):
                b["skipped_items"] += 1
                reason = (it.get("reason") or "").strip() or "（未填原因）"
                b["skip_reason_counts"][reason] = b["skip_reason_counts"].get(reason, 0) + 1
            else:  # 已执行 / 部分执行
                if "部分" in status:
                    b["partial_items"] += 1
                else:
                    b["executed_items"] += 1
                b["invested_amount"] += float(it.get("amount") or 0)
                if not (it.get("suggestion_source") or "").strip():
                    b["off_plan_items"] += 1

    result = []
    for m in sorted(months, reverse=True):
        b = months[m]
        if b["execution_records"] > 0 and b["reports"] == 0:
            b["traded_without_report"] = b["execution_records"]
        executed_total = b["executed_items"] + b["partial_items"]
        findings = []
        if b["off_plan_items"] > 0:
            findings.append(f"{b['off_plan_items']} 笔执行没有对应的建议来源（计划外操作）")
        if b["traded_without_report"] > 0:
            findings.append(f"{b['traded_without_report']} 次执行当月没有任何周报（未先看周报就交易）")
        if b["data_quality_issues"] > 0:
            findings.append(f"{b['data_quality_issues']} 次周报数据质量不足，不应据此交易")
        if b["execution_records"] == 0:
            verdict, verdict_level = "本月无执行记录", "none"
        elif not findings:
            verdict, verdict_level = "守规则：执行均有建议来源，数据质量达标", "good"
        else:
            verdict, verdict_level = "需注意：存在计划外或数据质量问题", "warn"
        out = {k: v for k, v in b.items() if not k.startswith("_")}
        out["executed_total"] = executed_total
        out["fees_total"] = round(b["fees_total"], 2)
        out["suggested_amount"] = round(b["suggested_amount"], 2)
        out["invested_amount"] = round(b["invested_amount"], 2)
        out["deviation_amount"] = round(b["invested_amount"] - b["suggested_amount"], 2)
        out["skip_reasons"] = [
            {"reason": k, "count": v}
            for k, v in sorted(b["skip_reason_counts"].items(), key=lambda kv: kv[1], reverse=True)
        ]
        out.pop("skip_reason_counts", None)
        out["findings"] = findings
        out["verdict"] = verdict
        out["verdict_level"] = verdict_level
        result.append(out)
    return result


def compute_holdings_draft(portfolio, records):
    """根据执行记录把当前持仓推算成"成交后草稿"。纯函数、可测；绝不写文件。

    side 取自 item['side']（buy/sell），缺失时按买入处理并在 warnings 标注。
    买入：shares += s，cash -= (amount+fee)；卖出：shares -= s，cash += (amount-fee)。
    """
    holdings, order = {}, []
    for h in (portfolio.get("holdings") or []):
        code = str(h.get("code"))
        sh = float(h.get("shares", 0) or 0)
        holdings[code] = {"name": h.get("name", code), "old": sh, "shares": sh,
                          "target_weight": h.get("target_weight", 0)}
        order.append(code)
    cash0 = float(portfolio.get("cash", 0) or 0)
    cash = cash0
    warnings, applied = [], 0
    for rec in records:
        for it in (rec.get("items") or []):
            status = str(it.get("status") or "")
            if "未执行" in status or not status.strip():
                continue
            code = str(it.get("code") or "").strip()
            if not code:
                continue
            shares = float(it.get("shares") or 0)
            amount = float(it.get("amount") or 0)
            fee = float(it.get("fee") or 0)
            side = str(it.get("side") or "").strip().lower()
            if side not in ("buy", "sell"):
                reason = str(it.get("reason") or "")
                side = "sell" if ("卖" in reason or "减" in reason) else "buy"
                warnings.append(f"{code} 一笔未标方向，按{'卖出' if side == 'sell' else '买入'}处理")
            if code not in holdings:
                holdings[code] = {"name": it.get("name", code), "old": 0.0, "shares": 0.0, "target_weight": 0}
                order.append(code)
                warnings.append(f"{code} 不在当前持仓表中，已新增一行")
            if side == "buy":
                holdings[code]["shares"] += shares
                cash -= (amount + fee)
            else:
                holdings[code]["shares"] -= shares
                cash += (amount - fee)
            applied += 1
    rows = [{"code": c, "name": holdings[c]["name"],
             "old_shares": round(holdings[c]["old"]),
             "new_shares": round(holdings[c]["shares"]),
             "delta_shares": round(holdings[c]["shares"] - holdings[c]["old"]),
             "target_weight": holdings[c]["target_weight"]}
            for c in order]
    return {
        "applied_items": applied,
        "cash_old": round(cash0, 2),
        "cash_new": round(cash, 2),
        "holdings": rows,
        "warnings": warnings,
        "changed": bool(any(r["delta_shares"] for r in rows) or round(cash, 2) != round(cash0, 2)),
    }


def main():
    ap = argparse.ArgumentParser(description="归档周报，供 Web 与 /周报 共用")
    ap.add_argument("--signals", default=os.path.join(HERE, "signals.json"))
    ap.add_argument("--flags", default=os.path.join(HERE, "flags.json"))
    args = ap.parse_args()
    report = archive_report(args.signals, args.flags)
    print(json.dumps({"ok": True, "id": report["id"], **report["summary"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
