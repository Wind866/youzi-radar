"""未收录席位观察池：让新面孔自己浮出来。

别名库 data/youzi_seats.json 是手工维护的。新出现的营业部不会自己变成
「已知」，而 YOUZI_TIERS 不含 other，这些资金也就不计进游资净额——这是口径
上的必然，不该用「自动改 tier」去修正，机器分不清某家营业部是新游资、
散户大本营还是量化通道，猜错就污染整张榜。

所以这里只观察、不改口径：每天把未收录席位里净买入够格的记下来，按交易日
累积；累计上榜够多且金额达门槛的标为候选，报告里点名。人工确认后写进
data/youzi_seats.json，它才算真正入库。next 次上榜就会被当成已知游资。

同一天重复运行是幂等的：金额按日期覆盖写入，不会因为 Worker 和 cron 各触发
一次就把金额翻倍。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCH_FILE = ROOT / "data" / "seat_watchlist.json"
HISTORY_DIR = ROOT / "history"

VERSION = 1

MIN_NET = 5e6        # 单日净买入不到 500 万不记，否则池子会被碎单淹没
CAND_DAYS = 3        # 候选门槛：累计上榜 ≥3 个交易日
CAND_NET = 1e8       # 且累计净买入 ≥1 亿
FAST_NET = 3e8       # 快速通道：累计 ≥3 亿，两天也认
KEEP_DAYS = 30       # date_net 明细保留的交易日数，更早折叠成标量
STALE_NET = 3e7      # 「金额够大」的界线：累计过 3000 万的长期保留
SHORT_DAYS = 10      # 单日、小额的席位给这么久等它再出现
STALE_DAYS = 60      # 反复上榜的席位，这么久不动就清掉
HARD_DAYS = 120      # 大额席位也留不住这么久
MAX_ENTRIES = 500    # 池子上限，防无限膨胀


def trade_days(td: str) -> list[str]:
    """已产出的交易日序列（history 文件名即交易日），含今天。"""
    days = {p.stem for p in HISTORY_DIR.glob("*.json")} if HISTORY_DIR.is_dir() else set()
    days.add(td)
    return sorted(days)


def load() -> dict:
    if not WATCH_FILE.exists():
        return {"version": VERSION, "updated": None, "seats": {}}
    try:
        d = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logging.warning("观察池读取失败，按空池重建: %s", e)
        return {"version": VERSION, "updated": None, "seats": {}}
    d.setdefault("seats", {})
    return d


def save(pool: dict) -> Path:
    WATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCH_FILE.write_text(
        json.dumps(pool, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return WATCH_FILE


def _new_node(td: str) -> dict:
    return {
        "first_seen": td,
        "last_seen": td,
        "days_pre": 0,      # 已折叠的早期上榜天数
        "net_pre": 0,       # 已折叠的早期累计净额
        "date_net": {},     # 近 KEEP_DAYS 个交易日的逐日净额
        "names": [],        # 营业部全名，便于核对与写规则
        "recent": {},       # 近 2 日买入标的，便于判断路数
    }


def _fold(node: dict, days: list[str]) -> None:
    """把超出保留窗口的逐日明细折叠成标量。

    不折叠的话，跑满一年每个席位要存 240 个日期键，池子会膨胀到几 MB，
    每天提交一次 diff 很难看。折叠后只丢「某一天具体多少」，累计值和
    上榜天数都还在。
    """
    keep = set(days[-KEEP_DAYS:])
    old = [d for d in node["date_net"] if d not in keep]
    for d in old:
        node["net_pre"] += node["date_net"].pop(d)
        node["days_pre"] += 1
    node["net_pre"] = round(node["net_pre"])


def _streak(node: dict, days: list[str]) -> int:
    """连续上榜天数：从最近交易日起逐日回看，缺席即断。"""
    n = 0
    for d in reversed(days):
        if d not in node["date_net"]:
            break
        n += 1
    return n


def _is_candidate(node: dict) -> bool:
    net = node.get("net_total", 0)
    if net <= 0:
        return False
    return (node.get("days", 0) >= CAND_DAYS and net >= CAND_NET) or net >= FAST_NET


def merge(pool: dict, td: str, rows: list[dict], days: list[str]) -> tuple[dict, list[str]]:
    """把当日未收录席位并入观察池，返回（池, 今日新晋候选）。

    rows 传 analyze 的 other_seats（全部未收录席位，不是报告里截断的 top10）。
    """
    seats = pool.setdefault("seats", {})
    before = {k: bool(v.get("candidate")) for k, v in seats.items()}

    for r in rows:
        net = float(r.get("net") or 0)
        if net < MIN_NET:
            # 只盯净买入：净卖出的新面孔不属于「可能的新游资」这条线，
            # 真要看也在出货榜里，不该让它占池子
            continue
        alias = r.get("alias") or ""
        if not alias:
            continue
        node = seats.get(alias) or _new_node(td)
        seats[alias] = node
        node["date_net"][td] = round(net)          # 按日覆盖，重跑不翻倍
        node["last_seen"] = max(td, node.get("last_seen") or td)
        node["first_seen"] = min(td, node.get("first_seen") or td)
        names = r.get("seat_names") or []
        if names:
            node["names"] = names[:3]
        node["recent"][td] = [[s["code"], s["name"], round(float(s["net"]))]
                              for s in (r.get("stocks") or [])[:6]]
        node["recent"] = dict(sorted(node["recent"].items())[-2:])

        _fold(node, days)
        node["days"] = node["days_pre"] + len(node["date_net"])
        node["net_total"] = round(node["net_pre"] + sum(node["date_net"].values()))
        node["streak"] = _streak(node, days)
        node["candidate"] = _is_candidate(node)

    pool["updated"] = td
    pool["version"] = VERSION
    _prune(pool, days)

    newly = [k for k, v in pool["seats"].items()
             if v.get("candidate") and not before.get(k)]
    return pool, newly


def _offset(days: list[str], n: int) -> str:
    """往前数 n 个交易日的日期；日期不够长就返回最早那天（等于不清理）。"""
    if not days:
        return ""
    return days[-n] if len(days) >= n else days[0]


def _prune(pool: dict, days: list[str]) -> None:
    """按「值不值得继续看」清池子。

    单日出现、金额又不大的席位占了绝大多数（六天里 152/193），全留着的话
    一年就上万条，diff 没法看。但也不能当天就删——它可能隔几天再来，那时
    才看得出是持续行为。所以分三档：够大的留 120 天、反复上榜的留 60 天、
    单日小额的只留 10 天等它续上。
    """
    seats = pool["seats"]
    short_cut = _offset(days, SHORT_DAYS)
    old_cut = _offset(days, STALE_DAYS)
    hard_cut = _offset(days, HARD_DAYS)

    drop = []
    for k, v in seats.items():
        net = abs(v.get("net_total") or 0)
        seen = v.get("last_seen") or ""
        if net >= STALE_NET or (v.get("days") or 0) >= 2:
            if seen < hard_cut:
                drop.append(k)
            continue
        if seen < short_cut:
            drop.append(k)
    for k in drop:
        seats.pop(k, None)

    if drop:
        logging.info("观察池清理 %s 条（久未上榜且金额不大），保留 %s 条", len(drop), len(seats))
    if len(seats) > MAX_ENTRIES:
        keep = sorted(seats.items(), key=lambda kv: -abs(kv[1].get("net_total") or 0))
        pool["seats"] = dict(keep[:MAX_ENTRIES])
        logging.info("观察池超过 %s 条，按累计金额保留前 %s 条", MAX_ENTRIES, MAX_ENTRIES)


def annotate(result: dict, pool: dict, td: str, newly: list[str]) -> None:
    """把观察池信息挂到分析结果上，供渲染层标注。"""
    seats = pool.get("seats") or {}
    rows = {}
    for n in result.get("unknown_seats") or []:
        node = seats.get(n["alias"])
        if not node:
            continue
        rows[n["alias"]] = {
            "days": node.get("days", 0),
            "streak": node.get("streak", 0),
            "net_total": node.get("net_total", 0),
            "candidate": bool(node.get("candidate")),
        }

    # 候选一旦达标就一直挂着，直到人工处理。只报「今日新晋」的话，漏看
    # 一天报告就再也看不到这条线索了；而这个线索的价值恰恰在于别错过。
    cands = []
    for k, v in seats.items():
        if not v.get("candidate"):
            continue
        today = (v.get("recent") or {}).get(td) or []
        cands.append({
            "alias": k,
            "days": v.get("days", 0),
            "streak": v.get("streak", 0),
            "net_total": v.get("net_total", 0),
            "first_seen": v.get("first_seen"),
            "last_seen": v.get("last_seen"),
            "names": v.get("names") or [],
            "stocks": [x[1] for x in today][:4],
            "is_new": k in set(newly),
            "active_today": v.get("last_seen") == td,
        })
    cands.sort(key=lambda x: -x["net_total"])

    result["watch"] = {
        "available": bool(seats),
        "pool_size": len(seats),
        "rows": rows,
        "candidates": cands[:8],
        "candidate_total": len(cands),
        "new_count": sum(1 for c in cands if c["is_new"]),
    }
