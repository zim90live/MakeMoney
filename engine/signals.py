#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────
# 【单一事实源 / SINGLE SOURCE OF TRUTH】 weekly-briefing 技能的唯一实现。
#
#   代码只此一份：     engine/signals.py（本文件）、engine/backtest.py、engine/validate_flags.py
#   用户配置（根目录）：portfolio.yaml（持仓，每周改）、strategy.yaml（策略参数）
#   两个 agent 入口（都只放 SKILL.md，调用本文件，无第二份代码）：
#       Claude →  .claude/skills/weekly-briefing/SKILL.md
#       Codex  →  .agents/skills/weekly-briefing/SKILL.md
#
#   ⚠️ 评审者（人或 AI）请注意：回测在 engine/backtest.py；不存在重复脚本；路径统一为 engine/...。
# ─────────────────────────────────────────────────────────────────────────
"""
周度信号引擎（量化骨架）。

读取 strategy.yaml + portfolio.yaml，多源拉取场内 ETF 的日终行情与估值，
计算 趋势 / 动量 / 估值分位 / 再平衡偏离，写出 signals.json 供 AI 增强层使用。

稳健性：
  - 行情多源：东方财富 → 新浪 → 本地缓存(engine/cache/)。
  - 估值缓存：估值接口失败时回退缓存，并给出 valuation_status（available/source/reason）。
    —— 估值"缺失"会被明确标注，绝不能被当成"中性"。
  - 数据新鲜度分级：完整 / 缓存可用 / 过旧 / 部分缺失；只有"完整/缓存可用"才允许再平衡建议。

这是自用私人投顾工具，输出的是"信号与建议"供所有者本人决策；不承诺收益，回测好 ≠ 未来赚钱。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime


def configure_console_encoding():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


configure_console_encoding()


def die(msg):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(1)


try:
    import yaml
except ImportError:
    die("缺少依赖 pyyaml，请先运行：pip install -r engine/requirements.txt")
try:
    import pandas as pd
except ImportError:
    die("缺少依赖 pandas，请先运行：pip install -r engine/requirements.txt")
try:
    import akshare as ak
except ImportError:
    die("缺少依赖 akshare，请先运行：pip install -r engine/requirements.txt")

import tactical  # noqa: E402  双向战术配置纯函数（Phase A 影子，只读不产生可执行交易）


HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
STALE_LIMIT_DAYS = 10        # 行情最新日期超过此日历天数 → "过旧"，禁用交易建议
VAL_STALE_LIMIT_DAYS = 30    # 估值缓存超过此天数 → 视为不可用（估值变化慢，限额可宽些）

DEFAULT_INVESTOR_PROFILE = {
    "target_annual_return": 0.05,
    "horizon_years": 5,
    "max_acceptable_drawdown": 0.15,
    "experience_level": "beginner",
    "emergency_cash_kept_outside": 0,
    "monthly_contribution": 0,
    "total_assets": 0,
    "stable_assets_outside": 0,     # 场外稳健桶（活期/固收/定存）：让算法知道有这笔缓冲，做全组合口径
    "stable_assets_yield": 0.025,   # 稳健桶假设年化（仅用于混合收益展示）
    "planned_etf_capital": 0,       # ETF 风险桶目标上限：用于缓冲比例与目标权重测算（0=不启用缓冲，按 ETF 桶自身回撤预算）
    "unemployment_monthly_expense": 6000,
    "unemployment_minimum_monthly_income": 0,
    "unemployment_runway_years": 5,
    "post_stress_reserve_months": 12,
}


def find_repo_root(start):
    d = start
    for _ in range(8):
        if os.path.exists(os.path.join(d, "portfolio.yaml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_investor_profile(root):
    path = os.path.join(root, "investor_profile.yaml") if root else None
    if path and os.path.exists(path):
        try:
            data = load_yaml(path) or {}
            return {**DEFAULT_INVESTOR_PROFILE, **data}
        except Exception:  # noqa: BLE001
            return dict(DEFAULT_INVESTOR_PROFILE)
    return dict(DEFAULT_INVESTOR_PROFILE)


def _num_ok(v, lo=None, hi=None, positive=False):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    if positive and not v > 0:
        return False
    if lo is not None and v < lo:
        return False
    if hi is not None and v > hi:
        return False
    return True


def validate_config(port, strat):
    """校验 portfolio.yaml，返回错误列表。"""
    errs = []
    uni_codes = {str(u.get("code")) for u in (strat.get("universe") or [])}
    holdings = port.get("holdings") or []
    if not holdings:
        errs.append("portfolio.yaml 没有任何 holdings")
    cash = port.get("cash", 0)
    if not _num_ok(cash, lo=0):
        errs.append(f"cash 非法（需 ≥0 的数字）：{cash!r}")
    seen, tw_sum = set(), 0.0
    for h in holdings:
        code = str(h.get("code"))
        if code in seen:
            errs.append(f"重复的 ETF 代码：{code}")
        seen.add(code)
        if uni_codes and code not in uni_codes:
            errs.append(f"{code} 不在 strategy.yaml 的 universe 里")
        if not _num_ok(h.get("shares", 0), lo=0):
            errs.append(f"{code} 的 shares 非法（需 ≥0 的数字）：{h.get('shares')!r}")
        tw = h.get("target_weight", 0)
        if not _num_ok(tw, lo=0, hi=1):
            errs.append(f"{code} 的 target_weight 非法（需 0~1）：{tw!r}")
        else:
            tw_sum += tw
    if holdings and abs(tw_sum - 1.0) > 0.01:
        errs.append(f"target_weight 合计 = {tw_sum:.3f}，应接近 1.0")
    return errs


def validate_strategy(strat):
    """校验 strategy.yaml，避免后续 KeyError 或不合理参数。"""
    errs = []
    uni = strat.get("universe") or []
    if not uni:
        errs.append("strategy.yaml 的 universe 为空")
    codes = [str(u.get("code")) for u in uni]
    if len(codes) != len(set(codes)):
        errs.append("universe 存在重复代码")
    bonds = [u for u in uni if u.get("asset") == "bond"]
    if len(bonds) != 1:
        errs.append(f"universe 必须有且仅有一个 asset:bond（现 {len(bonds)} 个）")
    F = strat.get("factors") or {}
    tf, mo, va, rb = (F.get(k, {}) for k in ("trend_filter", "momentum", "valuation", "rebalance"))
    if not _num_ok(tf.get("ma_days"), positive=True):
        errs.append("trend_filter.ma_days 须为正数")
    if not _num_ok(mo.get("lookback_days"), positive=True):
        errs.append("momentum.lookback_days 须为正数")
    if not _num_ok(va.get("lookback_years"), positive=True):
        errs.append("valuation.lookback_years 须为正数")
    cp, rp = va.get("cheap_pct"), va.get("rich_pct")
    if not _num_ok(cp, lo=0, hi=1):
        errs.append("valuation.cheap_pct 须在 0~1")
    if not _num_ok(rp, lo=0, hi=1):
        errs.append("valuation.rich_pct 须在 0~1")
    if _num_ok(cp) and _num_ok(rp) and not cp < rp:
        errs.append("valuation.cheap_pct 必须 < rich_pct")
    if not _num_ok(rb.get("abs_threshold_pp"), positive=True):
        errs.append("rebalance.abs_threshold_pp 须为正数")
    if not _num_ok(rb.get("rel_threshold"), lo=0, hi=1) or rb.get("rel_threshold", 0) <= 0:
        errs.append("rebalance.rel_threshold 须在 (0,1]")
    if "check_frequency" in rb and str(rb.get("check_frequency")).lower() not in ("weekly", "biweekly", "monthly", "quarterly"):
        errs.append("rebalance.check_frequency 须为 weekly/biweekly/monthly/quarterly")
    if "circuit_breaker_pp" in rb and not (_num_ok(rb.get("circuit_breaker_pp"), positive=True)
                                           and float(rb.get("circuit_breaker_pp")) > float(rb.get("abs_threshold_pp", 0) or 0)):
        errs.append("rebalance.circuit_breaker_pp 须为正数且大于 abs_threshold_pp")
    for fk in ("trend_filter", "momentum", "valuation", "rebalance"):
        if not isinstance(F.get(fk, {}).get("enabled"), bool):
            errs.append(f"factors.{fk}.enabled 须为 true/false")
    rp = strat.get("risk_profile")
    if rp is not None and rp not in ("保守", "平衡", "进取"):
        errs.append("risk_profile 须为 保守/平衡/进取")
    rc = strat.get("risk_controls") or {}
    if rc:
        if not _num_ok(rc.get("min_trade_amount", 0), lo=0):
            errs.append("risk_controls.min_trade_amount 须为 ≥0 的数字")
        if not _num_ok(rc.get("max_weekly_trade_amount", 0), lo=0):
            errs.append("risk_controls.max_weekly_trade_amount 须为 ≥0 的数字")
        if not _num_ok(rc.get("first_tranche_pct", 0), lo=0, hi=1):
            errs.append("risk_controls.first_tranche_pct 须在 0~1")
        if not isinstance(rc.get("allow_trade_with_cache", False), bool):
            errs.append("risk_controls.allow_trade_with_cache 须为 true/false")
    watch = strat.get("watchlist") or []
    watch_codes = [str(w.get("code")) for w in watch]
    if len(watch_codes) != len(set(watch_codes)):
        errs.append("watchlist 存在重复代码")
    overlap = sorted(set(codes) & set(watch_codes))
    if overlap:
        errs.append(f"watchlist 与 universe 重复：{', '.join(overlap)}")
    for w in watch:
        code = str(w.get("code", "")).strip()
        if not code:
            errs.append("watchlist 存在空 code")
        if not w.get("name"):
            errs.append(f"watchlist {code} 缺少 name")
        if not w.get("role"):
            errs.append(f"watchlist {code} 缺少 role")
        if not w.get("asset"):
            errs.append(f"watchlist {code} 缺少 asset")
    asm = strat.get("assumptions")
    if asm is not None:
        if not isinstance(asm, dict):
            errs.append("assumptions 须为映射")
        else:
            d = asm.get("defaults") or {}
            if "shock" in d and not _num_ok(d.get("shock"), lo=-1, hi=0):
                errs.append("assumptions.defaults.shock 越界（应在 [-1,0]）")
            if "expected_return" in d and not _num_ok(d.get("expected_return"), lo=-1, hi=1):
                errs.append("assumptions.defaults.expected_return 越界（应在 [-1,1]）")
            if "return_haircut" in d and not _num_ok(d.get("return_haircut"), lo=0, hi=0.15):
                errs.append("assumptions.defaults.return_haircut 越界（应在 [0,0.15]）")   # 批3：§9.1 边界校验
            sl = asm.get("sleeves") or {}
            if not isinstance(sl, dict):
                errs.append("assumptions.sleeves 须为映射")
            else:
                for asset, cfg in sl.items():
                    cfg = cfg or {}
                    if "shock" in cfg and not _num_ok(cfg.get("shock"), lo=-1, hi=0):
                        errs.append(f"assumptions.sleeves.{asset}.shock 越界（应在 [-1,0]）")
                    if "expected_return" in cfg and not _num_ok(cfg.get("expected_return"), lo=-1, hi=1):
                        errs.append(f"assumptions.sleeves.{asset}.expected_return 越界（应在 [-1,1]）")
                    for rk in ("return_conservative", "return_optimistic"):   # 批3：显式 per-sleeve 区间边界
                        if rk in cfg and not _num_ok(cfg.get(rk), lo=-1, hi=1):
                            errs.append(f"assumptions.sleeves.{asset}.{rk} 越界（应在 [-1,1]）")
                    rc_, ro_, ce_ = cfg.get("return_conservative"), cfg.get("return_optimistic"), cfg.get("expected_return")
                    if _num_ok(rc_) and _num_ok(ro_) and rc_ > ro_:           # 批3：断言 conservative ≤ optimistic
                        errs.append(f"assumptions.sleeves.{asset}：return_conservative 不得高于 return_optimistic")
                    if _num_ok(rc_) and _num_ok(ce_) and rc_ > ce_:           # conservative ≤ central
                        errs.append(f"assumptions.sleeves.{asset}：return_conservative 不得高于 expected_return")
                    if _num_ok(ro_) and _num_ok(ce_) and ro_ < ce_:           # central ≤ optimistic
                        errs.append(f"assumptions.sleeves.{asset}：return_optimistic 不得低于 expected_return")
                    for sk in ("source", "note"):
                        if sk in cfg and not isinstance(cfg.get(sk), str):
                            errs.append(f"assumptions.sleeves.{asset}.{sk} 须为字符串")
    return errs


# ---------- 行情多源取数 + 缓存 ----------

def _norm(df):
    cols = ["date", "close"] + (["amount"] if "amount" in df.columns else [])
    df = df[cols].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    return df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)


def _try_em(code, retries):
    for _ in range(retries):
        try:
            d = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="qfq")
            if d is not None and not d.empty:
                d = d.rename(columns={"日期": "date", "收盘": "close"})
                if "close" in d.columns:
                    return _norm(d)
        except Exception:  # noqa: BLE001
            time.sleep(1.2)
    return None


def _try_sina(code, retries):
    prefix = "sh" if code[:1] in ("5", "6") else "sz"
    for _ in range(retries):
        try:
            d = ak.fund_etf_hist_sina(symbol=prefix + code)
            if d is not None and not d.empty and "close" in d.columns:
                return _norm(d)
        except Exception:  # noqa: BLE001
            time.sleep(1.2)
    return None


def _save_cache(name, df):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_csv(os.path.join(CACHE_DIR, f"{name}.csv"), index=False)
    except Exception:  # noqa: BLE001
        pass


def _read_cache(name):
    p = os.path.join(CACHE_DIR, f"{name}.csv")
    if os.path.exists(p):
        try:
            return _norm(pd.read_csv(p))
        except Exception:  # noqa: BLE001
            return None
    return None


# ── westock（腾讯自选股）行情：实测更稳更全，作为日线【首选源】（东财/新浪/缓存为后备）──
# 取到的是新鲜实时前复权价 → source="westock"，按"完整"对待、不触发缓存禁令。
# 性能：main() 先调 prefetch_westock() 一次性批量取所有 code（输出含 symbol 列的单表），
# 之后 fetch_hist 的 westock 源直接命中 _WESTOCK_HIST，避免逐只 npx。
WESTOCK_PKG = "westock-data-skillhub@1.0.3"
_WESTOCK_HIST = {}   # bare_code -> DataFrame[date,close(,amount)]；由 prefetch_westock 批量填充


def _westock_symbol(code):
    """裸代码 → 带市场前缀（5/6 开头=沪市 sh，其余=深市 sz，如 159915→sz159915）。"""
    return ("sh" if str(code)[:1] in ("5", "6") else "sz") + str(code)


def _parse_westock_kline(md):
    """解析 westock `kline` 的 Markdown 表为 DataFrame[date, close(, amount)]（close 取 last 列、amount 取成交额列）。失败返回 None。"""
    if not md:
        return None
    lines = [ln for ln in md.splitlines() if ln.strip().startswith("|")]
    header, body = None, None
    for i in range(len(lines) - 1):
        sep = lines[i + 1].replace("|", "").replace(" ", "")
        if sep and set(sep) <= set("-"):
            header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            body = lines[i + 2:]
            break
    if not header or "date" not in header or "last" not in header or not body:
        return None
    di, ci = header.index("date"), header.index("last")
    ai = header.index("amount") if "amount" in header else None
    rows = []
    for ln in body:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) == len(header):
            row = {"date": cells[di], "close": cells[ci]}
            if ai is not None:
                row["amount"] = cells[ai]
            rows.append(row)
    if not rows:
        return None
    try:
        return _norm(pd.DataFrame(rows))
    except Exception:  # noqa: BLE001
        return None


def _parse_westock_kline_batch(md):
    """解析批量 kline 输出（含 symbol 列的单表）为 {bare_code: DataFrame[date,close(,amount)]}。"""
    if not md:
        return {}
    lines = [ln for ln in md.splitlines() if ln.strip().startswith("|")]
    header, body = None, None
    for i in range(len(lines) - 1):
        sep = lines[i + 1].replace("|", "").replace(" ", "")
        if sep and set(sep) <= set("-"):
            header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            body = lines[i + 2:]
            break
    if not header or not body or not {"symbol", "date", "last"} <= set(header):
        return {}
    si, di, ci = header.index("symbol"), header.index("date"), header.index("last")
    ai = header.index("amount") if "amount" in header else None
    grouped = {}
    for ln in body:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) != len(header):
            continue
        sym = cells[si]
        bare = sym[2:] if sym[:2] in ("sh", "sz") else sym
        row = {"date": cells[di], "close": cells[ci]}
        if ai is not None:
            row["amount"] = cells[ai]
        grouped.setdefault(bare, []).append(row)
    out = {}
    for bare, rows in grouped.items():
        try:
            df = _norm(pd.DataFrame(rows))
            if not df.empty:
                out[bare] = df
        except Exception:  # noqa: BLE001
            pass
    return out


def prefetch_westock(codes, limit=320):
    """一次性批量取 westock 日线，填入 _WESTOCK_HIST（bare_code->df），供 fetch_hist 命中。

    npx 不可用/失败/超时则不填充（fetch_hist 会自然回退到东财/新浪/缓存）。
    """
    codes = [str(c) for c in codes if c]
    exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not codes or not exe:
        return
    syms = ",".join(_westock_symbol(c) for c in codes)
    try:
        r = subprocess.run(
            [exe, "-y", WESTOCK_PKG, "kline", syms, "--period", "day", "--limit", str(limit), "--fq", "qfq"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r.returncode == 0:
            _WESTOCK_HIST.update(_parse_westock_kline_batch(r.stdout))
    except Exception:  # noqa: BLE001
        pass


def _try_westock(code, limit=320):
    """取 westock 日线前复权价：优先用 prefetch 批量结果；未预取则单只取一次。失败返回 None。"""
    bare = str(code)
    if bare in _WESTOCK_HIST:
        df = _WESTOCK_HIST[bare]
        return df if df is not None and not df.empty else None
    exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "-y", WESTOCK_PKG, "kline", _westock_symbol(code),
             "--period", "day", "--limit", str(limit), "--fq", "qfq"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r.returncode == 0:
            return _parse_westock_kline(r.stdout)
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_hist(code, retries=2):
    """多源取日终价格。返回 (DataFrame[date,close], source)；source ∈ {'westock','live','cache',None}。

    顺序：westock(腾讯自选股, qfq, 首选) → 东财(qfq) → 新浪 → 本地缓存。
    westock 取到的是新鲜实时价，按"完整"对待（不计入 used_cache、不触发缓存交易禁令）。
    """
    df = _try_westock(code)
    if df is not None and not df.empty:
        _save_cache(code, df)
        return df, "westock"
    df = _try_em(code, retries)
    if df is not None:
        _save_cache(code, df)
        return df, "live"
    df = _try_sina(code, retries)
    if df is not None:
        _save_cache(code, df)
        return df, "live"
    df = _read_cache(code)
    if df is not None:
        return df, "cache"
    print(f"  [警告] {code} 所有数据源失败且无缓存", file=sys.stderr)
    return None, None


def fetch_valuation_pct(index_name, lookback_years, retries=3):
    """估值分位（滚动市盈率），失败回退缓存。返回 (result|None, status)。

    status: {available, source('live'/'cache'/'cache_stale'/None), as_of, stale_days, reason?}
    """
    cache_path = os.path.join(CACHE_DIR, f"valuation_{index_name}.json")
    today = date.today()
    for _ in range(retries):
        try:
            df = ak.stock_index_pe_lg(symbol=index_name)
            if df is not None and not df.empty and "滚动市盈率" in df.columns:
                s = pd.to_numeric(df["滚动市盈率"], errors="coerce").dropna()
                if len(s) >= 30:
                    s2 = s.tail(int(lookback_years * 244))
                    cur = float(s2.iloc[-1])
                    pct = float((s2 < cur).mean())
                    as_of = str(pd.to_datetime(df["日期"].iloc[-1]).date())
                    res = {"pe": round(cur, 2), "percentile": round(pct, 3), "as_of": as_of}
                    try:
                        os.makedirs(CACHE_DIR, exist_ok=True)
                        with open(cache_path, "w", encoding="utf-8") as f:
                            json.dump({**res, "fetched_at": str(today)}, f, ensure_ascii=False)
                    except Exception:  # noqa: BLE001
                        pass
                    return res, {"available": True, "source": "live", "as_of": as_of, "stale_days": 0}
            return None, {"available": False, "source": None, "reason": "bad_response"}
        except Exception:  # noqa: BLE001
            time.sleep(1.2)
    # 回退缓存
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                c = json.load(f)
            fa = c.get("fetched_at")
            stale = (today - datetime.strptime(fa, "%Y-%m-%d").date()).days if fa else 999
            if stale <= VAL_STALE_LIMIT_DAYS:
                return ({"pe": c["pe"], "percentile": c["percentile"], "as_of": c.get("as_of")},
                        {"available": True, "source": "cache", "as_of": c.get("as_of"), "stale_days": stale})
            return None, {"available": False, "source": "cache_stale", "reason": "cache_too_old", "stale_days": stale}
        except Exception:  # noqa: BLE001
            pass
    return None, {"available": False, "source": None, "reason": "network_failed"}


def grade_data(missing, provenance):
    max_stale = max((p["stale_days"] for p in provenance.values()), default=0)
    if missing:
        return "部分缺失", False, max_stale
    if max_stale > STALE_LIMIT_DAYS:
        return "过旧", False, max_stale
    if any(p["source"] == "cache" for p in provenance.values()):
        return "缓存可用", True, max_stale
    return "完整", True, max_stale


def floor_to_lot(amount, price, lot_size=100):
    if amount <= 0 or price <= 0:
        return 0
    return int(amount // (price * lot_size)) * lot_size


def build_first_funding_schedule(holdings, prices, cash, first_pct, max_weekly, min_trade):
    """0持仓账户的多周分批建仓草案。后续周次必须复盘后再执行。"""
    if cash <= 0 or first_pct <= 0:
        return []
    weeks = max(4, min(8, int((1 / first_pct) + 0.999)))
    weekly_cap = cash * first_pct
    if max_weekly > 0:
        weekly_cap = min(weekly_cap, max_weekly)
    schedule = []
    remaining_cash = cash
    for week in range(1, weeks + 1):
        planned = min(weekly_cap, remaining_cash)
        if planned <= 0:
            break
        orders, actual = [], 0.0
        for h in holdings:
            code = str(h["code"])
            price = prices.get(code)
            tw = float(h.get("target_weight", 0) or 0)
            target_amount = planned * tw
            shares = floor_to_lot(target_amount, price or 0)
            amount = shares * price if price else 0.0
            reasons = []
            if target_amount < min_trade:
                reasons.append(f"目标金额低于最小交易门槛 {min_trade:.0f} 元")
            if shares <= 0 and target_amount > 0:
                reasons.append("不足一手，暂不下单")
            actual += amount
            orders.append({
                "code": code,
                "name": h.get("name", code),
                "target_weight": round(tw, 4),
                "target_amount": round(target_amount, 0),
                "estimated_shares": shares,
                "estimated_amount": round(amount, 0),
                "blocked_reasons": reasons,
            })
        schedule.append({
            "week": week,
            "planned_amount": round(planned, 0),
            "estimated_amount": round(actual, 0),
            "estimated_unallocated": round(max(planned - actual, 0), 0),
            "orders": orders,
            "status": "ready" if week == 1 else "requires_prior_review",
            "notes": ["第1周可作为试仓预览；后续周次必须先完成上周复盘，不自动执行"],
        })
        remaining_cash -= planned
    return schedule


def build_preflight_checks(grade, rebal_ok, used_cache, allow_cache_trade, holdings, per, min_trade, max_weekly,
                           is_zero_position, risk_budget_breached=False, target_stress_drawdown=0, max_drawdown=0):
    checks = []
    checks.append({
        "id": "data_quality",
        "label": "数据质量",
        "status": "pass" if rebal_ok else "block",
        "message": f"当前数据质量：{grade}" if rebal_ok else f"当前数据质量：{grade}，禁止交易动作",
    })
    cache_block = used_cache and not allow_cache_trade
    checks.append({
        "id": "cache_policy",
        "label": "缓存行情",
        "status": "block" if cache_block else "pass",
        "message": "包含缓存行情，当前规则禁止据此交易" if cache_block else "未触发缓存交易禁令",
    })
    valuation_missing = []
    valuation_rich = []
    for h in holdings:
        code = str(h["code"])
        s = per.get(code) or {}
        if s.get("asset") in VALUATION_APPLICABLE_ASSETS:
            if s.get("valuation_missing"):
                valuation_missing.append(h.get("name", code))
            elif (s.get("valuation") or {}).get("tag") == "rich":
                valuation_rich.append(h.get("name", code))
    if valuation_missing:
        checks.append({
            "id": "valuation_missing",
            "label": "估值覆盖",
            "status": "warn",
            "message": "权益类估值缺失：" + "、".join(valuation_missing) + "；需要额外确认，不能当作中性",
        })
    elif valuation_rich:
        checks.append({
            "id": "valuation_rich",
            "label": "估值位置",
            "status": "warn",
            "message": "权益类估值偏贵：" + "、".join(valuation_rich) + "；首次建仓应保持小额分批",
        })
    else:
        checks.append({"id": "valuation", "label": "估值检查", "status": "pass", "message": "未发现权益估值缺失或偏贵提示"})
    over_weight = [h.get("name", str(h.get("code"))) for h in holdings if float(h.get("target_weight", 0) or 0) > 0.5]
    checks.append({
        "id": "concentration",
        "label": "单品种上限",
        "status": "warn" if over_weight else "pass",
        "message": ("目标权重超过 50%：" + "、".join(over_weight)) if over_weight else "无单个 ETF 目标权重超过 50%",
    })
    checks.append({
        "id": "trade_thresholds",
        "label": "交易门槛",
        "status": "pass",
        "message": f"单笔门槛 {min_trade:.0f} 元；单周上限 {max_weekly:.0f} 元" if max_weekly > 0 else f"单笔门槛 {min_trade:.0f} 元；未设置单周上限",
    })
    checks.append({
        "id": "risk_budget",
        "label": "风险预算",
        "status": "block" if risk_budget_breached else "pass",
        "message": (
            f"目标组合压力回撤约 {target_stress_drawdown * 100:.1f}%，超过可接受回撤 {max_drawdown * 100:.1f}%"
            if risk_budget_breached else
            f"目标组合压力回撤约 {target_stress_drawdown * 100:.1f}%，未超过可接受回撤 {max_drawdown * 100:.1f}%"
        ),
    })
    checks.append({
        "id": "zero_position",
        "label": "0 持仓状态",
        "status": "warn" if is_zero_position else "pass",
        "message": "当前为 0 持仓，只使用首次建仓预览，不直接执行再平衡" if is_zero_position else "非 0 持仓，可按再平衡纪律评估",
    })
    return checks


# 各资产类别的简化假设（单一事实源，战略构建也复用这两张表）：
#   ASSET_SHOCKS = 压力情景冲击（用于回撤估算，非预测）
#   ASSET_EXPECTED_RETURN = 假设长期年化（用于目标可行性体检，非承诺）
ASSET_SHOCKS = {
    "bond": -0.03, "cash": 0.0, "short_bond": -0.02,
    "equity": -0.30, "equity_defensive": -0.20, "gold": -0.15,
    "global_equity": -0.30, "global_growth": -0.40, "china_growth": -0.40,
}
ASSET_EXPECTED_RETURN = {
    "bond": 0.030, "cash": 0.020, "short_bond": 0.025,
    "equity": 0.070, "equity_defensive": 0.055, "gold": 0.020,
    "global_equity": 0.080, "global_growth": 0.100, "china_growth": 0.090,
}
DEFAULT_SHOCK = -0.25
DEFAULT_EXPECTED_RETURN = 0.05

# 估值分位（A股滚动 PE）只对 A 股权益类适用；QDII/黄金/债券/现金/短债没有可比 A股 PE 序列，
# 应如实标"不适用"——既不当缺失（不必额外确认），更不能被当成"估值中性"。
VALUATION_APPLICABLE_ASSETS = ("equity", "equity_defensive", "china_growth")


def load_assumptions(strat):
    """收益/冲击假设的【单一来源】：默认=本模块两张表，strategy.yaml 的 `assumptions` 块逐键覆盖。

    返回 {shocks, returns, default_shock, default_return, meta:{asset:{source,note}}}。
    缺省（无 assumptions 块）即回退到硬编码默认，向后兼容。战略构建也只读这里、不另写一份。
    """
    shocks = dict(ASSET_SHOCKS)
    returns = dict(ASSET_EXPECTED_RETURN)
    default_shock, default_return = DEFAULT_SHOCK, DEFAULT_EXPECTED_RETURN
    meta = {}
    block = (strat or {}).get("assumptions") or {}
    defaults = block.get("defaults") or {}
    if _num_ok(defaults.get("shock")):
        default_shock = float(defaults["shock"])
    if _num_ok(defaults.get("expected_return")):
        default_return = float(defaults["expected_return"])
    for asset, cfg in (block.get("sleeves") or {}).items():
        cfg = cfg if isinstance(cfg, dict) else {}
        if _num_ok(cfg.get("shock")):
            shocks[asset] = float(cfg["shock"])
        if _num_ok(cfg.get("expected_return")):
            returns[asset] = float(cfg["expected_return"])
        m = {}
        if cfg.get("source"):
            m["source"] = str(cfg["source"])
        if cfg.get("note"):
            m["note"] = str(cfg["note"])
        if m:
            meta[asset] = m
    # §9.1 收益区间：每类给 central/conservative/optimistic。优先用 sleeve 显式值，否则按 haircut 从 central 派生。
    haircut = float(defaults["return_haircut"]) if _num_ok(defaults.get("return_haircut")) else 0.03
    returns_conservative, returns_optimistic = {}, {}
    for asset, central in returns.items():
        returns_conservative[asset] = round(central - haircut, 6)
        returns_optimistic[asset] = round(central + haircut, 6)
    for asset, cfg in (block.get("sleeves") or {}).items():
        cfg = cfg if isinstance(cfg, dict) else {}
        if _num_ok(cfg.get("return_conservative")):
            returns_conservative[asset] = float(cfg["return_conservative"])
        if _num_ok(cfg.get("return_optimistic")):
            returns_optimistic[asset] = float(cfg["return_optimistic"])
    return {"shocks": shocks, "returns": returns,
            "default_shock": default_shock, "default_return": default_return, "meta": meta,
            "returns_conservative": returns_conservative, "returns_optimistic": returns_optimistic,
            "return_haircut": haircut,
            "default_return_conservative": round(default_return - haircut, 6),
            "default_return_optimistic": round(default_return + haircut, 6)}


# §9.3 多情景压力：每情景一条完整资产冲击向量（负=损失、正=受益），不用单资产独立冲击相加。
#   数值为据史的示意档，所有者可在 strategy.yaml: stress_scenarios 覆盖并定档严重度。
DEFAULT_STRESS_SCENARIOS = [
    {"name": "全球权益危机", "shocks": {"equity": -0.35, "equity_defensive": -0.25, "china_growth": -0.45,
                                  "global_equity": -0.35, "global_growth": -0.45, "bond": -0.02, "gold": 0.05}},
    {"name": "中国权益危机", "shocks": {"equity": -0.35, "equity_defensive": -0.25, "china_growth": -0.45,
                                  "global_equity": -0.10, "global_growth": -0.12, "bond": 0.01, "gold": 0.03}},
    {"name": "美国科技重估", "shocks": {"equity": -0.08, "equity_defensive": -0.05, "china_growth": -0.15,
                                  "global_equity": -0.20, "global_growth": -0.40, "bond": 0.01, "gold": 0.0}},
    {"name": "利率急升", "shocks": {"equity": -0.10, "equity_defensive": -0.08, "china_growth": -0.20,
                              "global_equity": -0.12, "global_growth": -0.25, "bond": -0.08, "gold": -0.10}},
    {"name": "通胀冲击", "shocks": {"equity": -0.10, "equity_defensive": -0.05, "china_growth": -0.15,
                              "global_equity": -0.10, "global_growth": -0.18, "bond": -0.06, "gold": 0.15}},
    {"name": "人民币升值", "shocks": {"equity": 0.0, "equity_defensive": 0.0, "china_growth": 0.0,
                               "global_equity": -0.10, "global_growth": -0.10, "bond": 0.0, "gold": -0.08}},
    {"name": "QDII额度/溢价冲击", "shocks": {"equity": 0.0, "equity_defensive": 0.0, "china_growth": 0.0,
                                     "global_equity": -0.08, "global_growth": -0.10, "bond": 0.0, "gold": 0.0}},
]


def load_stress_scenarios(strat):
    """多情景压力(§9.3)。strategy.yaml `stress_scenarios`（list of {name, shocks}）覆盖；缺省=内置七情景。

    返回 [{name, shocks:{asset:shock}}]。
    """
    block = (strat or {}).get("stress_scenarios")
    if isinstance(block, list) and block:
        out = []
        for sc in block:
            if isinstance(sc, dict) and isinstance(sc.get("shocks"), dict):
                sh = {str(k): float(v) for k, v in sc["shocks"].items() if _num_ok(v)}
                if sh:
                    out.append({"name": str(sc.get("name") or "情景"), "shocks": sh})
        if out:
            return out
    return [{"name": s["name"], "shocks": dict(s["shocks"])} for s in DEFAULT_STRESS_SCENARIOS]


# §0C #1 历史危机情景（据真实峰谷标定，非拍脑袋）：用于【周度风险预算展示】"若 20XX 重演会怎样"。
#   标定来源：`python engine/backtest.py --stress-scenarios`（据 engine/data/idx_*.csv 种子，2026-06-08）。
#   口径：价格指数峰→谷；锚=各窗口内跌最深的权益代理，全资产用同一对日期算（捕捉债/金在权益低点的真实对冲）。
#   ⚠️ 故意【不】喂给战略构建的接受闸——把 -71% 权益塞进 construct 会逼出极端保守组合（属 §0C #3 的接受判定改造）；
#      这里只做诚实展示，让所有者看见"85% 权益桶在真实 08 级尾部约亏多少"。china_growth 用中证500 代理。
HISTORICAL_CRISIS_SCENARIOS = [
    {"name": "2008金融危机", "window": ["2008-01-15", "2008-11-04"], "anchor": "sh000905",
     "shocks": {"equity": -0.7143, "equity_defensive": -0.7143, "china_growth": -0.7242,
                "global_equity": -0.2717, "global_growth": -0.2637, "bond": 0.0691, "gold": -0.1903,
                "short_bond": 0.0345, "cash": 0.0}},
    {"name": "2015股灾", "window": ["2015-06-12", "2016-01-28"], "anchor": "sh000905",
     "shocks": {"equity": -0.4651, "equity_defensive": -0.4651, "china_growth": -0.5435,
                "global_equity": -0.0959, "global_growth": -0.1078, "bond": 0.0401, "gold": -0.0586,
                "short_bond": 0.02, "cash": 0.0}},
    {"name": "2018贸易战去杠杆", "window": ["2018-01-08", "2018-10-18"], "anchor": "sh000905",
     "shocks": {"equity": -0.2682, "equity_defensive": -0.2682, "china_growth": -0.3766,
                "global_equity": 0.0044, "global_growth": 0.0458, "bond": 0.0382, "gold": -0.0663,
                "short_bond": 0.0191, "cash": 0.0}},
    {"name": "2020疫情闪崩", "window": ["2020-02-19", "2020-03-23"], "anchor": "spx",
     "shocks": {"equity": -0.1286, "equity_defensive": -0.1286, "china_growth": -0.1079,
                "global_equity": -0.3392, "global_growth": -0.3012, "bond": 0.013, "gold": -0.0492,
                "short_bond": 0.0065, "cash": 0.0}},
    {"name": "2022加息回调", "window": ["2021-12-27", "2022-12-28"], "anchor": "ixic",
     "shocks": {"equity": -0.213, "equity_defensive": -0.213, "china_growth": -0.194,
                "global_equity": -0.2104, "global_growth": -0.3565, "bond": 0.037, "gold": 0.0025,
                "short_bond": 0.0185, "cash": 0.0}},
]


def load_historical_scenarios(strat):
    """历史危机情景（§0C #1）。strategy.yaml `historical_stress_scenarios` 覆盖；缺省=内置标定档。

    返回 [{name, window?, anchor?, shocks:{asset:shock}}]。
    """
    block = (strat or {}).get("historical_stress_scenarios")
    if isinstance(block, list) and block:
        out = []
        for sc in block:
            if isinstance(sc, dict) and isinstance(sc.get("shocks"), dict):
                sh = {str(k): float(v) for k, v in sc["shocks"].items() if _num_ok(v)}
                if sh:
                    out.append({"name": str(sc.get("name") or "情景"), "shocks": sh,
                                "window": sc.get("window"), "anchor": sc.get("anchor")})
        if out:
            return out
    return [dict(s) for s in HISTORICAL_CRISIS_SCENARIOS]


def estimate_stress_scenarios(holdings, universe, scenarios, default_shock=None):
    """多情景压力（§0C #1）：每情景算 ETF 桶【净】损失（同情景内对冲资产的受益抵损 → 体现真实分散）。

    每情景 loss = Σ 权重×冲击；ETF 桶回撤 = max(0, -loss)。返回 (按回撤降序的列表, 最坏情景|None)。
    纯函数、同输入同输出；不预测，仅"若该情景重演的目标组合损益"。
    """
    default_shock = DEFAULT_SHOCK if default_shock is None else default_shock
    results = []
    for sc in (scenarios or []):
        shocks = sc.get("shocks") or {}
        net = 0.0
        contributions = []
        for h in holdings:
            code = str(h.get("code"))
            tw = float(h.get("target_weight", 0) or 0)
            asset = (universe.get(code) or {}).get("asset")
            shock = shocks.get(asset, default_shock)
            net += tw * shock
            contributions.append({"code": code, "name": h.get("name", code), "asset": asset,
                                  "target_weight": round(tw, 4), "shock": round(shock, 4),
                                  "contribution": round(tw * shock, 4)})
        results.append({
            "name": sc.get("name") or "情景", "window": sc.get("window"), "anchor": sc.get("anchor"),
            "etf_drawdown": round(max(0.0, -net), 4), "net": round(net, 4),
            "contributions": contributions,
        })
    results.sort(key=lambda r: r["etf_drawdown"], reverse=True)
    return results, (results[0] if results else None)


# §0C #4 趋势过滤回撤保护（据长面板"趋势过滤 vs 静态"标定，非拍脑袋）：
#   量化"权益跌破 MA200 不动手会多扛多少回撤"。来源：`python engine/backtest.py --trend-benefit`（2026-06-08）。
#   ⚠️ 样本内、线上不自动执行——只把回测里趋势过滤的好处明牌给所有者，由人确认减仓。
TREND_PROTECTION_BENEFIT = {
    "static_maxdd": -0.4227, "trend_maxdd": -0.2114, "delta_pp": 21.1,
    "trend_cagr": 0.1174, "static_cagr": 0.1147, "years": 21.1, "start": "2005-12-20", "end": "2026-06-05",
}


def load_trend_protection(strat):
    """趋势过滤回撤保护标定（§0C #4）。strategy.yaml `trend_protection_benefit` 覆盖；缺省=内置档。"""
    block = (strat or {}).get("trend_protection_benefit")
    if isinstance(block, dict) and _num_ok(block.get("delta_pp")):
        return {**TREND_PROTECTION_BENEFIT, **{k: v for k, v in block.items() if _num_ok(v) or isinstance(v, str)}}
    return dict(TREND_PROTECTION_BENEFIT)


