"""回补历史快照，用于验证连续性追踪。

用法：python tools/backfill.py [天数]
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import analyze as engine  # noqa: E402
from src import eastmoney  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")


def main(days: int = 6):
    td = eastmoney.latest_trade_date()
    dates = []
    for _ in range(days):
        dates.append(td)
        try:
            td = eastmoney.previous_trade_date(td)
        except Exception:  # noqa: BLE001
            break
    dates.reverse()
    print("回补交易日:", dates)

    for d in dates:
        try:
            snap = eastmoney.fetch_snapshot(d)
            r = engine.analyze(snap, history_days=6)
            engine.save_history(r)
            print(f"  {d} ok  {r['market']['stock_count']}只  "
                  f"净额 {r['market']['total_net'] / 1e8:+.2f}亿")
        except Exception as e:  # noqa: BLE001
            print(f"  {d} 失败: {e}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 6)
