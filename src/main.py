"""游资动向日报主流程。

用法：
    python -m src.main                      # 最近交易日，抓取并推送
    python -m src.main --date 2026-11-20    # 指定交易日
    python -m src.main --dry-run            # 只生成报告不推送
    python -m src.main --no-cache --print   # 强制重抓并打印 Markdown
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

from . import analyze as engine
from . import eastmoney, feishu, ledger, render

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports"


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="A股游资动向日报")
    p.add_argument("--date", help="交易日 YYYY-MM-DD，默认最近交易日")
    p.add_argument("--dry-run", action="store_true", help="不推送飞书")
    p.add_argument("--no-cache", action="store_true", help="忽略本地缓存，强制重新抓取")
    p.add_argument("--no-push", action="store_true", help="同 --dry-run")
    p.add_argument("--print", dest="show", action="store_true", help="打印 Markdown 报告")
    p.add_argument("--no-history", action="store_true", help="不写入历史快照")
    p.add_argument("--skip-if-stale", action="store_true",
                   help="工作日但取不到当日数据时跳过（用于定时任务避开休市日）")
    p.add_argument("--force", action="store_true",
                   help="忽略台账判重，强制重新推送（台账已记过的交易日默认跳过推送）")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _prev_stocks(trade_date: str) -> list[dict] | None:
    """上一交易日上榜个股，其 D1 涨幅即今日表现，用来衡量赚钱效应。"""
    try:
        ptd = eastmoney.previous_trade_date(trade_date)
        rows = eastmoney.billboard_stocks(ptd)
        logging.info("赚钱效应样本：%s 上榜 %s 条", ptd, len(rows))
        return rows
    except Exception as e:  # noqa: BLE001
        logging.warning("取上一交易日数据失败，跳过赚钱效应: %s", e)
        return None


def run(args: argparse.Namespace) -> int:
    td = args.date or eastmoney.latest_trade_date()
    now = datetime.now()
    if args.skip_if_stale and not args.date and td != now.strftime("%Y-%m-%d") and now.weekday() < 5:
        logging.info("今日 %s 无新龙虎榜数据（最近交易日 %s），判定为休市，跳过推送",
                     now.strftime("%Y-%m-%d"), td)
        return 0

    logging.info("===== 游资动向 %s =====", td)

    snapshot = eastmoney.fetch_snapshot(td, use_cache=not args.no_cache)
    result = engine.analyze(snapshot, prev_stocks=_prev_stocks(td))

    md = render.to_markdown(result)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORT_DIR / f"{td}.md"
    md_path.write_text(md, encoding="utf-8")
    logging.info("Markdown 已写入 %s（%s 字）", md_path.relative_to(ROOT), len(md))

    if not args.no_history:
        hist = engine.save_history(result)
        logging.info("历史快照 %s", hist.relative_to(ROOT))

    if args.show:
        print("\n" + md + "\n")

    card = render.to_feishu_card(result)
    size = len(str(card).encode("utf-8"))
    logging.info("飞书卡片 %s 字节", size)

    if args.dry_run or args.no_push:
        logging.info("已跳过推送。卡片预览：")
        logging.info(render.to_text(result))
        return 0

    # 台账判重：Worker 准点派发与 GitHub cron 兜底会各触发一次，只认第一次推送
    prev = ledger.pushed(td)
    if prev and not args.force:
        logging.info("⏭ 跳过本次推送：%s 已于 %s 推送成功（强制重推加 --force）",
                     td, prev.get("pushed_at"))
        return 0

    ok = feishu.send(card)
    if ok is False:
        logging.error("推送失败，请检查 webhook 与签名配置")
        return 2
    if ok is None:
        logging.warning("未配置飞书 webhook，本次仅生成报告")
    else:
        ledger.record(td, card_bytes=size,
                      stock_count=result["market"]["stock_count"],
                      youzi_net=round(result["market"]["youzi_net"], 2))
        logging.info("台账已记录 %s", td)

    m = result["market"]
    logging.info("完成 | 上榜 %s 只 | 全榜净额 %s | 游资 %s",
                 m["stock_count"], render.fmt_amt(m["total_net"]), render.fmt_amt(m["youzi_net"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    try:
        return run(args)
    except Exception as e:  # noqa: BLE001
        logging.error("运行失败: %s", e)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
