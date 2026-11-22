"""统计未收录席位，辅助补全别名库。"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import analyze, eastmoney, seats  # noqa: E402


def main(td="2026-11-20"):
    snap = eastmoney.fetch_snapshot(td)
    seat_rows = analyze._dedup(snap["seats"], analyze.seat_key)
    agg = defaultdict(lambda: {"net": 0.0, "buy": 0.0, "sell": 0.0, "n": 0, "stocks": []})
    for r in seat_rows:
        name = r.get("OPERATEDEPT_NAME") or ""
        info = seats.identify(name)
        if info["tier"] != "other":
            continue
        a = agg[name]
        a["net"] += analyze._f(r.get("NET_AMT"))
        a["buy"] += analyze._f(r.get("ACT_BUY"))
        a["sell"] += analyze._f(r.get("ACT_SELL"))
        a["n"] += 1
        a["stocks"].append(r.get("SECURITY_NAME_ABBR"))

    rows = sorted(agg.items(), key=lambda kv: -kv[1]["net"])
    print(f"未收录席位 {len(rows)} 个，按净买入排序：\n")
    for name, a in rows[:28]:
        print(f"{a['net']/1e8:+7.2f}亿  {a['n']:>2}笔  {name}  {a['stocks'][:3]}")

    print("\n--- 缺行业 ---")
    r = analyze.analyze(snap)
    for s in r["stocks"]:
        if not s["industry"]:
            print(" ", s["code"], s["name"], round(s["youzi_net"] / 1e8, 2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-11-20")
