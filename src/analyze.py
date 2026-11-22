"""游资动向分析引擎。

口径说明：
  1. 龙虎榜同一只股票可能因多个上榜原因出现多条记录，"连续三个交易日"类属于三日累计口径，
     与单日口径混用会严重高估，因此一律按"单日口径优先"去重。
  2. 金额一律由席位明细聚合得出，不使用个股汇总表的金额字段，保证口径统一。
  3. 游资口径 = 一线游资 + 活跃游资 + 量化席位 + 外资；机构/北向/散户单独统计。
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path

from . import seats as seatlib

log = logging.getLogger(__name__)

HISTORY_DIR = Path(__file__).resolve().parent.parent / "history"
AMT_UNITS = {"亿": 1e8, "万": 1e4}


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _scope_rank(explanation: str | None) -> int:
    """0=单日口径，1=多日累计口径。"""
    return 1 if (explanation and "连续" in explanation) else 0


def is_convertible_bond(code: str) -> bool:
    """可转债 T+0 交易，成交额与股票不可比，混入榜单会严重扭曲排名。"""
    return str(code)[:2] in {"11", "12"}


def _dedup(rows: list[dict], key_fn, expl_key: str = "EXPLANATION") -> list[dict]:
    best: dict = {}
    for r in rows:
        k = key_fn(r)
        rank = _scope_rank(r.get(expl_key))
        cur = best.get(k)
        if cur is None or rank < cur[0]:
            best[k] = (rank, r)
    return [v[1] for v in best.values()]


# ---------------------------------------------------------------- 席位聚合

def _agg_seats(seat_rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in seat_rows:
        name = r.get("OPERATEDEPT_NAME") or r.get("ORG_NAME_ABBR") or ""
        info = seatlib.identify(name)
        alias = info["alias"]
        buy, sell = _f(r.get("ACT_BUY")), _f(r.get("ACT_SELL"))
        net = _f(r.get("NET_AMT")) or (buy - sell)

        node = out.get(alias)
        if node is None:
            node = out[alias] = {
                "alias": alias,
                "tier": info["tier"],
                "tags": info["tags"],
                "buy": 0.0,
                "sell": 0.0,
                "net": 0.0,
                "seat_names": set(),
                "_stock_map": {},
                "buy_times": 0,
                "sell_times": 0,
                "rise_probs": [],
                "active_3d": [],
            }
        node["buy"] += buy
        node["sell"] += sell
        node["net"] += net
        node["seat_names"].add(name)
        if r.get("RISE_PROB_3D") is not None:
            node["rise_probs"].append(_f(r.get("RISE_PROB_3D")))
        if r.get("ACTIVE_3D") is not None:
            node["active_3d"].append(_f(r.get("ACTIVE_3D")))
        if net > 0:
            node["buy_times"] += 1
        elif net < 0:
            node["sell_times"] += 1

        code = str(r.get("SECURITY_CODE"))
        sm = node["_stock_map"].get(code)
        if sm is None:
            node["_stock_map"][code] = {
                "code": code,
                "name": r.get("SECURITY_NAME_ABBR") or "",
                "net": net, "buy": buy, "sell": sell,
                "change": _f(r.get("CHANGE_RATE")),
            }
        else:
            sm["net"] += net
            sm["buy"] += buy
            sm["sell"] += sell

    for node in out.values():
        # 按净额降序（不是绝对值）：加仓榜要的是「买得最多的排前面」，
        # 按绝对值会让一笔大额卖出插到买入前面，读起来像在推荐那只票
        node["stocks"] = sorted(node["_stock_map"].values(), key=lambda x: -x["net"])
        node["stock_count"] = len(node["stocks"])
        node["seat_count"] = len(node["seat_names"])
        node["seat_names"] = sorted(node["seat_names"])
        probs = node.pop("rise_probs")
        node["rise_prob"] = sum(probs) / len(probs) if probs else None
        actives = node.pop("active_3d")
        node["active_3d"] = max(actives) if actives else None
        node.pop("_stock_map", None)
    return out


# ---------------------------------------------------------------- 个股聚合

def _agg_stocks(
    stock_meta: dict[str, dict],
    seat_rows: list[dict],
    industries: dict[str, str],
    limit_up_codes: set[str],
) -> list[dict]:
    out: dict[str, dict] = {}

    for code, meta in stock_meta.items():
        out[code] = {
            "code": code,
            "name": meta.get("SECURITY_NAME_ABBR") or "",
            "close": _f(meta.get("CLOSE_PRICE")),
            "change": _f(meta.get("CHANGE_RATE")),
            "turnover": _f(meta.get("TURNOVERRATE")),
            "float_cap": _f(meta.get("FREE_MARKET_CAP")),
            "amount": _f(meta.get("ACCUM_AMOUNT")),
            "reason": meta.get("EXPLANATION") or "",
            "industry": industries.get(code, ""),
            "is_limit_up": code in limit_up_codes,
            "youzi_buy": 0.0, "youzi_sell": 0.0, "youzi_net": 0.0,
            "inst_net": 0.0, "north_net": 0.0, "retail_net": 0.0,
            "other_net": 0.0, "total_net": 0.0,
            "buyers": {}, "sellers": [],
        }

    for r in seat_rows:
        code = str(r.get("SECURITY_CODE"))
        node = out.get(code)
        if node is None:
            continue
        name = r.get("OPERATEDEPT_NAME") or r.get("ORG_NAME_ABBR") or ""
        info = seatlib.identify(name)
        buy, sell = _f(r.get("ACT_BUY")), _f(r.get("ACT_SELL"))
        net = _f(r.get("NET_AMT")) or (buy - sell)
        tier = info["tier"]
        node["total_net"] += net

        if seatlib.is_youzi(info):
            node["youzi_buy"] += buy
            node["youzi_sell"] += sell
            node["youzi_net"] += net
            alias = info["alias"]
            item = node["buyers"].get(alias)
            if item is None:
                item = node["buyers"][alias] = {
                    "alias": alias,
                    "tier": tier,
                    "tier_label": seatlib.tier_label(tier),
                    "tags": info["tags"],
                    "net": 0.0, "buy": 0.0, "sell": 0.0,
                }
            item["net"] += net
            item["buy"] += buy
            item["sell"] += sell
        elif tier == "inst":
            node["inst_net"] += net
        elif tier == "north":
            node["north_net"] += net
        elif tier == "retail":
            node["retail_net"] += net
        else:
            node["other_net"] += net

    for node in out.values():
        parts = list(node["buyers"].values())
        node["buyers"] = sorted([p for p in parts if p["net"] > 0], key=lambda x: -x["net"])
        node["sellers"] = sorted([p for p in parts if p["net"] < 0], key=lambda x: x["net"])
        node["in_count"] = len(node["buyers"])
        node["out_count"] = len(node["sellers"])
        node["youzi_count"] = node["in_count"] + node["out_count"]
    return list(out.values())


# ---------------------------------------------------------------- 板块

def _agg_sectors(stocks: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for s in stocks:
        ind = s["industry"] or "未分类"
        node = agg.setdefault(ind, {"industry": ind, "youzi_net": 0.0, "count": 0,
                                    "inst_net": 0.0, "stocks": []})
        node["youzi_net"] += s["youzi_net"]
        node["inst_net"] += s["inst_net"]
        node["count"] += 1
        node["stocks"].append({"code": s["code"], "name": s["name"],
                               "net": s["youzi_net"], "change": s["change"]})
    for node in agg.values():
        node["stocks"].sort(key=lambda x: -x["net"])
        node["stocks"] = node["stocks"][:3]
    return sorted(agg.values(), key=lambda x: -x["youzi_net"])


# ---------------------------------------------------------------- 连板梯队

def _build_ladder(pool: list[dict]) -> dict:
    if not pool:
        return {"total": 0, "max_board": 0, "tiers": [], "broken": 0}
    tiers: dict[int, list] = defaultdict(list)
    for z in pool:
        tiers[int(z.get("lbc") or 1)].append({
            "code": str(z.get("c")),
            "name": z.get("n"),
            "board": int(z.get("lbc") or 1),
            "industry": z.get("hybk") or "",
            "change": _f(z.get("zdp")),
            "first_time": z.get("fbt"),
            "broken_times": z.get("zbc") or 0,
            "amount": _f(z.get("amount")),
        })
    out = []
    for b in sorted(tiers, reverse=True):
        out.append({"board": b, "count": len(tiers[b]), "stocks": tiers[b]})
    above = {b: v for b, v in tiers.items() if b >= 2}
    return {
        "total": len(pool),
        "total_2plus": sum(len(v) for v in above.values()),
        "max_board": max(tiers) if tiers else 0,
        "tiers": out,
        "broken": sum(1 for z in pool if (z.get("zbc") or 0) > 0),
    }


# ---------------------------------------------------------------- 历史连续性

def load_history(days: int = 6, before: str | None = None) -> list[dict]:
    """读取历史快照，按时间从旧到新返回。"""
    if not HISTORY_DIR.exists():
        return []
    files = sorted(HISTORY_DIR.glob("*.json"), key=lambda f: f.stem)
    if before:
        files = [f for f in files if f.stem < before]
    out = []
    for f in files[-days:]:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return out


def _continuity(today_seats: dict[str, dict], today_stocks: list[dict], history: list[dict]) -> dict:
    if not history:
        return {"available": False, "days": 0}

    seat_hist: dict[str, dict] = {}
    stock_hist: dict[str, dict] = {}
    for h in history:  # 旧 → 新，便于计算连续净买入天数
        for s in h.get("seats") or []:
            node = seat_hist.setdefault(s["alias"], {"net": 0.0, "days": 0, "streak": 0,
                                                     "tier": s.get("tier")})
            net = _f(s.get("net"))
            node["net"] += net
            node["days"] += 1
            node["streak"] = node["streak"] + 1 if net > 0 else 0
        for s in h.get("stocks") or []:
            node = stock_hist.setdefault(s["code"], {"net": 0.0, "days": 0, "streak": 0,
                                                     "name": s.get("name")})
            net = _f(s.get("net"))
            node["net"] += net
            node["days"] += 1
            node["streak"] = node["streak"] + 1 if net > 0 else 0

    persistent, fresh = [], []
    for alias, node in today_seats.items():
        if node["tier"] in {"inst", "north", "retail"}:
            continue
        today_net = node["net"]
        h = seat_hist.get(alias)
        if today_net <= 0:
            continue
        streak = (h["streak"] if h else 0) + 1
        hist_net = h["net"] if h else 0.0
        total = hist_net + today_net
        if streak >= 2:
            persistent.append({
                "alias": alias, "net": today_net, "tier": node["tier"],
                "hist_net": hist_net, "total_net": total, "days": streak,
                "stock_count": node["stock_count"],
            })
        elif h is None and today_net >= 3e7:
            fresh.append({"alias": alias, "net": today_net, "tier": node["tier"],
                          "stock_count": node["stock_count"]})
    persistent.sort(key=lambda x: -x["total_net"])
    fresh.sort(key=lambda x: -x["net"])

    # 个股：连续被游资净买入
    stock_persist = []
    for s in today_stocks:
        if s["youzi_net"] <= 0:
            continue
        h = stock_hist.get(s["code"])
        streak = (h["streak"] if h else 0) + 1
        if streak < 2:
            continue
        stock_persist.append({
            "code": s["code"], "name": s["name"], "days": streak,
            "today_net": s["youzi_net"],
            "total_net": (h["net"] if h else 0.0) + s["youzi_net"],
            "change": s["change"], "industry": s["industry"],
            # 只说「连续被买」不够用，得交代是谁在持续买
            "buyers": [{"alias": b["alias"], "net": b["net"], "tier": b.get("tier")}
                       for b in (s.get("buyers") or [])[:4]],
            "buyer_count": len(s.get("buyers") or []),
        })
    stock_persist.sort(key=lambda x: -x["total_net"])

    return {
        "available": True,
        "days": len(history),
        "persistent_seats": persistent[:8],
        "fresh_seats": fresh[:6],
        "persistent_stocks": stock_persist[:8],
    }


# ---------------------------------------------------------------- 主入口

def analyze(
    snapshot: dict,
    prev_stocks: list[dict] | None = None,
    history_days: int = 6,
) -> dict:
    td = snapshot["trade_date"]
    seat_rows = _dedup(snapshot["seats"],
                       lambda r: (r.get("OPERATEDEPT_CODE"), r.get("SECURITY_CODE")))
    bond_codes = {str(r.get("SECURITY_CODE")) for r in seat_rows
                  if is_convertible_bond(r.get("SECURITY_CODE"))}
    if bond_codes:
        log.info("剔除可转债 %s 只（%s）", len(bond_codes), ",".join(sorted(bond_codes)))
        seat_rows = [r for r in seat_rows if str(r.get("SECURITY_CODE")) not in bond_codes]

    stock_meta = {
        str(r["SECURITY_CODE"]): r
        for r in _dedup(snapshot["stocks"], lambda r: r.get("SECURITY_CODE"))
        if not is_convertible_bond(r["SECURITY_CODE"])
    }

    industries = snapshot.get("industries") or {}
    pool = snapshot.get("limit_up") or []
    limit_codes = {str(z.get("c")) for z in pool if z.get("c")}

    seat_agg = _agg_seats(seat_rows)
    stocks = _agg_stocks(stock_meta, seat_rows, industries, limit_codes)

    # 总览
    def _sum(tiers):
        return sum(n["net"] for n in seat_agg.values() if n["tier"] in tiers)

    youzi_net = _sum(seatlib.YOUZI_TIERS)
    youzi_buy = sum(n["buy"] for n in seat_agg.values() if n["tier"] in seatlib.YOUZI_TIERS)
    youzi_sell = sum(n["sell"] for n in seat_agg.values() if n["tier"] in seatlib.YOUZI_TIERS)
    total_buy = sum(n["buy"] for n in seat_agg.values())
    total_net = sum(n["net"] for n in seat_agg.values())

    market = {
        "stock_count": len(stocks),
        "seat_count": len({(r.get("OPERATEDEPT_CODE")) for r in seat_rows}),
        "youzi_buy": youzi_buy,
        "youzi_sell": youzi_sell,
        "youzi_net": youzi_net,
        "inst_net": _sum({"inst"}),
        "north_net": _sum({"north"}),
        "retail_net": _sum({"retail"}),
        "other_net": _sum({"other"}),
        "total_net": total_net,
        "total_buy": total_buy,
        "quant_net": _sum({"quant"}),
        "top_net": _sum({"top"}),
        "youzi_in_stocks": sum(1 for s in stocks if s["youzi_net"] > 0),
        "youzi_out_stocks": sum(1 for s in stocks if s["youzi_net"] < 0),
        "limit_up_count": len(pool),
        "limit_up_youzi": sum(1 for s in stocks if s["is_limit_up"] and s["youzi_net"] > 0),
        "known_ratio": (youzi_net / total_net) if total_net else 0,
        "known_buy_ratio": (youzi_buy / total_buy) if total_buy else 0,
    }

    # 榜单
    youzi_seats = [n for n in seat_agg.values() if seatlib.is_youzi(n)]
    top_buy_seats = sorted(youzi_seats, key=lambda x: -x["net"])[:15]
    top_sell_seats = sorted([n for n in youzi_seats if n["net"] < 0], key=lambda x: x["net"])[:12]

    hot_money_seats = [n for n in seat_agg.values() if seatlib.is_hot_money(n)]
    top_hot_seats = sorted(hot_money_seats, key=lambda x: -x["net"])[:12]

    # 未收录的营业部里净买入最大的，往往是还没被命名的游资
    unknown_seats = sorted(
        [n for n in seat_agg.values() if n["tier"] == "other"],
        key=lambda x: -x["net"],
    )[:10]

    # 抱团做多：多路游资同时净买入且股价没有大跌，按合力金额排序
    huddle = sorted(
        [s for s in stocks if s["youzi_net"] > 0 and s["in_count"] >= 2 and s["change"] > -5],
        key=lambda s: -s["youzi_net"],
    )[:12]

    # 跌停接货：股价重挫仍被游资净买入，博反弹或撬板
    limbo = sorted(
        [s for s in stocks if s["youzi_net"] > 2e7 and s["change"] <= -5],
        key=lambda s: -s["youzi_net"],
    )[:10]

    dump = sorted([s for s in stocks if s["youzi_net"] < 0], key=lambda s: s["youzi_net"])[:10]

    divergence = sorted(
        [s for s in stocks if (s["inst_net"] > 0 and s["youzi_net"] < 0)
         or (s["inst_net"] < 0 and s["youzi_net"] > 0)],
        key=lambda s: -(abs(s["inst_net"]) + abs(s["youzi_net"])),
    )[:10]

    gainers = sorted(stocks, key=lambda s: -s["youzi_net"])[:10]
    sectors = _agg_sectors(stocks)

    # 量化席位专项
    quant_seats = sorted([n for n in seat_agg.values() if n["tier"] == "quant"],
                         key=lambda x: -abs(x["net"]))[:10]
    quant_stocks = sorted([s for s in stocks if any(b["tier"] == "quant" for b in s["buyers"])],
                          key=lambda s: -s["youzi_net"])[:10]

    # 赚钱效应：上一交易日上榜股今日表现
    prev_effect = None
    if prev_stocks:
        chg = [_f(r.get("CHANGE_RATE")) for r in prev_stocks]
        d1 = [_f(r.get("D1_CLOSE_ADJCHRATE")) for r in prev_stocks if r.get("D1_CLOSE_ADJCHRATE") is not None]
        if d1:
            up = sum(1 for x in d1 if x > 0)
            prev_effect = {
                "count": len(d1),
                "avg": sum(d1) / len(d1),
                "up_ratio": up / len(d1),
                "limit_up": sum(1 for x in d1 if x >= 9.8),
                "down5": sum(1 for x in d1 if x <= -5),
            }
        elif chg:
            pass

    return {
        "trade_date": td,
        "market": market,
        "seat_agg": seat_agg,
        "top_buy_seats": top_buy_seats,
        "top_sell_seats": top_sell_seats,
        "top_hot_seats": top_hot_seats,
        "unknown_seats": unknown_seats,
        "stocks": stocks,
        "huddle": huddle,
        "limbo": limbo,
        "dump": dump,
        "divergence": divergence,
        "gainers": gainers,
        "sectors": sectors,
        "ladder": _build_ladder(pool),
        "quant_seats": quant_seats,
        "quant_stocks": quant_stocks,
        "prev_effect": prev_effect,
        "continuity": _continuity(seat_agg, stocks, load_history(history_days, before=td)),
    }


def save_history(result: dict) -> Path:
    """保存精简快照，供次日做连续性分析。"""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    seats = sorted(result["seat_agg"].values(), key=lambda x: -abs(x["net"]))
    payload = {
        "trade_date": result["trade_date"],
        "seats": [
            {"alias": n["alias"], "net": round(n["net"], 2), "tier": n["tier"],
             "stock_count": n["stock_count"]}
            for n in seats[:200] if n["tier"] != "inst"
        ],
        "stocks": [
            {"code": s["code"], "name": s["name"], "net": round(s["youzi_net"], 2)}
            for s in sorted(result["stocks"], key=lambda x: -abs(x["youzi_net"]))[:150]
        ],
        "market": result["market"],
    }
    path = HISTORY_DIR / f"{result['trade_date']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path