def build_trend_derisk(per, holdings, universe, mkt_vals, equity_assets, ma_days, look, *, min_trade, benefit):
    """§0C #4：权益跌破 MA200 → 具体减仓建议（移到债券/防御），带回测量化的回撤差。人确认、不自动下单。

    纯函数。返回 [{code,name,asset,suggest:'derisk',derisk_amount,reserve_code,reserve_name,actionable,blocked_reasons,...}]。
    减仓金额=该品种当前市值（与回测"跌破即移出全部到债券"一致）；reserve=universe 里的 asset:bond。
    """
    reserve_code = next((str(h["code"]) for h in holdings
                         if (universe.get(str(h["code"])) or {}).get("asset") == "bond"), None)
    reserve_name = (universe.get(reserve_code) or {}).get("name") if reserve_code else None
    out = []
    for c, s in per.items():
        if not (isinstance(s, dict) and s.get("asset") in equity_assets and s.get("trend") == "below"):
            continue
        value = float(mkt_vals.get(c, 0) or 0)
        amount = round(value, 0)
        blocked = []
        if reserve_code is None:
            blocked.append("universe 无国债/防御标的可移入")
        elif reserve_code == c:
            blocked.append("该品种即防御标的，无需移仓")
        if amount < min_trade:
            blocked.append(f"持仓市值 {amount:.0f} 低于最小交易门槛 {min_trade:.0f} 元")
        out.append({
            "code": c, "name": s.get("name", c), "asset": s.get("asset"),
            "momentum": s.get(f"momentum_{look}d"), f"ma{ma_days}": s.get(f"ma{ma_days}"), "last": s.get("last"),
            "suggest": "derisk", "reserve_code": reserve_code, "reserve_name": reserve_name,
            "derisk_amount": amount, "actionable": not blocked, "blocked_reasons": blocked,
        })
    return out


