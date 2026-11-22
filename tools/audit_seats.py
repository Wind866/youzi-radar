"""统计未收录席位，辅助补全别名库。"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import analyze, eastmoney, seats, watchlist  # noqa: E402


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

    # 观察池里已累积多日、金额达门槛的，才是真正值得写规则的对象——
    # 单日金额榜里排在前面的大多只是偶尔上榜的普通营业部
    pool = watchlist.load()
    cands = sorted(
        [(k, v) for k, v in (pool.get("seats") or {}).items() if v.get("candidate")],
        key=lambda kv: -kv[1].get("net_total", 0),
    )
    print(f"\n--- 观察池候选 {len(cands)} 家（池内共 {len(pool.get('seats') or {})} 家）---")
    if not cands:
        print("  暂无。观察池需要累积至少 %s 个交易日才出候选。" % watchlist.CAND_DAYS)
    for k, v in cands[:12]:
        print(f"  {k}  上榜 {v['days']} 日（连榜 {v.get('streak', 0)}）  "
              f"累计 {v['net_total']/1e8:+.2f}亿  首次 {v.get('first_seen')}")
        print(f"      {v.get('names')}")
    if cands:
        k, v = cands[0]
        full = (v.get("names") or [""])[0]
        org, place = seats.split_name(full)
        skeleton = {
            "id": "待填-唯一标识",
            "alias": k,
            "tier": "hot",
            "tags": [],
            "orgs": [org] if org else [],
            "match": [place] if place else [k],
            "note": f"观察池候选：累计 {v['days']} 日 {v['net_total']/1e8:.2f} 亿，"
                    f"首次 {v.get('first_seen')}。tier 按实际路数确认后再定",
        }
        print("\n可粘贴进 data/youzi_seats.json 的骨架（记得改 id 与 tier）：")
        print(json.dumps(skeleton, ensure_ascii=False, indent=2))

        # 自检：拿骨架里的 orgs+match 反测全名。别名不是全名子串，所以只能
        # 用这两条去验；验不过说明拆分出来的地名不可靠，得手改
        orgs = skeleton["orgs"]
        ok = (not orgs or any(o in full for o in orgs)) and \
             any(x in full for x in skeleton["match"])
        print(f"\n自检：{full}\n  → orgs={orgs} match={skeleton['match']} "
              f"{'命中' if ok else '未命中，需要手改 match'}")
        print("提示：tier 取 top/hot/quant/foreign 才会算进游资口径；"
              "普通营业部建议不入库，留在观察池即可。")

    print("\n--- 缺行业 ---")
    r = analyze.analyze(snap)
    for s in r["stocks"]:
        if not s["industry"]:
            print(" ", s["code"], s["name"], round(s["youzi_net"] / 1e8, 2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-11-20")
