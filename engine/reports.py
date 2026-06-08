#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared report archive helpers for the weekly briefing workflow."""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPORTS_DIR = os.path.join(ROOT, "reports")
EXECUTIONS_DIR = os.path.join(ROOT, "journal", "executions")
DECISIONS_DIR = os.path.join(ROOT, "journal", "decisions")
NAV_DIR = os.path.join(ROOT, "journal", "nav")
CASHFLOWS_DIR = os.path.join(ROOT, "journal", "cashflows")
CONFIG_PATHS = {
    "portfolio_version": os.path.join(ROOT, "portfolio.yaml"),
    "strategy_version": os.path.join(ROOT, "strategy.yaml"),
    "investor_profile_version": os.path.join(ROOT, "investor_profile.yaml"),
}


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


def _report_day_id():
    """周报按『自然日』归档：同一天反复刷新覆盖同一份，不再堆秒级目录。

    执行记录仍用 _now_id()（精确到秒）——一天可有多笔成交，绝不能互相覆盖。
    决策记录 journal/decisions/<cycle_id>.json 以 cycle_id 为键，自然日 id 下
    同日重刷会复用同一份决策（保留已做的 skip/execute 标记），不被清空。
    """
    return datetime.now().strftime("%Y-%m-%d")


def safe_name(s):
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", str(s)).strip("_") or "item"


def config_versions():
    versions = {}
    for key, path in CONFIG_PATHS.items():
        if not os.path.exists(path):
            versions[key] = None
            continue
        with open(path, "rb") as f:
            versions[key] = hashlib.sha256(f.read()).hexdigest()[:16]
    return versions


def cycle_version_status(report, current=None):
    expected = (report or {}).get("config_versions") or {}
    current = current or config_versions()
    if not expected:
        return {"status": "legacy", "changed": [], "expected": expected, "current": current}
    changed = [key for key in CONFIG_PATHS if expected.get(key) != current.get(key)]
    return {"status": "stale" if changed else "current", "changed": changed,
            "expected": expected, "current": current}


def refresh_cycle_config_versions(report=None):
    report = report or load_active_cycle()
    if not report:
        return None
    report["config_versions"] = config_versions()
    _write_report(report)
    return report


def _decision_path(cycle_id):
    return os.path.join(DECISIONS_DIR, f"{safe_name(cycle_id)}.json")


def load_cycle_decisions(cycle_id):
    return load_json(_decision_path(cycle_id), {"cycle_id": cycle_id, "actions": {}})


def _action_key(source, code, side):
    return f"{source or 'rebalance'}:{str(code).strip()}:{str(side).strip().lower() or 'buy'}"