def resolve_policy_number(profile, key, default, *, lo=None, hi=None):
    """Track C §5.2 权威「零值/缺失/非法」规则——绝不用 `v or default` 吞掉合法 0。

    返回 (value, status)：
      - 字段缺失 / None        → (default, "defaulted")
      - 合法数值（含 0）        → (v,       "ok")
      - 布尔/非数 / 越界        → (default, "invalid")   ← 不静默修正，交调用方记诊断
    """
    if key not in profile or profile.get(key) is None:
        return float(default), "defaulted"
    v = profile.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return float(default), "invalid"
    v = float(v)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return float(default), "invalid"
    return v, "ok"


def estimate_target_stress_drawdown(holdings, universe, shocks=None, default_shock=None):
    """按目标权重做简化压力测试；用于风险预算校准，不是预测。

    shocks/default_shock 缺省回退到模块表（保持旧调用与测试不变）；传入时用注入的假设（WS4 单一来源）。
    """
    shocks = ASSET_SHOCKS if shocks is None else shocks
    default_shock = DEFAULT_SHOCK if default_shock is None else default_shock
    contributions = []
    total = 0.0
    for h in holdings:
        code = str(h.get("code"))
        tw = float(h.get("target_weight", 0) or 0)
        asset = (universe.get(code) or {}).get("asset")
        shock = shocks.get(asset, default_shock)
        contribution = tw * shock
        total += contribution
        contributions.append({
            "code": code,
            "name": h.get("name", code),
            "asset": asset,
            "target_weight": round(tw, 4),
            "shock": round(shock, 4),
            "contribution": round(contribution, 4),
        })
    return abs(total), contributions


