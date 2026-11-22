"""本地调试：打印分析结果概览。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import analyze, eastmoney, watchlist  # noqa: E402


def y(v):
    return f"{v / 1e8:+.2f}亿"


def main(td=None):
    snap = eastmoney.fetch_snapshot(td)
    r = analyze.analyze(snap)
    m = r["market"]

    print("交易日", r["trade_date"], "| 上榜", m["stock_count"], "只 | 席位", m["seat_count"], "个")
    print("全榜净额", y(m["total_net"]))
    print("  知名游资", y(m["youzi_net"]), f"占全榜买入额 {m['known_buy_ratio']*100:.0f}%")
    print("  一线", y(m["top_net"]), "| 量化", y(m["quant_net"]),
          "| 机构", y(m["inst_net"]), "| 北向", y(m["north_net"]),
          "| 散户", y(m["retail_net"]), "| 其他", y(m["other_net"]))

    print("\n=== 一线/活跃游资 加仓榜 ===")
    for n in r["top_hot_seats"][:8]:
        tags = " ".join(n["tags"][:2])
        names = [f"{s['name']}{s['change']:+.0f}%" for s in n["stocks"][:3]]
        print(f"  {n['alias']:<12}{n['tier']:<6}{y(n['net']):>9} {n['stock_count']}只 "
              f"胜率{(n['rise_prob'] or 0):.0f}% [{tags}] {names}")

    print("\n=== 量化席位 ===")
    for n in r["quant_seats"][:5]:
        print(f"  {n['alias']:<12}{y(n['net']):>9} {n['stock_count']}只 {[s['name'] for s in n['stocks'][:3]]}")

    print("\n=== 未收录营业部净买入 TOP ===")
    for n in r["unknown_seats"][:6]:
        print(f"  {n['alias']:<26}{y(n['net']):>9} {n['stock_count']}只")

    print("\n=== 出货榜 ===")
    for n in r["top_sell_seats"][:6]:
        print(f"  {n['alias']:<12}{y(n['net']):>9} {[s['name'] for s in n['stocks'][:3]]}")

    print("\n=== 抱团榜 ===")
    for s in r["huddle"][:6]:
        print(f"  {s['name']}({s['code']}) {s['change']:+.2f}% 游资净{y(s['youzi_net'])} "
              f"买家{s['in_count']}家 {[b['alias'] for b in s['buyers'][:4]]}")

    print("\n=== 机构游资分歧 ===")
    for s in r["divergence"][:5]:
        print(f"  {s['name']} {s['change']:+.2f}% 游资{y(s['youzi_net'])} "
              f"机构{y(s['inst_net'])} 北向{y(s['north_net'])}")

    print("\n=== 未收录观察池 ===")
    pool = watchlist.load()
    seats = pool.get("seats") or {}
    cands = [v for v in seats.values() if v.get("candidate")]
    print(f"  池内 {len(seats)} 家，其中候选 {len(cands)} 家（只读，不累积）")
    for k, v in sorted(seats.items(), key=lambda kv: -kv[1].get("net_total", 0))[:8]:
        print(f"  {k:<26}{y(v.get('net_total', 0)):>9} 上榜{v.get('days', 0)}日 "
              f"连{v.get('streak', 0)} {'⚑候选' if v.get('candidate') else ''}")

    print("\n=== 连板梯队 ===")
    for t in r["ladder"]["tiers"][:4]:
        print(f"  {t['board']}板 x{t['count']}: {[x['name'] for x in t['stocks']][:6]}")

    print("\n连续性:", r["continuity"].get("available"), "历史天数", r["continuity"].get("days"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