def save_cycle_decision(cycle_id, source, code, side, status, reason=""):
    if status not in ("skipped", "rejected", "pending"):
        raise ValueError("决策状态须为 skipped/rejected/pending")
    if not code:
        raise ValueError("决策动作缺少 ETF 代码")
    data = load_cycle_decisions(cycle_id)
    key = _action_key(source, code, side)
    if status == "pending":
        data["actions"].pop(key, None)
    else:
        data["actions"][key] = {
            "source": source or "rebalance",
            "code": str(code),
            "side": str(side or "buy").lower(),
            "status": status,
            "reason": str(reason or "").strip(),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(DECISIONS_DIR, exist_ok=True)
    with open(_decision_path(cycle_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


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


def _report_path(report_id):
    return os.path.join(REPORTS_DIR, safe_name(report_id), "report.json")


def _write_report(report):
    report_id = report.get("id")
    if not report_id:
        raise ValueError("report 缺少 id")
    report_dir = os.path.join(REPORTS_DIR, safe_name(report_id))
    os.makedirs(report_dir, exist_ok=True)
    with open(os.path.join(report_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)


def _latest_report():
    reports = _all_reports()
    return reports[-1] if reports else None


def load_active_cycle():
    """返回当前活动决策周期。

    新格式显式标记 cycle_status=active；兼容旧数据时，将最新一份周报视为活动周期。
    """
    reports = _all_reports()
    for report in reversed(reports):
        if report.get("cycle_status") == "active":
            return report
    return reports[-1] if reports else None


def prior_tactical_states():
    """§5.1：从上一份活动决策周期读取每只 ETF 的战术 state_after，供下一周期状态机推进。

    读取 `signals.tactical.diagnostics[code].state_after`；无活动周期或无战术诊断时返回 {}。
    历史报告只读——不另建可被覆盖的“当前状态”文件，避免状态源分裂。
    """
    report = load_active_cycle()
    if not report:
        return {}
    diag = (((report.get("signals") or {}).get("tactical") or {}).get("diagnostics")) or {}
    return {code: d.get("state_after") for code, d in diag.items() if isinstance(d, dict) and d.get("state_after")}


def _supersede_active_cycle(new_cycle_id, at):
    active = load_active_cycle()
    if not active or active.get("id") == new_cycle_id:
        return
    active["cycle_status"] = "superseded"
    active["superseded_at"] = at
    active["superseded_by"] = new_cycle_id
    _write_report(active)


def _execution_side(item):
    side = str((item or {}).get("side") or "").strip().lower()
    if side in ("buy", "sell"):
        return side
    reason = str((item or {}).get("reason") or "")
    return "sell" if ("卖" in reason or "减" in reason) else "buy"


def _executed_action_keys(cycle_id, executions=None):
    keys = set()
    for record in executions if executions is not None else load_executions():
        if str(record.get("report_id") or "") != str(cycle_id or ""):
            continue
        for item in record.get("items") or []:
            status = str(item.get("status") or "")
            if "执行" not in status or "未执行" in status:
                continue
            code = str(item.get("code") or "").strip()
            if code:
                keys.add((code, _execution_side(item)))
    return keys


def _tactical_cycle_suggestions(report, tac, executions, include_completed):
    """Phase C：advisory 模式下，把战术净动作派生为可执行调仓建议（取代结构性 5/25）。仅 mode==advisory 调用。"""
    cycle_id = report.get("id")
    executed = _executed_action_keys(cycle_id, executions)
    decisions = (load_cycle_decisions(cycle_id).get("actions") or {})
    name_of = {c: (v.get("name") or c) for c, v in ((report.get("signals") or {}).get("signals") or {}).items()}
    out = []
    for a in tac.get("actions") or []:
        if not a.get("actionable"):
            continue
        side = "sell" if a.get("side") == "trim" else "buy"
        key = _action_key("tactical", a.get("code"), side)
        status = "executed" if (str(a.get("code")), side) in executed else (
            (decisions.get(key) or {}).get("status") or "pending")
        if status != "pending" and not include_completed:
            continue
        out.append({"cycle_id": cycle_id, "action_status": status, "source": "tactical",
                    "code": a.get("code"), "name": name_of.get(a.get("code"), a.get("code")),
                    "side": side, "suggested_amount": a.get("approx_amount", 0), "suggested_shares": None,
                    "decision_reason": (decisions.get(key) or {}).get("reason", "")})
    return out


def cycle_suggestions(report=None, executions=None, include_completed=False):
    """从活动决策周期派生调仓建议，并用该周期关联的成交记录标记完成状态。"""
    report = report or load_active_cycle()
    if not report:
        return []
    cycle_id = report.get("id")
    signals = report.get("signals") or {}
    # Phase C 闸：仅当战术层处于 advisory 且有动作时，用战术动作取代结构性建议（§8.3）；
    # 默认 shadow → 走下方原结构性路径，行为零变化（影子绝不泄漏进可执行）。
    tac = signals.get("tactical") or {}
    if tac.get("mode") == "advisory" and (tac.get("actions")):
        return _tactical_cycle_suggestions(report, tac, executions, include_completed)
    executed = _executed_action_keys(cycle_id, executions)
    decisions = (load_cycle_decisions(cycle_id).get("actions") or {})
    suggestions = []
    for action in signals.get("actionable_rebalance") or []:
        if not action.get("actionable"):
            continue
        side = "sell" if action.get("suggest") == "trim" else "buy"
        key = _action_key("rebalance", action.get("code"), side)
        status = "executed" if (str(action.get("code")), side) in executed else (
            (decisions.get(key) or {}).get("status") or "pending")
        if status != "pending" and not include_completed:
            continue
        suggestions.append({
            "cycle_id": cycle_id,
            "action_status": status,
            "source": "rebalance",
            "code": action.get("code"),
            "name": action.get("name"),
            "side": side,
            "suggested_amount": action.get("approx_amount", 0),
            "suggested_shares": None,
            "decision_reason": (decisions.get(key) or {}).get("reason", ""),
        })
    for order in (signals.get("first_funding_plan") or {}).get("orders", []):
        if not order.get("actionable"):
            continue
        key = _action_key("first_funding", order.get("code"), "buy")
        status = "executed" if (str(order.get("code")), "buy") in executed else (
            (decisions.get(key) or {}).get("status") or "pending")
        if status != "pending" and not include_completed:
            continue
        suggestions.append({
            "cycle_id": cycle_id,
            "action_status": status,
            "source": "first_funding",
            "code": order.get("code"),
            "name": order.get("name"),
            "side": "buy",
            "suggested_amount": order.get("estimated_amount", 0),
            "suggested_shares": order.get("estimated_shares", 0),
            "decision_reason": (decisions.get(key) or {}).get("reason", ""),
        })
    return suggestions


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
            reason = a.get("action_reason")
            line = f"- {verb} {a.get('name')} `{a.get('code')}` 约 ¥{a.get('approx_amount', 0):,.0f}"
            lines.append(line + (f" —— {reason}" if reason else ""))
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
    report_id = _report_day_id()          # 自然日：同日刷新覆盖同一份周报，跨日才新建+supersede 上一份
    created_at = datetime.now().isoformat(timespec="seconds")
    _supersede_active_cycle(report_id, created_at)
    report_dir = os.path.join(REPORTS_DIR, report_id)
    os.makedirs(report_dir, exist_ok=True)
    report = {
        "id": report_id,
        "created_at": created_at,
        "cycle_status": "active",
        "superseded_at": None,
        "superseded_by": None,
        "config_versions": config_versions(),
        "summary": report_summary(signals),
        "signals": signals,
        "flags": flags,
    }
    # 紧凑写盘（不缩进）省体积；report.md 不再落盘——前端历史周报由 report.json 重渲染，
    # render_report_md() 仍保留，供按需导出 Markdown 使用。
    with open(os.path.join(report_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)
    try:
        save_nav_snapshot(signals)   # WS3：每份正式周报落一份 NAV 快照（同日覆盖）；失败不阻断归档
    except Exception:  # noqa: BLE001
        pass
    return report


def list_reports():
    if not os.path.exists(REPORTS_DIR):
        return []
    rows = []
    for name in sorted(os.listdir(REPORTS_DIR), reverse=True):
        p = os.path.join(REPORTS_DIR, name, "report.json")
        report = load_json(p)
        if report:
            rows.append({
                "id": report.get("id", name),
                "cycle_status": report.get("cycle_status", "legacy"),
                "superseded_by": report.get("superseded_by"),
                **(report.get("summary") or {}),
            })
    return rows


def load_report(report_id):
    return load_json(_report_path(report_id))


def current_suggestions():
    return cycle_suggestions()


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


def save_cash_flow(action, amount, cash_before, cash_after, note=""):
    """记录现金收支（添加/提取 ETF 桶现金）→ journal/cashflows/<id>.json。
    这只调整可投现金余额，不是 ETF 成交、不进 TWR/MWR（业绩只算已投入 ETF）。"""
    record_id = _now_id()
    record = {"id": record_id, "created_at": datetime.now().isoformat(timespec="seconds"),
              "action": action, "amount": round(float(amount), 2),
              "cash_before": round(float(cash_before), 2), "cash_after": round(float(cash_after), 2),
              "note": str(note or "")}
    os.makedirs(CASHFLOWS_DIR, exist_ok=True)
    with open(os.path.join(CASHFLOWS_DIR, f"{record_id}.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def load_cash_flows():
    if not os.path.exists(CASHFLOWS_DIR):
        return []
    rows = []
    for fn in sorted(os.listdir(CASHFLOWS_DIR), reverse=True):
        if fn.endswith(".json"):
            item = load_json(os.path.join(CASHFLOWS_DIR, fn))
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


def delete_execution_record(record_id):
    path = os.path.join(EXECUTIONS_DIR, f"{safe_name(record_id)}.json")
    if os.path.exists(path):
        os.remove(path)


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


def _formal_reports_for_review(reports):
    """月度复盘每个自然日只采用最后一份正式决策周期，避免重复刷新放大计划金额。"""
    by_day = {}
    for report in reports:
        summary = report.get("summary") or {}
        day = summary.get("generated_for") or (report.get("created_at") or report.get("id") or "")[:10]
        current = by_day.get(day)
        if current is None or str(report.get("created_at") or report.get("id") or "") >= str(
                current.get("created_at") or current.get("id") or ""):
            by_day[day] = report
    return [by_day[k] for k in sorted(by_day)]


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

    for r in _formal_reports_for_review(_all_reports()):
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
        for decision in (load_cycle_decisions(r.get("id")).get("actions") or {}).values():
            if decision.get("status") not in ("skipped", "rejected"):
                continue
            b["skipped_items"] += 1
            reason = (decision.get("reason") or "").strip()
            if not reason:
                reason = "已否决建议" if decision.get("status") == "rejected" else "本周期跳过"
            b["skip_reason_counts"][reason] = b["skip_reason_counts"].get(reason, 0) + 1

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


# ───────────── WS3：真实业绩跟踪 TWR / MWR（剔除注入本金，不把追加本金当收益）─────────────
# 口径：业绩书 = 已投入的 ETF（NAV=etf_value，不含未投现金）；执行记录派生现金流（买=+投入、卖=−撤出）。
# TWR 按时间加权剔除"何时/投多少"；MWR 按资金加权（XIRR）。费用单列为披露，不混入收益。

def _pdate(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def save_nav_snapshot(signals, portfolio=None):
    """每份正式周报落一份 NAV 快照（同 generated_for 当日覆盖→一天一条）。从 signals 取值；纯 IO、失败返 None。"""
    s = signals or {}
    as_of = s.get("generated_for")
    pv = s.get("portfolio_value")
    if not as_of or pv is None:
        return None
    cash = float(s.get("cash") or 0)
    holdings = []
    if portfolio:
        for h in portfolio.get("holdings") or []:
            code = str(h.get("code"))
            sig = (s.get("signals") or {}).get(code) or {}
            price, shares = sig.get("last"), float(h.get("shares", 0) or 0)
            holdings.append({"code": code, "name": h.get("name", code), "shares": shares, "price": price,
                             "value": round(shares * price, 2) if price else None,
                             "price_source": sig.get("source"), "price_as_of": sig.get("as_of")})
    snap = {"as_of": as_of, "created_at": datetime.now().isoformat(timespec="seconds"),
            "etf_value": round(pv - cash, 2), "cash": round(cash, 2), "portfolio_value": round(pv, 2),
            "data_quality": s.get("data_quality"), "stale_days_max": s.get("stale_days_max"), "holdings": holdings}
    try:
        os.makedirs(NAV_DIR, exist_ok=True)
        with open(os.path.join(NAV_DIR, f"{safe_name(as_of)}.json"), "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        return None
    return snap


def load_nav_series():
    if not os.path.exists(NAV_DIR):
        return []
    rows = [load_json(os.path.join(NAV_DIR, fn)) for fn in sorted(os.listdir(NAV_DIR)) if fn.endswith(".json")]
    return sorted([r for r in rows if r and r.get("as_of")], key=lambda r: r["as_of"])


def cash_flows_from_executions(executions=None):
    """从执行记录派生 ETF 业绩书的【外部现金流】：买=+amount(投入)、卖=−amount(撤出)。费用单列。"""
    flows, total_fee = [], 0.0
    for rec in (executions if executions is not None else load_executions()):
        when = (rec.get("created_at") or rec.get("id") or "")[:10]
        for it in rec.get("items") or []:
            status = str(it.get("status") or "")
            if "未执行" in status or "执行" not in status:
                continue
            amount, fee = float(it.get("amount") or 0), float(it.get("fee") or 0)
            total_fee += fee
            if amount and when:
                flows.append({"date": when, "amount": amount if _execution_side(it) == "buy" else -amount})
    return flows, round(total_fee, 2)


def _xirr(cashflows):
    """二分法解 XIRR：cashflows=[(date, amount)]；无符号变化→None。rate∈[-0.9999, 10]。"""
    if len(cashflows) < 2:
        return None
    cfs = sorted(cashflows, key=lambda x: x[0])
    t0 = cfs[0][0]

    def npv(r):
        return sum(a / (1.0 + r) ** ((d - t0).days / 365.0) for d, a in cfs)

    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo == 0:
        return lo
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        fm = npv(mid)
        if abs(fm) < 1e-7:
            return mid
        if flo * fm <= 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2.0


def compute_twr(nav_series, flows):
    """时间加权收益（剔除注入本金；期末流入约定：子区间 r=(V_end−期内净流入)/V_start−1，链乘）。纯函数。"""
    pts = sorted([p for p in nav_series if p.get("etf_value") is not None], key=lambda p: p["as_of"])
    if len(pts) < 2:
        return {"available": False, "reason": "NAV 快照不足 2 个，无法计算 TWR"}
    flows = sorted(flows, key=lambda f: f["date"])
    growth, periods, skipped = 1.0, 0, 0
    for k in range(1, len(pts)):
        v0, v1 = pts[k - 1]["etf_value"], pts[k]["etf_value"]
        if v0 <= 0:
            skipped += 1
            continue
        pf = sum(f["amount"] for f in flows if pts[k - 1]["as_of"] < f["date"] <= pts[k]["as_of"])
        growth *= (v1 - pf) / v0
        periods += 1
    if periods == 0:
        return {"available": False, "reason": "无有效子区间（起始 NAV 均 ≤0）"}
    twr = growth - 1.0
    days = (_pdate(pts[-1]["as_of"]) - _pdate(pts[0]["as_of"])).days
    # base=1+twr 可能 ≤0（某子区间"期内流入>期末市值"→该段亏损>100%，多见于快照早于买入市值反映/陈旧错价）；
    # 对负底数取非整数次幂会得到复数→round 崩溃。此处守卫：base≤0 时年化记 −100%，不取复数幂。
    base = 1.0 + twr
    if days <= 0:
        ann = None
    elif base > 0:
        ann = base ** (365.0 / days) - 1.0
    else:
        ann = -1.0
    return {"available": True, "twr": round(twr, 4), "annualized": (round(ann, 4) if ann is not None else None),
            "periods": periods, "skipped": skipped, "start": pts[0]["as_of"], "end": pts[-1]["as_of"]}


def compute_mwr(nav_series, flows):
    """资金加权收益率（XIRR）：起始 NAV 为投入、期内买入=追加投入、卖出=撤出、期末 NAV 为最终价值。纯函数。"""
    pts = sorted([p for p in nav_series if p.get("etf_value") is not None], key=lambda p: p["as_of"])
    if len(pts) < 2:
        return {"available": False, "reason": "NAV 快照不足 2 个，无法计算 MWR"}
    v0, vN = pts[0]["etf_value"], pts[-1]["etf_value"]
    if v0 <= 0:
        return {"available": False, "reason": "起始 NAV ≤0，无法计算 MWR"}
    cfs = [(_pdate(pts[0]["as_of"]), -v0)]
    for f in flows:
        if pts[0]["as_of"] < f["date"] <= pts[-1]["as_of"]:
            cfs.append((_pdate(f["date"]), -f["amount"]))   # 业务流入 +amount = 投资者贡献 −amount
    cfs.append((_pdate(pts[-1]["as_of"]), vN))
    r = _xirr(cfs)
    if r is None:
        return {"available": False, "reason": "现金流无符号变化，IRR 无解"}
    return {"available": True, "mwr": round(r, 4), "start": pts[0]["as_of"], "end": pts[-1]["as_of"]}


def performance_summary(benchmark_points=None):
    """汇总 TWR/MWR + 基准(单只沪深300)TWR + 累计费用 + 诚实注脚。benchmark_points=[{date, close}]。"""
    navs = load_nav_series()
    flows, total_fee = cash_flows_from_executions()
    twr, mwr = compute_twr(navs, flows), compute_mwr(navs, flows)
    bench = None
    if benchmark_points and len(benchmark_points) >= 2:
        bp = sorted(benchmark_points, key=lambda x: x["date"])
        p0, pN = bp[0]["close"], bp[-1]["close"]
        if p0 and p0 > 0:
            btwr = pN / p0 - 1.0
            days = (_pdate(bp[-1]["date"]) - _pdate(bp[0]["date"])).days
            bann = (1.0 + btwr) ** (365.0 / days) - 1.0 if days > 0 else None
            bench = {"twr": round(btwr, 4), "annualized": (round(bann, 4) if bann is not None else None),
                     "start": bp[0]["date"], "end": bp[-1]["date"], "name": "沪深300(510300)"}
    return {"twr": twr, "mwr": mwr, "benchmark": bench, "total_fees": total_fee, "snapshots": len(navs),
            "nav_curve": [{"date": p["as_of"], "etf_value": p["etf_value"], "portfolio_value": p.get("portfolio_value")}
                          for p in navs],
            "caveats": ["已剔除注入本金：TWR 按时间加权、MWR 按资金加权(XIRR)，不把追加本金当收益。",
                        "非承诺、仅历史回看；基准为单只沪深300、非完全可比；费用单列、未计税。"]}


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