def expected_etf_return(holdings, universe, returns=None, default_return=None):
    """按目标权重 × 各 sleeve 假设年化，估 ETF 桶现实预期年化（非承诺，仅目标可行性刻度）。

    returns/default_return 缺省回退到模块表（保持旧调用与测试不变）；传入时用注入的假设（WS4 单一来源）。
    """
    returns = ASSET_EXPECTED_RETURN if returns is None else returns
    default_return = DEFAULT_EXPECTED_RETURN if default_return is None else default_return
    total = 0.0
    for h in holdings:
        code = str(h.get("code"))
        tw = float(h.get("target_weight", 0) or 0)
        asset = (universe.get(code) or {}).get("asset")
        total += tw * returns.get(asset, default_return)
    return total


def whole_portfolio_stress(etf_stress_drawdown, etf_value, stable_outside):
    """把 ETF 桶的压力回撤折算到全组合（场外稳健桶按 0 冲击纳入分母，是安全垫）。

    whole_dd = etf_dd × etf_value / (etf_value + stable_outside)。
    稳健桶为 0 时退化为 ETF 桶自身口径。
    """
    whole = etf_value + max(0.0, float(stable_outside or 0))
    if whole <= 0:
        return etf_stress_drawdown
    return etf_stress_drawdown * etf_value / whole


