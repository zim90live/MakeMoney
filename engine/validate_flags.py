#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────
# 【单一事实源 / SINGLE SOURCE OF TRUTH】 与 engine/signals.py、engine/backtest.py 同属唯一实现。
# 作用：把 AI 增强层的『风险旗标』变成可机械校验的结构，杜绝自由发挥 / 事后解释。
# ─────────────────────────────────────────────────────────────────────────
"""
校验 AI 增强层产出的风险旗标(engine/flags.json)是否符合 engine/flags_schema.json。

用法：python3 engine/validate_flags.py [flags.json]
     python3 engine/validate_flags.py --init-empty
  - 校验通过退出码 0，失败退出码 1。
  - 空 flags 数组视为合法（本周无重大事件）。
  - --init-empty 会生成 engine/flags.json，表示本周无重大事件。
"""
import json
import os
import sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def configure_console_encoding():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def die(m):
    print(f"[错误] {m}", file=sys.stderr)
    sys.exit(2)


def find_repo_root(start):
    d = start
    for _ in range(8):
        if os.path.exists(os.path.join(d, "portfolio.yaml")):
            return d
        p = os.path.dirname(d)
        if p == d:
            break
        d = p
    return None


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def validate_flags_data(data, universe=None, today=None):
    """纯函数：按 flags_schema.json 校验旗标数据，返回 (errors, warns)。不打印、不退出。

    供运行时管道（reports.load_validated_flags）与 CLI 复用（单一实现）。
    universe=None → 跳过 affected_assets「须在 universe 内」一项（其余核心校验照常）；today=None → 用今天。
    """
    schema = load_json(os.path.join(HERE, "flags_schema.json"))
    flags = (data or {}).get("flags", [])
    errors, warns = [], []
    if not isinstance(flags, list):
        return ["flags 必须是数组"], warns
    cats = set(schema["categories"])
    dirs = set(schema["directions"])
    confs = set(schema["confidences"])
    required = schema["required_fields"]
    uni = {str(c) for c in universe} if universe else set()
    today = today or date.today()
    for i, f in enumerate(flags):
        p = f"旗标#{i + 1}"
        if not isinstance(f, dict):
            errors.append(f"{p}: 不是对象")
            continue
        for k in required:
            v = f.get(k)
            if v is None or v == "" or v == []:
                errors.append(f"{p}: 缺少字段 {k}")
        if f.get("category") not in cats:
            errors.append(f"{p}: category 非法（须 ∈ {sorted(cats)}）")
        if f.get("direction") not in dirs:
            errors.append(f"{p}: direction 非法（须 ∈ {sorted(dirs)}）")
        if f.get("confidence") not in confs:
            errors.append(f"{p}: confidence 非法（须 ∈ {sorted(confs)}）")

        d = f.get("date")
        dt = None
        if isinstance(d, str):
            try:
                dt = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                errors.append(f"{p}: date 格式须 YYYY-MM-DD（得到 {d!r}）")
        if dt:
            if dt > today:
                errors.append(f"{p}: date 在未来（{d}）")
            elif (today - dt).days > 21:
                warns.append(f"{p}: 事件已超过 21 天（{d}），可能不算本周新事件")

        aa = f.get("affected_assets")
        if isinstance(aa, list):
            for code in aa:
                if code != "ALL" and uni and str(code) not in uni:
                    errors.append(f"{p}: affected_assets 含未知代码 {code}（须在 universe 内或 'ALL'）")

        act = f.get("actionable")
        if not isinstance(act, bool):
            errors.append(f"{p}: actionable 必须是 true/false")
        elif f.get("confidence") == "低" and act is True:
            errors.append(f"{p}: 低置信度不得 actionable=true")

        su = f.get("source_url")
        if su is not None and su != "":
            if not isinstance(su, str) or not (su.startswith("http://") or su.startswith("https://")):
                errors.append(f"{p}: source_url 须为 http(s) 链接（可选字段；没有就省略）")

    if len(flags) > 5:
        warns.append(f"旗标共有 {len(flags)} 条（建议 ≤5，避免噪音）")
    return errors, warns


def _load_universe(root):
    if not root:
        return set()
    try:
        import yaml
    except ImportError:
        die("缺少依赖 pyyaml，请先运行：pip install -r engine/requirements.txt")
    sp = os.path.join(root, "strategy.yaml")
    if not os.path.exists(sp):
        return set()
    with open(sp, encoding="utf-8") as f:
        st = yaml.safe_load(f) or {}
    return {str(u["code"]) for u in (st.get("universe") or [])}


def main():
    configure_console_encoding()
    if "--init-empty" in sys.argv[1:]:
        out = os.path.join(HERE, "flags.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"generated_for": str(date.today()), "flags": []}, f, ensure_ascii=False, indent=2)
        print(f"✓ 已生成空旗标文件：{out}（本周无重大事件）")
        sys.exit(0)

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags_path = args[0] if args else os.path.join(HERE, "flags.json")
    if not os.path.exists(flags_path):
        die(f"找不到 {flags_path}；AI 增强层应先把旗标写到这里（无事件则写 {{\"flags\": []}}）。")

    data = load_json(flags_path)
    errors, warns = validate_flags_data(data, universe=_load_universe(find_repo_root(HERE)))
    flags = data.get("flags", [])
    flags = flags if isinstance(flags, list) else []

    if not flags and not errors:
        print("✓ flags 为空 → 本周无重大事件，校验通过")
    for w in warns:
        print(f"[提示] {w}")
    if errors:
        print(f"✗ 校验未通过，共 {len(errors)} 处问题：")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    if flags:
        print(f"✓ {len(flags)} 条旗标全部合规")
    sys.exit(0)


if __name__ == "__main__":
    main()
