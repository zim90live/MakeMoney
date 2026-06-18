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
STRATEGIC_APPLIES_DIR = os.path.join(ROOT, "journal", "strategic_applies")
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
        "signal_as_of": signals.get("signal_as_of") or signals.get("as_of_summary"),
        "execution_checked_at": signals.get("execution_checked_at"),
        "snapshot_id": signals.get("snapshot_id"),
        "data_quality": signals.get("data_quality"),
        "as_of_summary": signals.get("as_of_summary"),
        "portfolio_value": signals.get("portfolio_value"),
        "cash": signals.get("cash"),
        "actionable_count": len(actionable),
        "first_funding_count": len(first_actions),
        "first_funding_amount": (first.get("estimated_deploy_amount") if first.get("eligible") else None),
        "first_funding_blocked_amount": (first.get("blocked_deploy_amount") if first.get("eligible") else None),
        "first_funding_remaining_cash": (first.get("remaining_cash_after_execution") if first.get("eligible") else None),
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


def _executed_action_keys(cycle_id, executions=None, since=None):
    """该周期已执行的 (code, side) 集合。

    M4（2026-06-10 审查）：since=周期 created_at 时，只认**本版周期生成之后**登记的成交——
    同日重新生成（如战略应用改 target 后）会复用同一自然日 id，早盘按旧版周期登记的成交
    不得把新版周期里同 (code,side) 的**新**建议自动标成"已执行"而隐藏。
    记录缺 created_at 时回退按文件 id 前缀（YYYY-MM-DD_HHMMSS）判断；仍无法判断则计入（保持旧行为）。"""
    keys = set()
    for record in executions if executions is not None else load_executions():
        if str(record.get("report_id") or "") != str(cycle_id or ""):
            continue
        if since:
            rec_at = str(record.get("created_at") or "")
            if not rec_at:
                rid = str(record.get("id") or "")
                if re.match(r"^\d{4}-\d{2}-\d{2}_\d{6}$", rid):
                    rec_at = f"{rid[:10]}T{rid[11:13]}:{rid[13:15]}:{rid[15:17]}"
            if rec_at and rec_at < str(since):
                continue
        for item in record.get("items") or []:
            status = str(item.get("status") or "")
            if "执行" not in status or "未执行" in status:
                continue
            code = str(item.get("code") or "").strip()
            if code:
                keys.add((code, _execution_side(item)))
    return keys


def _cycle_decision_actions(cycle_id, since=None):
    """该周期的 skip/reject 决策（M4：同样只认本版周期生成之后做出的决策，旧版周期的否决不得隐藏新建议）。"""
    actions = load_cycle_decisions(cycle_id).get("actions") or {}
    if not since:
        return actions
    return {k: v for k, v in actions.items()
            if not (str(v.get("updated_at") or "") and str(v.get("updated_at")) < str(since))}


def _tactical_cycle_suggestions(report, tac, executions, include_completed):
    """Phase C：advisory 模式下，把战术净动作派生为可执行调仓建议（取代结构性 5/25）。仅 mode==advisory 调用。"""
    cycle_id = report.get("id")
    executed = _executed_action_keys(cycle_id, executions, since=report.get("created_at"))
    decisions = _cycle_decision_actions(cycle_id, since=report.get("created_at"))
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
    executed = _executed_action_keys(cycle_id, executions, since=report.get("created_at"))
    decisions = _cycle_decision_actions(cycle_id, since=report.get("created_at"))
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
            "suggested_shares": action.get("lot_shares"),   # 整手化后的建议份额
            "suggested_price": action.get("last"),          # 最新价（供前端自动算金额/手续费）
            "execution_reference": action.get("execution_reference"),
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
            "suggested_price": order.get("last"),
            "execution_reference": order.get("execution_reference"),
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


# C（反幻觉防线接进管道）：旗标在被消费前先机械校验 + 判新鲜度。
FLAGS_STALE_GATE_DAYS = 7   # 旗标比本周信号晚超过这么多天 → 只展示"过旧"、不参与拦买