# ── WS1：本周每只持仓 ETF 的「加仓/减仓/不动」理由（后端确定性纯函数，可测试、可复现、归档可重渲染）──

def _momentum_bucket(m):
    if m is None:
        return None
    if m >= 0.05:
        return "偏强"
    if m <= -0.05:
        return "偏弱"
    return "中性"


def _signal_momentum(signal):
    k = next((k for k in signal if isinstance(k, str) and k.startswith("momentum_")), None)
    return signal.get(k) if k else None


def valuation_state(signal):
    """估值三态（+无）：cheap/neutral/rich（有分位）| na（不适用）| missing（缺失·非中性）| None（无估值字段）。"""
    if signal.get("valuation"):
        return (signal["valuation"] or {}).get("tag")
    if signal.get("valuation_na"):
        return "na"
    if "valuation_missing" in signal:
        return "missing"
    return None


def _signal_qualifiers(signal):
    """趋势/动量/估值三态的人话限定语（确定性、可复用）。估值严格区分三态，缺失绝不写'中性'。"""
    parts = []
    trend = signal.get("trend")
    if trend == "above":
        parts.append("价在 MA200 上方")
    elif trend == "below":
        parts.append("已跌破 MA200（趋势转弱，危机保险信号）")
    mb = _momentum_bucket(_signal_momentum(signal))
    if mb:
        parts.append(f"动量{mb}")
    vs = valuation_state(signal)
    if vs in ("cheap", "neutral", "rich"):
        pct = ((signal.get("valuation") or {}).get("percentile") or 0) * 100
        parts.append(f"估值分位{pct:.0f}%（{ {'cheap': '偏便宜', 'neutral': '估值中性', 'rich': '偏贵'}[vs] }）")
    elif vs == "na":
        parts.append("估值分位不适用（QDII/黄金/债券类）")
    elif vs == "missing":
        parts.append("估值数据缺失(非中性)")
    return parts


def explain_rebalance_action(row, signal, *, abs_thr_pp, rel_thr, min_trade, max_weekly):
    """为单只持仓 ETF 的 加仓/减仓/不动 生成人话理由 + 结构化 reason_factors。

    优先级：数据错误 > 被拦截 > 触发(add/trim) > 不动(hold)。error 行绝不使用买卖措辞。
    返回 (reason_str, reason_factors)。row=actionable_rebalance 行；signal=per[code]。纯函数、同输入同输出。
    """
    signal = signal or {}
    suggest = row.get("suggest")
    triggered = bool(row.get("triggered"))
    actionable = bool(row.get("actionable"))
    dev = float(row.get("deviation_pp") or 0)
    amount = row.get("approx_amount") or 0
    blockers = list(row.get("blocked_reasons") or [])
    vs = valuation_state(signal)
    factors = {
        "deviation_pp": round(dev, 2), "threshold_pp": abs_thr_pp, "rel_threshold": rel_thr,
        "suggest": suggest, "triggered": triggered, "actionable": actionable,
        "trend": signal.get("trend"), "momentum_bucket": _momentum_bucket(_signal_momentum(signal)),
        "valuation_state": vs, "blockers": blockers, "exec_quality": "none", "valuation_decel": False,
    }
    if signal.get("error"):
        factors["state"] = "no_data"
        return f"数据不足/拉取失败，本周不评估（{signal.get('error')}）", factors
    quals = _signal_qualifiers(signal)
    qual_txt = ("；" + "、".join(quals)) if quals else ""
    verb = {"add": "加仓", "trim": "减仓"}.get(suggest, "")
    if triggered and not actionable:
        why = "；".join(blockers) if blockers else "未通过执行门槛"
        return f"原信号建议{verb}（偏离 {dev:+.1f}pp），但被拦截：{why}{qual_txt}", factors
    if triggered and suggest in ("add", "trim"):
        base = f"偏离目标 {dev:+.1f}pp（超过阈值 {abs_thr_pp:.0f}pp 或相对 {rel_thr:.0%}）→ 建议{verb}约 ¥{amount:,.0f}"
        extra = ""
        if suggest == "add" and vs == "rich":
            pct = ((signal.get("valuation") or {}).get("percentile") or 0) * 100
            extra = f"；估值分位偏高（{pct:.0f}%），建议缓建/小额、分批靠近目标"
            factors["valuation_decel"] = True
        return base + qual_txt + extra, factors
    return f"未超过 5/25 阈值（偏离 {dev:+.1f}pp），维持当前仓位{qual_txt}", factors


def decelerate_add(row, signal, risk_profile):
    """WS5：对 rich 估值的触发加仓做有界软化（给出更小的【建议执行规模】）。

    只缩不放、仅 add、仅 valuation==rich；估值 na/missing/neutral/cheap 绝不软化（需真实分位）。
    就地给 row 加 action_mode='缓建' / soften_amount；**不改 approx_amount/deviation_pp、不翻转 actionable、不加 blocked**
    （它是元数据，不是闸门，不会破坏一笔合法再平衡）。返回 row。
    """
    if row.get("suggest") != "add" or not row.get("triggered"):
        return row
    if valuation_state(signal) != "rich":
        return row
    pct = (signal.get("valuation") or {}).get("percentile") or 0
    base, extreme = {"进取": (0.50, 0.33), "平衡": (0.40, 0.25), "保守": (0.30, 0.20)}.get(risk_profile, (0.40, 0.25))
    factor = extreme if pct >= 0.90 else base
    row["action_mode"] = "缓建"
    row["soften_amount"] = round((row.get("approx_amount") or 0) * factor)
    return row


# 再平衡频率 → 两次再平衡的最短间隔天数（含约 1 天容差，避免略早跑的周报被卡）。
REBAL_FREQ_DAYS = {"weekly": 0, "biweekly": 13, "monthly": 28, "quarterly": 84}
REBAL_FREQ_ZH = {"weekly": "每周", "biweekly": "每两周", "monthly": "每月", "quarterly": "每季"}


