#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────
# 【单一事实源 / SINGLE SOURCE OF TRUTH】 观察池学习系统的唯一实现。
#
#   把"观察池"做成"学完才解锁讨论纳入"的学习系统：
#     · 学习卡内容：engine/learning_cards.yaml（教育内容，可入库）
#     · 观察次数：从 reports/ 周报归档里统计该 code 出现过几次（= 观察了几周）
#     · 学习确认：用户在前端点"我已学习理解"，记到 journal/learning/<code>.json（个人，不入库）
#     · 解锁门槛：观察次数 ≥ UNLOCK_MIN_OBSERVATIONS 且已确认学习 → "可讨论纳入持仓池"
#
#   ⚠️ 铁律：观察池永远不触发买卖；"解锁"也只是允许『和助手讨论是否纳入』，
#            纳入仍需用户手动改 strategy.yaml 的 universe 并手动下单。这里不写任何交易动作。
# ─────────────────────────────────────────────────────────────────────────
import json
import os
import re
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPORTS_DIR = os.path.join(ROOT, "reports")
LEARNING_DIR = os.path.join(ROOT, "journal", "learning")
CARDS_PATH = os.path.join(HERE, "learning_cards.yaml")

UNLOCK_MIN_OBSERVATIONS = 4   # 至少在 4 份周报里被观察过，才算"观察够了"

try:
    import yaml
except ImportError:  # pragma: no cover - 依赖缺失时降级
    yaml = None


def _safe_code(s):
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", str(s)).strip("_") or "item"


def _load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def load_cards():
    """读取学习卡内容，返回 {code: card}；缺文件或缺依赖时返回 {}。"""
    if yaml is None or not os.path.exists(CARDS_PATH):
        return {}
    try:
        with open(CARDS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return {}
    cards = data.get("cards") or {}
    return {str(k): v for k, v in cards.items()}


def observed_counts():
    """统计每个观察池 code 在多少份周报里成功出现过（= 观察了几次/周）。"""
    counts = {}
    if not os.path.exists(REPORTS_DIR):
        return counts
    for name in os.listdir(REPORTS_DIR):
        report = _load_json(os.path.join(REPORTS_DIR, name, "report.json"))
        if not report:
            continue
        ws = ((report.get("signals") or {}).get("watchlist_signals")) or {}
        for code, sig in ws.items():
            if isinstance(sig, dict) and "error" not in sig:
                counts[str(code)] = counts.get(str(code), 0) + 1
    return counts


def load_acks():
    """读取所有学习确认记录，返回 {code: ack_record}。"""
    acks = {}
    if not os.path.exists(LEARNING_DIR):
        return acks
    for fn in os.listdir(LEARNING_DIR):
        if fn.endswith(".json"):
            rec = _load_json(os.path.join(LEARNING_DIR, fn))
            if rec and rec.get("code"):
                acks[str(rec["code"])] = rec
    return acks


def save_ack(code, acknowledged=True, notes=""):
    """记录/更新某 code 的学习确认。返回写入的记录。"""
    code = str(code).strip()
    if not code:
        raise ValueError("缺少 code")
    os.makedirs(LEARNING_DIR, exist_ok=True)
    rec = {
        "code": code,
        "acknowledged": bool(acknowledged),
        "acknowledged_at": datetime.now().isoformat(timespec="seconds") if acknowledged else None,
        "notes": (notes or "").strip(),
    }
    with open(os.path.join(LEARNING_DIR, f"{_safe_code(code)}.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    return rec


def _unlock_state(observed, acknowledged, min_obs=UNLOCK_MIN_OBSERVATIONS):
    """计算解锁状态。注意：解锁只代表'可讨论纳入'，不代表可买入。"""
    remaining = max(0, min_obs - observed)
    if acknowledged and observed >= min_obs:
        return ("unlocked", "已满足学习门槛：可与助手讨论是否纳入持仓池（仍需手动加入、手动下单）")
    if not acknowledged and observed >= min_obs:
        return ("need_ack", f"已观察 {observed} 次，达到次数门槛；请先完成学习卡确认再讨论纳入")
    if acknowledged and observed < min_obs:
        return ("observing", f"学习已确认，但还需继续观察 {remaining} 次（共 {min_obs} 次）才可讨论纳入")
    return ("learning", f"待学习：先读懂学习卡并确认，同时累计观察（还需 {remaining} 次）")


def watchlist_learning(strat):
    """合并 watchlist + 学习卡 + 观察次数 + 学习确认 + 解锁状态。

    返回列表，每项含：code/name/role/asset/note/card/observed/min_observations/
    acknowledged/acknowledged_at/unlock_status/unlock_reason/buyable(恒为 False)。
    """
    cards = load_cards()
    counts = observed_counts()
    acks = load_acks()
    items = []
    for w in (strat.get("watchlist") or []):
        code = str(w.get("code"))
        ack = acks.get(code) or {}
        acknowledged = bool(ack.get("acknowledged"))
        observed = int(counts.get(code, 0))
        status, reason = _unlock_state(observed, acknowledged)
        items.append({
            "code": code,
            "name": w.get("name", code),
            "role": w.get("role"),
            "asset": w.get("asset"),
            "note": w.get("note"),
            "card": cards.get(code),
            "observed": observed,
            "min_observations": UNLOCK_MIN_OBSERVATIONS,
            "acknowledged": acknowledged,
            "acknowledged_at": ack.get("acknowledged_at"),
            "unlock_status": status,
            "unlock_reason": reason,
            "buyable": False,   # 铁律：观察池永不可直接买入
        })
    return items