def _parse_day(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _load_universe_codes():
    """从仓库根 strategy.yaml 取 universe 代码集合（供旗标 affected_assets 校验）。失败/缺 yaml → None（跳过该项）。"""
    try:
        import yaml
    except ImportError:
        return None
    sp = os.path.join(os.path.dirname(HERE), "strategy.yaml")
    if not os.path.exists(sp):
        return None
    try:
        with open(sp, encoding="utf-8") as fh:
            st = yaml.safe_load(fh) or {}
        return {str(u["code"]) for u in (st.get("universe") or [])}
    except Exception:  # noqa: BLE001
        return None


def load_validated_flags(flags_path=None, signal_generated_for=None, universe=None):
    """加载 + 机械校验 flags.json，并判定新鲜度（把反幻觉防线接进运行时管道）。

    - 校验不通过 → flags 置空（失败即放空：宁可放过，不可被未校验/手误旗标误导买卖）。
    - 旗标日期比本周信号**早** > FLAGS_STALE_GATE_DAYS 天（age_days=信号日−旗标日）→ stale=True
      （前端只展示"过旧"，不参与拦买）。
    返回 dict（保留 .flags 以兼容旧 report["flags"] 结构 + 校验/新鲜度元数据）。
    """
    flags_path = flags_path or os.path.join(HERE, "flags.json")
    raw = load_json(flags_path, {"flags": []}) or {"flags": []}
    generated_for = raw.get("generated_for")
    if universe is None:
        universe = _load_universe_codes()
    try:
        from validate_flags import validate_flags_data
        errors, _warns = validate_flags_data(raw, universe=universe)
    except Exception as exc:  # noqa: BLE001  校验器自身异常也视为不通过（失败即放空）
        errors = [f"校验器异常：{exc}"]
    flags = list(raw.get("flags") or [])
    if errors:
        status, flags = "rejected", []
    elif not flags:
        status = "empty"
    else:
        status = "ok"
    sd, fd = _parse_day(signal_generated_for), _parse_day(generated_for)
    age_days = (sd - fd).days if (sd and fd) else None
    stale = bool(age_days is not None and age_days > FLAGS_STALE_GATE_DAYS)
    return {
        "generated_for": generated_for,
        "flags": flags,
        "validation_status": status,         # ok | rejected | empty
        "validation_errors": errors,
        "age_days": age_days,
        "stale": stale,
    }


def archive_report(signals_path=None, flags_path=None, signals=None):
    # signals 传入时直接用（已含 app 层的执行质量闸等加工），否则从 signals.json 读。
    flags_path = flags_path or os.path.join(HERE, "flags.json")
    if signals is None:
        signals_path = signals_path or os.path.join(HERE, "signals.json")
        signals = load_json(signals_path)
        if not signals:
            raise FileNotFoundError(f"找不到信号文件：{signals_path}")
    # C：校验 + 新鲜度后再嵌入（不通过→置空；过旧→标 stale 供前端"过旧"提示）。
    flags = load_validated_flags(flags_path, signal_generated_for=(signals or {}).get("generated_for"))
    report_id = _report_day_id()          # 自然日：同日刷新覆盖同一份周报，跨日才新建+supersede 上一份
    created_at = datetime.now().isoformat(timespec="seconds")
    _supersede_active_cycle(report_id, created_at)
    report_dir = os.path.join(REPORTS_DIR, report_id)
    os.makedirs(report_dir, exist_ok=True)
    # M4（2026-06-10 审查）：同日覆盖前把旧版存入 history/——盘中若按旧版建议做过真实交易，
    # 支撑那笔交易的建议版本必须可追溯（审计留痕），不得被整份覆盖销毁。失败不阻断归档。
    prev = load_json(os.path.join(report_dir, "report.json"))
    if prev and prev.get("created_at") != created_at:
        try:
            hist_dir = os.path.join(report_dir, "history")
            os.makedirs(hist_dir, exist_ok=True)
            stamp = safe_name(str(prev.get("created_at") or "prev").replace(":", ""))
            with open(os.path.join(hist_dir, f"report-{stamp}.json"), "w", encoding="utf-8") as f:
                json.dump(prev, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
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


def save_strategic_apply(*, fingerprint, policy_version, quality_status,
                         old_weights, new_weights, source):
    """§0B 审计痕迹：每次把权威构建 apply 进 portfolio.yaml 时落一条 mode=applied 记录
    （fingerprint / policy_version / quality_status / old→new 权重 diff / 触发源 / 时间戳）。
    单一所有者本地工具、无多用户认证 → 用触发入口 source 作为 who 的代理。纯落盘、不抛改写错误给调用方以外。"""
    old = {str(k): round(float(v or 0.0), 4) for k, v in (old_weights or {}).items()}
    new = {str(k): round(float(v or 0.0), 4) for k, v in (new_weights or {}).items()}
    diff = [{"code": c, "old": old.get(c, 0.0), "new": new.get(c, 0.0)}
            for c in sorted(set(old) | set(new))
            if abs(new.get(c, 0.0) - old.get(c, 0.0)) > 1e-9]
    record_id = _now_id()
    record = {"id": record_id, "mode": "applied",
              "applied_at": datetime.now().isoformat(timespec="seconds"),
              "source": str(source or "unknown"),
              "input_fingerprint": fingerprint,
              "policy_version": policy_version,
              "product_quality_status": quality_status,
              "target_weight_diff": diff,
              "new_target_weights": new}
    os.makedirs(STRATEGIC_APPLIES_DIR, exist_ok=True)
    with open(os.path.join(STRATEGIC_APPLIES_DIR, f"{record_id}.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def load_strategic_applies(limit=None):
    if not os.path.exists(STRATEGIC_APPLIES_DIR):
        return []
    rows = []
    for fn in sorted(os.listdir(STRATEGIC_APPLIES_DIR), reverse=True):
        if fn.endswith(".json"):
            item = load_json(os.path.join(STRATEGIC_APPLIES_DIR, fn))
            if item:
                rows.append(item)
            if limit and len(rows) >= limit:
                break
    return rows


# 场内 ETF 交易成本：无印花税、无过户费，仅券商佣金。万0.5 / 最低 0.1 元/笔（银河证券，2026-06 起）。
COMMISSION_RATE = 0.00005
COMMISSION_MIN = 0.1


def estimate_commission(amount):
    """按场内 ETF 佣金估算手续费：max(最低0.1元, 成交额×万0.5)。amount<=0 → 0。"""
    amt = abs(float(amount or 0))
    if amt <= 0:
        return 0.0
    return round(max(COMMISSION_MIN, amt * COMMISSION_RATE), 2)


def _is_executed_item(item):
    status = str((item or {}).get("status") or "")
    return bool(status.strip()) and "未执行" not in status and "执行" in status


def apply_estimated_fees(items):
    """对已执行、且未显式填手续费(fee<=0)的成交项，按佣金(万0.5/最低0.1元)估算并写回 fee。

    就地修改并返回 items。让现金扣减(compute_holdings_draft)与台账记录(save_execution_record)
    都带上手续费，避免每笔少扣佣金导致现金被逐笔高估。显式填了正手续费的尊重用户输入、不覆盖。
    已知局限（L12，评估后保留现状）：fee=0 被当"未填"而非"免佣"——前端手续费为只读自动算、
    没有表达"真 0 费"的入口，且把 0 当有效值会重开 §4-1 的 fee:0 现金虚高漏洞；免佣户如需 0 费
    记账，应改前端提供显式开关而非放宽此哨兵。
    """
    for item in (items or []):
        if not isinstance(item, dict) or not _is_executed_item(item):
            continue
        if float(item.get("fee") or 0) > 0:
            continue
        amount = float(item.get("amount") or 0)
        if amount <= 0:
            amount = float(item.get("shares") or 0) * float(item.get("price") or 0)
        if amount > 0:
            item["fee"] = estimate_commission(amount)
    return items


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
        # （L13：原先此处还有一层 `if status and "未执行" not in status:`——上一行 continue 后恒真，已移除死分支）
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
            "suggested_amount": 0.0,       # 本月周报里"可执行"建议的合计金额（计划，按建议身份去重）
            "invested_amount": 0.0,        # 本月实际成交金额（执行）
            "skip_reason_counts": {},
            "portfolio_value_end": None,
            "_pv_date": None,
            "_sug": {},                    # M11：(source, code, 方向) → 当月最新建议金额（去重累计的载体）
        })

    for r in _formal_reports_for_review(_all_reports()):
        summ = r.get("summary") or {}
        gen = summ.get("generated_for") or r.get("created_at") or r.get("id")
        b = bucket(_month_of(gen))
        b["reports"] += 1
        sig = r.get("signals") or {}
        # M11（2026-06-10 审查）：同一持续性建议会在当月多份日报里反复出现，不再按日累计
        # （此前 6 月"建议 ¥85,532 vs 实际 ¥40,887"严重失真）。按建议身份 (source, code, 方向)
        # 月内去重，金额取当月最后一份周报的版本（5/25 建议金额随价格漂移，最新版最接近计划口径）。
        has_lists = bool((sig.get("actionable_rebalance") or [])
                         or (sig.get("first_funding_plan") or {}).get("orders"))
        if has_lists:
            for a in sig.get("actionable_rebalance") or []:
                if a.get("actionable"):
                    b["_sug"][("rebalance", str(a.get("code")), str(a.get("suggest") or ""))] = \
                        float(a.get("approx_amount") or 0)
            for o in (sig.get("first_funding_plan") or {}).get("orders", []):
                if o.get("actionable"):
                    b["_sug"][("first_funding", str(o.get("code")), "buy")] = float(o.get("estimated_amount") or 0)
        else:   # 旧/轻量报告无 signals 明细 → 退回 summary 计数（无法按身份去重，如实累计）
            b["suggested_actions"] += int(summ.get("actionable_count") or 0) + int(summ.get("first_funding_count") or 0)
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
            if "未执行" in status or (not status):
                b["skipped_items"] += 1
                reason = (it.get("reason") or "").strip() or "（未填原因）"
                b["skip_reason_counts"][reason] = b["skip_reason_counts"].get(reason, 0) + 1
            else:  # 已执行 / 部分执行
                if "部分" in status:
                    b["partial_items"] += 1
                else:
                    b["executed_items"] += 1
                b["fees_total"] += float(it.get("fee") or 0)   # L13：只累计已执行项的费用（未执行没有费）
                b["invested_amount"] += float(it.get("amount") or 0)
                if not (it.get("suggestion_source") or "").strip():
                    b["off_plan_items"] += 1

    result = []
    for m in sorted(months, reverse=True):
        b = months[m]
        b["suggested_actions"] += len(b["_sug"])             # M11：去重后的建议动作数
        b["suggested_amount"] += sum(b["_sug"].values())     # M11：去重后的计划金额（取当月最新版）
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
    path = os.path.join(NAV_DIR, f"{safe_name(as_of)}.json")
    # 幂等：同日已有快照且三项财务值（etf_value/cash/portfolio_value）未变 → 不重写文件，
    # 保留原 created_at。否则每次启动/刷新都翻 created_at，把 journal/nav 弄脏污染 git。
    prev = load_json(path)
    if prev and all(prev.get(k) == snap[k] for k in ("etf_value", "cash", "portfolio_value", "holdings")):
        return prev
    try:
        os.makedirs(NAV_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        return None
    return snap


def load_nav_series():
    if not os.path.exists(NAV_DIR):
        return []
    rows = [load_json(os.path.join(NAV_DIR, fn)) for fn in sorted(os.listdir(NAV_DIR)) if fn.endswith(".json")]
    return sorted([r for r in rows if r and r.get("as_of")], key=lambda r: r["as_of"])


def apply_execution_to_nav_snapshot(items, when=None):
    """成交登记后就地校准【当日】NAV 快照，使其成为"成交后值"。

    背景：compute_twr 约定"流计入区间末"——若当日快照落于成交前（如开盘前生成信号），
    而成交流日期也是当日，TWR 会把净买入当成当期亏损（约 −净买入/期初，纯假象）并留下永久残差。
    买入：etf_value += amount（成本近似市值）、cash −= amount+fee；卖出：etf_value −= amount、cash += amount−fee。
    当日无快照则不动（流会落到下一个快照的区间，那个快照天然是成交后值）。纯 IO、失败返 None。"""
    when = when or datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(NAV_DIR, f"{safe_name(when)}.json")
    snap = load_json(path)
    if not snap:
        return None
    etf = float(snap.get("etf_value") or 0)
    cash = float(snap.get("cash") or 0)
    holdings = {str(h.get("code")): dict(h) for h in (snap.get("holdings") or []) if h.get("code")}
    applied = 0
    for it in items or []:
        status = str(it.get("status") or "")
        if "未执行" in status or "执行" not in status:   # 与 cash_flows_from_executions 同口径
            continue
        amount = float(it.get("amount") or 0)
        fee = float(it.get("fee") or 0)
        if not amount:
            continue
        if _execution_side(it) == "buy":
            etf += amount
            cash -= (amount + fee)
            side_sign = 1
        else:
            etf -= amount
            cash += (amount - fee)
            side_sign = -1
        code = str(it.get("code") or "")
        shares = float(it.get("shares") or 0)
        price = float(it.get("price") or 0)
        if code and shares > 0:
            row = holdings.get(code) or {"code": code, "name": it.get("name") or code, "shares": 0.0}
            row["shares"] = max(0.0, float(row.get("shares") or 0) + side_sign * shares)
            if price > 0:
                row["price"] = price
                row["value"] = round(row["shares"] * price, 2)
                row["price_source"] = "execution"
                row["price_as_of"] = when
            holdings[code] = row
        applied += 1
    if not applied:
        return None
    snap["etf_value"] = round(etf, 2)
    snap["cash"] = round(cash, 2)
    snap["portfolio_value"] = round(etf + cash, 2)
    snap["holdings"] = [holdings[c] for c in sorted(holdings)]
    snap["created_at"] = datetime.now().isoformat(timespec="seconds")
    snap["post_trade_adjusted"] = True   # 注记：本快照已按当日成交校准为"成交后值"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        return None
    return snap


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
        return {"available": False, "reason": "IRR 暂无解（现金流无符号变化，或短窗年化超出求解区间——快照多攒几天即可）"}
    return {"available": True, "mwr": round(r, 4), "start": pts[0]["as_of"], "end": pts[-1]["as_of"]}


def compute_asset_attribution(nav_series, flows):
    """用相邻 NAV 持仓快照拆解 ETF 市场损益；返回可核对的金额贡献与残差。"""
    pts = sorted([p for p in nav_series if p.get("etf_value") is not None], key=lambda p: p["as_of"])
    if len(pts) < 2:
        return {"available": False, "reason": "NAV 快照不足 2 个，无法归因"}
    flows = sorted(flows or [], key=lambda f: f.get("date", ""))
    by_code, total_market_pnl, capital_base, intervals = {}, 0.0, 0.0, 0
    for k in range(1, len(pts)):
        p0, p1 = pts[k - 1], pts[k]
        v0, v1 = float(p0.get("etf_value") or 0), float(p1.get("etf_value") or 0)
        if v0 <= 0:
            continue
        period_flow = sum(float(f.get("amount") or 0) for f in flows
                          if p0["as_of"] < str(f.get("date") or "") <= p1["as_of"])
        total_market_pnl += (v1 - period_flow) - v0
        capital_base += v0
        intervals += 1
        h0 = {str(h.get("code")): h for h in (p0.get("holdings") or []) if h.get("code")}
        h1 = {str(h.get("code")): h for h in (p1.get("holdings") or []) if h.get("code")}
        for code, a in h0.items():
            b = h1.get(code) or {}
            shares = float(a.get("shares") or 0)
            px0, px1 = float(a.get("price") or 0), float(b.get("price") or 0)
            if shares <= 0 or px0 <= 0 or px1 <= 0:
                continue
            row = by_code.setdefault(code, {"code": code, "name": a.get("name") or b.get("name") or code,
                                             "pnl": 0.0, "intervals": 0})
            row["pnl"] += shares * (px1 - px0)
            row["intervals"] += 1
    if intervals == 0 or capital_base <= 0:
        return {"available": False, "reason": "尚无正持仓跨日区间，无法归因"}
    rows = []
    for row in by_code.values():
        row = dict(row)
        row["pnl"] = round(row["pnl"], 2)
        row["contribution"] = round(row["pnl"] / capital_base, 6)
        rows.append(row)
    rows.sort(key=lambda x: abs(x["pnl"]), reverse=True)
    explained = sum(r["pnl"] for r in rows)
    residual = total_market_pnl - explained
    return {"available": True, "by_asset": rows, "market_pnl": round(total_market_pnl, 2),
            "explained_pnl": round(explained, 2), "residual_pnl": round(residual, 2),
            "residual_bps": round(residual / capital_base * 10000, 2),
            "capital_base": round(capital_base, 2), "intervals": intervals,
            "start": pts[0]["as_of"], "end": pts[-1]["as_of"]}


def execution_cost_attribution(executions):
    """拆出成交价相对决策时市场价/IOPV 的成本；缺引用价时诚实跳过。"""
    rows, covered = {}, 0
    for rec in executions or []:
        for item in rec.get("items") or []:
            if not _is_executed_item(item):
                continue
            code = str(item.get("code") or "")
            shares, price = float(item.get("shares") or 0), float(item.get("price") or 0)
            if not code or shares <= 0 or price <= 0:
                continue
            side = _execution_side(item)
            sign = 1.0 if side == "buy" else -1.0
            ref_price = float(item.get("reference_price") or item.get("suggested_price") or 0)
            ref_iopv = float(item.get("reference_iopv") or 0)
            row = rows.setdefault(code, {"code": code, "decision_to_execution_cost": 0.0,
                                          "nav_premium_cost": 0.0, "trades": 0})
            if ref_price > 0:
                row["decision_to_execution_cost"] += sign * (price - ref_price) * shares
                covered += 1
            if ref_iopv > 0:
                row["nav_premium_cost"] += sign * (price - ref_iopv) * shares
            row["trades"] += 1
    out = []
    for row in rows.values():
        row = dict(row)
        row["decision_to_execution_cost"] = round(row["decision_to_execution_cost"], 2)
        row["nav_premium_cost"] = round(row["nav_premium_cost"], 2)
        out.append(row)
    return {"available": bool(covered), "covered_trades": covered, "by_asset": out,
            "decision_to_execution_cost": round(sum(r["decision_to_execution_cost"] for r in out), 2),
            "nav_premium_cost": round(sum(r["nav_premium_cost"] for r in out), 2),
            "reason": None if covered else "历史成交缺少决策时参考价；新成交开始积累后可计算"}


def static_target_benchmark(asset_points, target_weights, start, end):
    """相同区间、按期初目标权重买入持有的静态组合基准。"""
    if not asset_points or not target_weights or not start or not end:
        return {"available": False, "reason": "目标组合价格序列不足"}
    missing, rows, total = [], [], 0.0
    for code, weight in target_weights.items():
        weight = float(weight or 0)
        if weight <= 0:
            continue
        pts = sorted([p for p in (asset_points.get(str(code)) or [])
                      if start <= str(p.get("date") or "") <= end], key=lambda p: p["date"])
        if len(pts) < 2 or float(pts[0].get("close") or 0) <= 0:
            missing.append(str(code))
            continue
        ret = float(pts[-1]["close"]) / float(pts[0]["close"]) - 1.0
        contribution = weight * ret
        total += contribution
        rows.append({"code": str(code), "weight": round(weight, 6), "return": round(ret, 6),
                     "contribution": round(contribution, 6)})
    if missing:
        return {"available": False, "reason": "部分目标资产缺少同区间价格", "missing": missing}
    days = (_pdate(end) - _pdate(start)).days
    annualized = ((1.0 + total) ** (365.0 / days) - 1.0) if days > 0 and total > -1 else None
    return {"available": True, "twr": round(total, 6),
            "annualized": round(annualized, 6) if annualized is not None else None,
            "start": start, "end": end, "name": "静态目标组合", "by_asset": rows}


def performance_summary(benchmark_points=None, target_weights=None, asset_points=None):
    """汇总 TWR/MWR、市场基准、静态目标基准与可核对实盘归因。"""
    navs = load_nav_series()
    executions = load_executions()
    flows, total_fee = cash_flows_from_executions(executions)
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
    start = navs[0]["as_of"] if navs else None
    end = navs[-1]["as_of"] if navs else None
    target_bench = static_target_benchmark(asset_points, target_weights, start, end)
    attribution = compute_asset_attribution(navs, flows)
    execution_costs = execution_cost_attribution(executions)
    avg_cash_weight = None
    valid_cash = [float(p.get("cash") or 0) / float(p.get("portfolio_value") or 1)
                  for p in navs if float(p.get("portfolio_value") or 0) > 0]
    if valid_cash:
        avg_cash_weight = sum(valid_cash) / len(valid_cash)
    cash_effect = None
    if avg_cash_weight is not None and target_bench.get("available"):
        cash_effect = -avg_cash_weight * float(target_bench.get("twr") or 0)
    benchmark_gap = None
    if twr.get("available") and target_bench.get("available"):
        benchmark_gap = float(twr.get("twr") or 0) - float(target_bench.get("twr") or 0)
    capital_base = float((attribution or {}).get("capital_base") or 0)
    fee_impact = (-total_fee / capital_base) if capital_base > 0 else None
    return {"twr": twr, "mwr": mwr, "benchmark": bench, "target_benchmark": target_bench,
            "attribution": attribution, "execution_costs": execution_costs,
            "cash_effect": round(cash_effect, 6) if cash_effect is not None else None,
            "benchmark_gap": round(benchmark_gap, 6) if benchmark_gap is not None else None,
            "fee_impact": round(fee_impact, 6) if fee_impact is not None else None,
            "total_fees": total_fee, "snapshots": len(navs),
            "nav_curve": [{"date": p["as_of"], "etf_value": p["etf_value"], "portfolio_value": p.get("portfolio_value")}
                          for p in navs],
            "caveats": ["已剔除注入本金：TWR 按时间加权、MWR 按资金加权(XIRR)，不把追加本金当收益。",
                        "归因使用相邻 NAV 的期初持仓；区间内成交、缺价和四舍五入进入残差，残差越小越可核对。",
                        "静态目标组合为期初权重买入持有；现金影响是近似值。非承诺、仅历史回看；费用单列、未计税。"]}


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
            reason = str(it.get("reason") or "")
            if side not in ("buy", "sell"):
                side = "sell" if ("卖" in reason or "减" in reason) else "buy"
                warnings.append(f"{code} 一笔未标方向，按{'卖出' if side == 'sell' else '买入'}处理")
            elif side == "buy" and ("卖" in reason or "减" in reason):
                warnings.append(f"{code} 方向标「买入」但原因写「{reason}」——若实为卖出，请把该行方向改成「卖出」再登记，否则份额与现金会双向算反")
            elif side == "sell" and ("买" in reason or "加" in reason):
                warnings.append(f"{code} 方向标「卖出」但原因写「{reason}」——若实为买入，请把该行方向改成「买入」再登记，否则份额与现金会双向算反")
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