def latest_execution_date(repo_root):
    """journal/executions 里最近一笔成交的日期（按文件名 YYYY-MM-DD_ 前缀解析）；无则 None。纯读取、不联网。"""
    if not repo_root:
        return None
    d = os.path.join(repo_root, "journal", "executions")
    if not os.path.isdir(d):
        return None
    best = None
    for fn in os.listdir(d):
        if not fn.endswith(".json") or len(fn) < 10:
            continue
        try:
            dt = datetime.strptime(fn[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if best is None or dt > best:
            best = dt
    return best


def frequency_gate_state(check_freq, last_exec_date, today):
    """再平衡频率闸状态（纯函数）：返回 (min_gap_days, days_since, freq_gated)。

    weekly→min_gap 0（不额外限制）；其它档要求"距上次成交 ≥ min_gap 天"才再平衡。
    无上次成交记录→不闸（days_since=None）。
    """
    min_gap = REBAL_FREQ_DAYS.get(str(check_freq).lower(), 0)
    days_since = (today - last_exec_date).days if last_exec_date else None
    gated = bool(min_gap > 0 and days_since is not None and days_since < min_gap)
    return min_gap, days_since, gated


def main():
    ap = argparse.ArgumentParser(description="周度信号引擎")
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--portfolio", default=None)
    ap.add_argument("--out", default=os.path.join(HERE, "signals.json"))
    args = ap.parse_args()

    repo_root = find_repo_root(HERE)
    strategy_path = args.strategy or (os.path.join(repo_root, "strategy.yaml") if repo_root else None)
    portfolio_path = args.portfolio or (os.path.join(repo_root, "portfolio.yaml") if repo_root else None)
    if not strategy_path or not os.path.exists(strategy_path):
        die("找不到 strategy.yaml，请用 --strategy 指定路径")
    if not portfolio_path or not os.path.exists(portfolio_path):
        die("找不到 portfolio.yaml，请用 --portfolio 指定路径")

    strat = load_yaml(strategy_path)
    port = load_yaml(portfolio_path)
    investor_profile = load_investor_profile(repo_root)

    errs = validate_strategy(strat) + validate_config(port, strat)
    if errs:
        die("配置校验未通过，请先修正 strategy.yaml / portfolio.yaml：\n  - " + "\n  - ".join(errs))

    assumptions = load_assumptions(strat)   # WS4：收益/冲击假设单一来源（含 strategy.yaml 覆盖）
    F = strat["factors"]
    uni = {str(u["code"]): u for u in strat["universe"]}
    ma_days = int(F["trend_filter"]["ma_days"])
    look = int(F["momentum"]["lookback_days"])
    vyears = float(F["valuation"]["lookback_years"])
    cheap = float(F["valuation"]["cheap_pct"])
    rich = float(F["valuation"]["rich_pct"])
    abs_thr = float(F["rebalance"]["abs_threshold_pp"]) / 100.0
    rel_thr = float(F["rebalance"]["rel_threshold"])
    check_freq = str((F["rebalance"]).get("check_frequency", "weekly")).lower()
    if check_freq not in REBAL_FREQ_DAYS:
        check_freq = "weekly"
    breaker_thr = float((F["rebalance"]).get("circuit_breaker_pp", 15)) / 100.0
    RC = strat.get("risk_controls") or {}
    min_trade = float(RC.get("min_trade_amount", 0) or 0)
    max_weekly = float(RC.get("max_weekly_trade_amount", 0) or 0)
    first_pct = float(RC.get("first_tranche_pct", 0) or 0)
    allow_cache_trade = bool(RC.get("allow_trade_with_cache", False))

    holdings = port.get("holdings", []) or []
    watchlist = strat.get("watchlist") or []
    cash = float(port.get("cash", 0) or 0)
    today = date.today()

    # westock 为首选源：先一次性批量预取所有 holding+watchlist 的日线，fetch_hist 即命中、避免逐只 npx
    prefetch_westock([str(h.get("code")) for h in holdings] + [str(w.get("code")) for w in watchlist])

    def build_signal(item, fallback=None):
        """生成单只 ETF 的展示信号；不包含仓位/交易动作。"""
        code = str(item["code"])
        meta = fallback or item
        name = item.get("name") or meta.get("name") or code
        df, src = fetch_hist(code)
        if df is None or len(df) < ma_days + 5:
            sig = {
                "name": name,
                "asset": meta.get("asset"),
                "role": item.get("role"),
                "note": item.get("note"),
                "error": "数据不足或拉取失败",
            }
            return code, sig, None, None, None, None
        close = df["close"]
        last = float(close.iloc[-1])
        as_of = df["date"].iloc[-1].date()
        ma = float(close.tail(ma_days).mean())
        mom = float(close.iloc[-1] / close.iloc[-1 - look] - 1) if len(close) > look else None
        sig = {
            "name": name,
            "asset": meta.get("asset"),
            "role": item.get("role"),
            "note": item.get("note"),
            "last": round(last, 4),
            "as_of": str(as_of),
            "source": src,
            f"ma{ma_days}": round(ma, 4),
            "trend": "above" if last >= ma else "below",
            f"momentum_{look}d": round(mom, 4) if mom is not None else None,
        }
        vst = None
        asset = meta.get("asset")
        if asset not in VALUATION_APPLICABLE_ASSETS:
            # QDII/黄金/债券/现金等：A股 PE 分位不适用，如实标注（非缺失、更非中性）
            sig["valuation_na"] = True
        elif F["valuation"]["enabled"] and meta.get("index"):
            v, vst = fetch_valuation_pct(meta["index"], vyears)
            if v:
                tag = "cheap" if v["percentile"] <= cheap else (
                    "rich" if v["percentile"] >= rich else "neutral")
                sig["valuation"] = {**v, "tag": tag}
            else:
                sig["valuation_missing"] = vst
        else:
            # A股权益但尚未接入可用估值源（如红利低波/创业板/科创50）→ 如实标缺失，绝不当中性
            vst = {"available": False, "source": None, "reason": "index_not_configured"}
            sig["valuation_missing"] = vst
        prov = {"source": src, "as_of": str(as_of), "stale_days": (today - as_of).days}
        return code, sig, last, prov, vst, close.tolist()

    def as_of_summary_from(provenance_map):
        as_ofs = sorted(p["as_of"] for p in provenance_map.values())
        if not as_ofs:
            return None, None, "无"
        as_of_min = as_ofs[0]
        as_of_max = as_ofs[-1]
        summary = as_of_min if as_of_min == as_of_max else f"{as_of_min} 至 {as_of_max}"
        return as_of_min, as_of_max, summary

    per, prices, provenance, valuation_status, closes_by_code = {}, {}, {}, {}, {}
    for h in holdings:
        code = str(h["code"])
        sig_code, sig, last, prov, vst, closes = build_signal(h, uni.get(code, {}))
        if last is not None:
            prices[sig_code] = last
            provenance[sig_code] = prov
        if closes:
            closes_by_code[sig_code] = closes
        if vst is not None:
            valuation_status[sig_code] = vst
        per[code] = sig

    missing = [str(h["code"]) for h in holdings if str(h["code"]) not in prices]
    grade, rebal_ok, max_stale = grade_data(missing, provenance)
    used_cache = any(p["source"] == "cache" for p in provenance.values())
    as_of_min, as_of_max, as_of_summary = as_of_summary_from(provenance)

    watch_signals, watch_prices, watch_provenance = {}, {}, {}
    for w in watchlist:
        code, sig, last, prov, vst, _ = build_signal(w)
        watch_signals[code] = sig
        if last is not None:
            watch_prices[code] = last
            watch_provenance[code] = prov
        if vst is not None:
            valuation_status[code] = vst
    watch_missing = [str(w["code"]) for w in watchlist if str(w["code"]) not in watch_prices]
    watch_grade, _, watch_max_stale = grade_data(watch_missing, watch_provenance)
    watch_as_of_min, watch_as_of_max, watch_as_of_summary = as_of_summary_from(watch_provenance)

    mkt_vals = {c: float(next(h for h in holdings if str(h["code"]) == c).get("shares", 0) or 0) * prices[c]
                for c in prices}
    total = cash + sum(mkt_vals.values())
    invested_value = sum(mkt_vals.values())
    is_zero_position = invested_value <= 0
    first_funding_eligible = is_zero_position and cash > 0
    target_stress_drawdown, stress_contributions = estimate_target_stress_drawdown(
        holdings, uni, assumptions["shocks"], assumptions["default_shock"])
    max_acceptable_drawdown, _mdd_status = resolve_policy_number(
        investor_profile, "max_acceptable_drawdown", 0.15, lo=0.0, hi=0.80)   # Track C §5.2：合法 0% 保留
    # 全组合口径：场外稳健桶按 0 冲击纳入分母，压力回撤折算到整个组合（稳健桶是安全垫）。
    stable_outside = float(investor_profile.get("stable_assets_outside", 0) or 0)
    whole_portfolio_value = total + stable_outside
    whole_portfolio_stress_drawdown = whole_portfolio_stress(target_stress_drawdown, total, stable_outside)
    # 风险预算闸门按"全组合"压力回撤评估，而非只看 ETF 桶（否则稳健桶的缓冲被忽略）。
    risk_budget_breached = whole_portfolio_stress_drawdown > max_acceptable_drawdown

    # §0C #1 多情景历史压力：用据真实峰谷标定的危机向量算"若 20XX 重演"的最坏回撤（仅诚实展示、不改硬闸）。
    historical_scenarios = load_historical_scenarios(strat)
    scenario_results, worst_scenario = estimate_stress_scenarios(
        holdings, uni, historical_scenarios, assumptions["default_shock"])
    worst_etf_drawdown = worst_scenario["etf_drawdown"] if worst_scenario else target_stress_drawdown
    whole_worst_scenario_drawdown = whole_portfolio_stress(worst_etf_drawdown, total, stable_outside)
    # 决策相关口径：按"计划满仓"(planned_etf_capital)折算，而非当前实投——少额真金期实投极小，
    #   当前口径会把尾部显示成接近 0、误导"该不该把计划资金投进去"。两个口径都给。
    planned_etf = float(investor_profile.get("planned_etf_capital", 0) or 0)
    whole_worst_at_planned = (whole_portfolio_stress(worst_etf_drawdown, planned_etf, stable_outside)
                              if planned_etf > 0 else whole_worst_scenario_drawdown)
    scenario_budget_breached = whole_worst_at_planned > max_acceptable_drawdown
    worst_scenario_note = (
        f"最坏历史情景「{worst_scenario['name']}」重演 → ETF 桶约 −{worst_etf_drawdown * 100:.0f}%；"
        f"按计划满仓折算全组合约 −{whole_worst_at_planned * 100:.1f}%，"
        f"{'击穿' if scenario_budget_breached else '未击穿'}可接受回撤 {max_acceptable_drawdown * 100:.0f}%"
        "（当前实投占比小，故当前口径尾部更小；此处按计划满仓给决策相关值）"
    ) if worst_scenario else None

    rebal = []
    for h in holdings:
        code = str(h["code"])
        tw = float(h.get("target_weight", 0) or 0)
        cw = (mkt_vals.get(code, 0) / total) if total > 0 else 0.0
        dev = cw - tw
        triggered = (rebal_ok and total > 0
                     and (abs(dev) >= abs_thr or (tw > 0 and abs(dev) / tw >= rel_thr)))
        rebal.append({
            "code": code, "name": h.get("name", code),
            "target_weight": round(tw, 4), "current_weight": round(cw, 4),
            "deviation_pp": round(dev * 100, 2), "triggered": bool(triggered),
            "suggest": ("trim" if dev > 0 else "add") if triggered else "hold",
            "approx_amount": round(abs(dev) * total, 0) if triggered else 0,
        })

    discipline_blockers = []
    if not rebal_ok:
        discipline_blockers.append("数据质量不足，禁止交易动作")
    if used_cache and not allow_cache_trade:
        discipline_blockers.append("行情包含缓存，risk_controls 不允许据此交易")
    if risk_budget_breached:
        discipline_blockers.append(
            f"全组合压力回撤约 {whole_portfolio_stress_drawdown * 100:.1f}%，超过可接受回撤 {max_acceptable_drawdown * 100:.1f}%"
        )
    rebalance_blockers = list(discipline_blockers)
    if first_funding_eligible:
        rebalance_blockers.append("0持仓账户使用首次建仓预览，不直接执行再平衡")
    # 再平衡频率闸：低频档（双周/月/季）要求距上次成交满 min_gap_days 才再平衡；
    # 但任一品种偏离 ≥ circuit_breaker_pp 时（崩盘级漂移）无视频率强制放行（仍受数据/金额/单周上限约束）。
    last_exec_date = latest_execution_date(repo_root)
    min_gap_days, days_since_rebal, freq_gated = frequency_gate_state(check_freq, last_exec_date, today)
    freq_block_reason = (
        f"未到再平衡周期（{REBAL_FREQ_ZH[check_freq]}）：距上次成交 {days_since_rebal} 天 < {min_gap_days} 天，"
        "未达熔断阈值的偏离本次不动手"
    ) if freq_gated else None

    actionable_rebalance = []
    weekly_used = 0.0
    for r in rebal:
        rr = dict(r)
        reasons = []
        breaker_hit = abs(float(r.get("deviation_pp") or 0)) >= breaker_thr * 100 - 1e-9
        if not r["triggered"]:
            reasons.append("未触发再平衡")
        if rebalance_blockers:
            reasons.extend(rebalance_blockers)
        if freq_block_reason and r["triggered"] and not breaker_hit:
            reasons.append(freq_block_reason)
        if r["triggered"] and breaker_hit and freq_gated:
            rr["circuit_breaker"] = True   # 已超熔断阈值，跨频率强制放行
        if r["triggered"] and r["approx_amount"] < min_trade:
            reasons.append(f"金额低于最小交易门槛 {min_trade:.0f} 元")
        if r["triggered"] and max_weekly > 0 and weekly_used + r["approx_amount"] > max_weekly:
            reasons.append(f"超过单周交易上限 {max_weekly:.0f} 元")
        allowed = r["triggered"] and not reasons
        if allowed:
            weekly_used += r["approx_amount"]
        rr["actionable"] = bool(allowed)
        rr["blocked_reasons"] = reasons
        reason_str, reason_factors = explain_rebalance_action(
            rr, per.get(str(r["code"]), {}),
            abs_thr_pp=abs_thr * 100, rel_thr=rel_thr, min_trade=min_trade, max_weekly=max_weekly)
        rr["action_reason"] = reason_str
        rr["reason_factors"] = reason_factors
        decelerate_add(rr, per.get(str(r["code"]), {}), strat.get("risk_profile"))
        actionable_rebalance.append(rr)

    first_deploy = 0.0
    if first_funding_eligible and first_pct > 0:
        first_deploy = cash * first_pct
        if max_weekly > 0:
            first_deploy = min(first_deploy, max_weekly)
    first_orders = []
    first_actual = 0.0
    for h in holdings:
        code = str(h["code"])
        price = prices.get(code)
        tw = float(h.get("target_weight", 0) or 0)
        target_amount = first_deploy * tw
        shares = floor_to_lot(target_amount, price or 0)
        actual_amount = shares * price if price else 0.0
        blocked = []
        if not is_zero_position:
            blocked.append("非 0 持仓账户，不适用首次建仓")
        elif cash <= 0:
            blocked.append("没有可用现金，无法生成首次建仓")
        if discipline_blockers:
            blocked.extend(discipline_blockers)
        if target_amount < min_trade:
            blocked.append(f"目标金额低于最小交易门槛 {min_trade:.0f} 元")
        if shares <= 0 and target_amount > 0:
            blocked.append("不足一手，暂不下单")
        allowed = first_funding_eligible and target_amount >= min_trade and shares > 0 and not discipline_blockers
        if allowed:
            first_actual += actual_amount
        first_orders.append({
            "code": code,
            "name": h.get("name", code),
            "target_weight": round(tw, 4),
            "target_amount": round(target_amount, 0),
            "last": round(price, 4) if price else None,
            "estimated_shares": shares,
            "estimated_amount": round(actual_amount, 0),
            "actionable": bool(allowed),
            "blocked_reasons": blocked,
        })
    first_funding_plan = {
        "is_zero_position": bool(is_zero_position),
        "eligible": bool(first_funding_eligible),
        "cash": round(cash, 2),
        "first_tranche_pct": first_pct,
        "planned_deploy_amount": round(first_deploy, 0),
        "estimated_deploy_amount": round(first_actual, 0),
        "estimated_unallocated": round(max(first_deploy - first_actual, 0), 0),
        "orders": first_orders,
        "notes": [
            "仅用于首次试仓预览，不自动下单",
            "观察池不参与首次建仓",
            "份额按 100 份一手粗略估算，实际以下单页面为准",
        ],
    }
    first_funding_plan["schedule"] = build_first_funding_schedule(
        holdings, prices, cash, first_pct, max_weekly, min_trade
    ) if first_funding_eligible else []

    target_annual_return, _tar_status = resolve_policy_number(
        investor_profile, "target_annual_return", 0.05, lo=0.0, hi=0.30)       # Track C §5.2：合法 0% 保留
    etf_expected_return = expected_etf_return(
        holdings, uni, assumptions["returns"], assumptions["default_return"])  # 当前目标权重的现实预期年化
    risk_budget = {
        "target_annual_return": target_annual_return,
        "target_annual_profit": round(total * target_annual_return, 2),       # 针对 ETF 桶
        "expected_etf_return": round(etf_expected_return, 4),                  # 现实预期年化（非承诺）
        "expected_target_gap": round(target_annual_return - etf_expected_return, 4),
        "max_acceptable_drawdown": max_acceptable_drawdown,                    # 全组合口径
        "max_acceptable_loss": round(whole_portfolio_value * max_acceptable_drawdown, 2),
        # Track C §5.2：策略输入解析状态（ok/defaulted/invalid）——合法 0% 不再被默认值吞掉
        "policy_inputs": {"max_acceptable_drawdown": _mdd_status, "target_annual_return": _tar_status},
        # ETF 桶自身口径（保留，标注；勿与全组合混淆）
        "target_portfolio_stress_drawdown": round(target_stress_drawdown, 4),
        "target_portfolio_stress_loss": round(total * target_stress_drawdown, 2),
        "etf_portfolio_value": round(total, 2),
        # 全组合口径（含场外稳健桶安全垫）
        "stable_assets_outside": round(stable_outside, 2),
        "whole_portfolio_value": round(whole_portfolio_value, 2),
        "whole_portfolio_stress_drawdown": round(whole_portfolio_stress_drawdown, 4),
        "whole_portfolio_stress_loss": round(whole_portfolio_value * whole_portfolio_stress_drawdown, 2),
        "stress_contributions": stress_contributions,
        "breached": bool(risk_budget_breached),
        # §0C #1 历史危机多情景（据真实峰谷标定，仅诚实展示尾部，不改硬闸 `breached`）
        "historical_scenarios": scenario_results,
        "worst_scenario": (worst_scenario or {}).get("name"),
        "worst_scenario_window": (worst_scenario or {}).get("window"),
        "worst_scenario_etf_drawdown": round(worst_etf_drawdown, 4),
        "whole_portfolio_worst_scenario_drawdown": round(whole_worst_scenario_drawdown, 4),
        "whole_portfolio_worst_scenario_loss": round(whole_portfolio_value * whole_worst_scenario_drawdown, 2),
        "whole_portfolio_worst_scenario_drawdown_at_planned": round(whole_worst_at_planned, 4),
        "scenario_breached": bool(scenario_budget_breached),   # 按计划满仓口径
        "worst_scenario_note": worst_scenario_note,
        "stress_losses": [
            {"drawdown": 0.05, "loss": round(whole_portfolio_value * 0.05, 2)},
            {"drawdown": 0.10, "loss": round(whole_portfolio_value * 0.10, 2)},
            {"drawdown": 0.15, "loss": round(whole_portfolio_value * 0.15, 2)},
        ],
        "assessment": (
            "全组合压力回撤超过预算，本周动作降级为只观察"
            if risk_budget_breached else
            "目标需要承担波动风险；首次建仓应分批执行"
            if target_annual_return >= 0.05 and is_zero_position
            else "按风险预算执行"
        ),
    }

    action_discipline = {
        "min_trade_amount": min_trade,
        "max_weekly_trade_amount": max_weekly,
        "first_tranche_pct": first_pct,
        "allow_trade_with_cache": allow_cache_trade,
        "trade_allowed": not discipline_blockers,
        "blocked_reasons": discipline_blockers,
        "rebalance_blocked_reasons": rebalance_blockers,
        "check_frequency": check_freq,
        "rebalance_min_gap_days": min_gap_days,
        "days_since_last_rebalance": days_since_rebal,
        "frequency_gated": bool(freq_gated),
        "circuit_breaker_pp": round(breaker_thr * 100, 2),
        "preflight_checks": build_preflight_checks(
            grade, rebal_ok, used_cache, allow_cache_trade, holdings, per,
            min_trade, max_weekly, is_zero_position,
            risk_budget_breached, whole_portfolio_stress_drawdown, max_acceptable_drawdown
        ),
    }

    equity_assets = ("equity", "equity_defensive", "global_equity", "global_growth", "china_growth")
    eq = [(c, s.get(f"momentum_{look}d")) for c, s in per.items()
          if isinstance(s, dict) and s.get("asset") in equity_assets
          and s.get(f"momentum_{look}d") is not None]
    eq.sort(key=lambda x: x[1], reverse=True)
    momentum_rank = [{"code": c, "name": per[c]["name"], "momentum": v} for c, v in eq]

    # 危机保险（§0C #4 升级）：权益跌破 MA200 → 不再只是提醒，而是**具体减仓建议**（移到债券）+ 回测量化的
    #   回撤差（"不动手会多扛约 X% 回撤"）。仍是建议、人确认、不自动下单——把回测里趋势过滤的好处明牌给所有者。
    trend_protection = load_trend_protection(strat)
    trend_alerts = build_trend_derisk(per, holdings, uni, mkt_vals, equity_assets, ma_days, look,
                                      min_trade=min_trade, benefit=trend_protection)

    # ── Track B Phase A：影子战术建议（只读、绝不进入 actionable_rebalance）──
    tactical_shadow = None
    try:
        tcfg = tactical.load_tactical_config(strat)
        if tcfg.get("reserve_asset") and tcfg.get("mode") in ("shadow", "advisory"):
            stable_o = float(investor_profile.get("stable_assets_outside", 0) or 0)
            planned = float(investor_profile.get("planned_etf_capital", 0) or 0)
            etf_share = planned / (planned + stable_o) if planned > 0 and (planned + stable_o) > 0 else 1.0
            tassets = []
            for h in holdings:
                code = str(h["code"])
                if code not in closes_by_code:
                    continue
                asset = (uni.get(code) or {}).get("asset")
                s = per.get(code) or {}
                tassets.append({
                    "code": code, "asset": asset,
                    "strategic_weight": float(h.get("target_weight", 0) or 0),
                    "closes": closes_by_code[code],
                    "valuation_percentile": (s.get("valuation") or {}).get("percentile"),
                    "valuation_status": valuation_status.get(code),
                    "provenance": provenance.get(code),
                    "shock": assumptions["shocks"].get(asset, assumptions["default_shock"]),
                })
            if len(tassets) >= 2:
                try:
                    import reports as _reports
                    prior_states = _reports.prior_tactical_states()
                except Exception:  # noqa: BLE001
                    prior_states = {}
                tactical_shadow = tactical.compute_shadow(
                    tassets, strat.get("risk_profile") or "平衡", tcfg["reserve_asset"],
                    etf_share=etf_share, max_whole_stress=max_acceptable_drawdown,
                    cfg=tcfg, prior_states=prior_states)
                fp_src = "|".join(f"{a['code']}:{(per.get(a['code']) or {}).get('last')}:"
                                  f"{(per.get(a['code']) or {}).get('as_of')}" for a in tassets)
                tactical_shadow["input_fingerprint"] = "sha256:" + hashlib.sha256(
                    (fp_src + "|model=tactical-v1").encode("utf-8")).hexdigest()[:16]
                tactical_shadow["enabled"] = bool(tcfg.get("enabled"))
                cur_w = {c: (mkt_vals.get(c, 0) / total if total > 0 else 0) for c in mkt_vals}
                tactical_shadow["actions"] = tactical.tactical_actions(
                    tactical_shadow, cur_w, total, min_trade=min_trade, max_weekly=max_weekly,
                    abs_thr_pp=tcfg["actions"]["tactical_abs_threshold_pp"],
                    rel_thr=tcfg["actions"]["tactical_rel_threshold"],
                    struct_abs_pp=abs_thr * 100, struct_rel=rel_thr,
                    strategic_weights={a["code"]: a["strategic_weight"] for a in tassets})
                tactical_shadow["note"] = (
                    "战术动作已接入调仓（advisory）。" if tcfg.get("mode") == "advisory"
                    else "影子战术建议：只读、不构成本周可执行动作；通过 §13.5 验收前不接入调仓。")
    except Exception as _e:  # noqa: BLE001
        tactical_shadow = {"error": f"影子战术计算失败：{_e}"}

    out = {
        "generated_for": str(today),
        "data_quality": grade,
        "rebalance_allowed": rebal_ok,
        "data_complete": grade == "完整",
        "missing_prices": missing,
        "used_cache": used_cache,
        "stale_days_max": max_stale,
        "as_of_min": as_of_min,
        "as_of_max": as_of_max,
        "as_of_summary": as_of_summary,
        "portfolio_value": round(total, 2),
        "cash": round(cash, 2),
        "investor_profile": investor_profile,
        "signals": per,
        "watchlist_signals": watch_signals,
        "watchlist_data_quality": watch_grade,
        "watchlist_missing_prices": watch_missing,
        "watchlist_stale_days_max": watch_max_stale,
        "watchlist_as_of_min": watch_as_of_min,
        "watchlist_as_of_max": watch_as_of_max,
        "watchlist_as_of_summary": watch_as_of_summary,
        "valuation_status": valuation_status,
        "rebalance": rebal,
        "action_discipline": action_discipline,
        "actionable_rebalance": actionable_rebalance,
        "first_funding_plan": first_funding_plan,
        "risk_budget": risk_budget,
        "momentum_rank": momentum_rank,
        "trend_alerts": trend_alerts,
        "trend_protection": trend_protection,   # §0C #4 回测量化的回撤保护（趋势过滤 vs 静态）
        "tactical": tactical_shadow,
        "params": {
            "ma_days": ma_days, "momentum_lookback": look,
            "rebalance_abs_pp": abs_thr * 100, "rebalance_rel": rel_thr,
            "rebalance_check_frequency": check_freq, "rebalance_circuit_breaker_pp": round(breaker_thr * 100, 2),
            "stale_limit_days": STALE_LIMIT_DAYS,
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 控制台可读摘要
    print("=" * 54)
    print(f"日期 {out['generated_for']} ｜ 数据【{grade}】{'（含缓存）' if used_cache else ''}"
          f" ｜ 行情截至 {as_of_summary}")
    print(f"组合总值约 ¥{total:,.0f}（含现金 ¥{cash:,.0f}）")
    print("-" * 54)
    for c, s in per.items():
        if "error" in s:
            print(f"{s['name']}({c}): {s['error']}")
            continue
        line = f"{s['name']}({c}){'[缓存]' if s.get('source') == 'cache' else ('[腾讯]' if s.get('source') == 'westock' else '')}: " + (
            "↑在均线上" if s["trend"] == "above" else "↓跌破均线")
        m = s.get(f"momentum_{look}d")
        if m is not None:
            line += f" ｜ 动量{m * 100:+.1f}%"
        if "valuation" in s:
            line += f" ｜ 估值分位{s['valuation']['percentile'] * 100:.0f}%({s['valuation']['tag']})"
        elif s.get("valuation_na"):
            line += " ｜ 估值不适用"
        elif "valuation_missing" in s:
            line += " ｜ 估值缺失(非中性)"
        print(line)
    print("-" * 54)
    if not rebal_ok:
        why = ("缺失行情：" + ", ".join(missing)) if missing else f"数据过旧（最旧 {max_stale} 天）"
        print(f"⚠️ {why} —— 本次不输出再平衡建议，请稍后重跑")
    else:
        if used_cache:
            print(f"注：部分数据来自缓存（最旧 {max_stale} 天），建议仅供参考")
        if discipline_blockers:
            print("纪律检查：本周不允许执行交易动作 —— " + "；".join(discipline_blockers))
        else:
            print(f"纪律检查：允许交易｜单笔≥¥{min_trade:,.0f}｜单周≤¥{max_weekly:,.0f}")
        actionable = [r for r in actionable_rebalance if r["actionable"]]
        blocked = [r for r in actionable_rebalance if r["triggered"] and not r["actionable"]]
        if actionable:
            print("可执行再平衡动作：")
            for r in actionable:
                verb = "减仓" if r["suggest"] == "trim" else "加仓"
                print(f"  {verb} {r['name']}({r['code']}) 约 ¥{r['approx_amount']:,.0f}"
                      f"（偏离 {r['deviation_pp']:+.1f}pp）")
                if r.get("action_reason"):
                    print(f"      理由：{r['action_reason']}")
        if blocked:
            print("被门槛拦截的原始再平衡信号：")
            for r in blocked:
                verb = "减仓" if r["suggest"] == "trim" else "加仓"
                print(f"  {verb} {r['name']}({r['code']}) 约 ¥{r['approx_amount']:,.0f}"
                      f" —— {'；'.join(r['blocked_reasons'])}")
        if not actionable and not blocked:
            print("无再平衡触发（持仓为空或未超阈值）")
    if trend_alerts:
        print("-" * 54)
        dpp = trend_protection.get("delta_pp")
        print(f"⚠️ 危机保险·趋势减仓建议（已跌破 MA{ma_days}）—— 趋势转弱，建议移到 {trend_alerts[0].get('reserve_name') or '债券'}：")
        for a in trend_alerts:
            tag = f"约 ¥{a['derisk_amount']:,.0f} → {a.get('reserve_name') or '债券'}" if a.get("actionable") \
                else "；".join(a.get("blocked_reasons") or [])
            print(f"   减 {a['name']}({a['code']}) {tag}")
        if dpp:
            print(f"   依据：历史上趋势过滤把最大回撤从 {abs(trend_protection['static_maxdd'])*100:.0f}% 降到 "
                  f"{abs(trend_protection['trend_maxdd'])*100:.0f}%（不动手约多扛 {dpp:.0f}pp 回撤）。样本内、需你确认，不自动下单。")
    if first_funding_plan["eligible"]:
        print("-" * 54)
        print(f"首次建仓预览：计划投入 ¥{first_funding_plan['planned_deploy_amount']:,.0f}"
              f"（现金的 {first_pct * 100:.0f}%）｜估算可成交 ¥{first_funding_plan['estimated_deploy_amount']:,.0f}")
        for o in first_funding_plan["orders"]:
            status = "可执行" if o["actionable"] else "暂不执行"
            print(f"  {o['name']}({o['code']}): {status} ｜ {o['estimated_shares']} 份"
                  f" ｜ 约 ¥{o['estimated_amount']:,.0f}")
    if watch_signals:
        print("-" * 54)
        print(f"观察池（仅学习/监控，不触发交易）｜数据【{watch_grade}】｜行情截至 {watch_as_of_summary}")
        for c, s in watch_signals.items():
            if "error" in s:
                print(f"{s['name']}({c}): {s['error']}")
                continue
            line = f"{s['name']}({c}){'[缓存]' if s.get('source') == 'cache' else ('[腾讯]' if s.get('source') == 'westock' else '')}: " + (
                "↑在均线上" if s["trend"] == "above" else "↓跌破均线")
            m = s.get(f"momentum_{look}d")
            if m is not None:
                line += f" ｜ 动量{m * 100:+.1f}%"
            if s.get("role"):
                line += f" ｜ {s['role']}"
            print(line)
    print("=" * 54)
    print(f"已写出 {args.out}")


if __name__ == "__main__":
    main()
