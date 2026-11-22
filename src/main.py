"""游资动向日报主流程。

用法：
    python -m src.main                      # 最近交易日，抓取并推送
    python -m src.main --date 2026-11-20    # 指定交易日
    python -m src.main --dry-run            # 只生成报告不推送
    python -m src.main --no-cache --print   # 强制重抓并打印 Markdown
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import analyze as engine
from . import eastmoney, feishu, ledger, render, watchlist

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
    p.add_argument("--no-watch", action="store_true", help="不更新未收录席位观察池")
    p.add_argument("--skip-if-stale", action="store_true",
                   help="工作日但取不到当日数据时跳过（用于定时任务避开休市日）")
    p.add_argument("--wait-minutes", type=int, default=0,
                   help="当日龙虎榜还没披露时，最多等待多少分钟再重试（定时档用）")
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


def _resolve_trade_date(args: argparse.Namespace) -> str:
    """确定要出报的交易日；定时档在数据未披露时在预算内等待。

    龙虎榜披露时间不固定（通常 17:30-18:00，偶尔更晚）。19:16 这一发如果碰上
    尚未披露就直接放弃，当天就没有报告了——而 cron 一天只有这一发。
    这里改成在超时预算内轮询，比直接跳过稳。
    """
    if args.date:
        return args.date
    td = eastmoney.latest_trade_date()
    budget = max(0, args.wait_minutes) * 60
    if not budget:
        return td
    today = datetime.now().strftime("%Y-%m-%d")
    deadline = time.time() + budget
    while td != today and time.time() < deadline:
        remain = int(deadline - time.time())
        nap = min(180, max(30, remain))
        logging.info("当日龙虎榜尚未披露（最近交易日 %s），%s 秒后重试", td, nap)
        time.sleep(nap)
        try:
            td = eastmoney.latest_trade_date()
        except Exception as e:  # noqa: BLE001
            logging.warning("重试查询交易日失败: %s", e)
    return td


def run(args: argparse.Namespace) -> int:
    td = _resolve_trade_date(args)
    now = datetime.now()
    if args.skip_if_stale and not args.date and td != now.strftime("%Y-%m-%d") and now.weekday() < 5:
        logging.info("今日 %s 无新龙虎榜数据（最近交易日 %s），判定为休市，跳过推送",
                     now.strftime("%Y-%m-%d"), td)
        return 0

    # 已推过的交易日直接结束：Worker 准点派发与仓库 cron 兜底会各触发一次，
    # 第二次没必要把几千行数据重抓一遍（真正推送前还有一次复查，防并发窗口）
    if not (args.dry_run or args.no_push):
        done = ledger.pushed(td)
        if done and not args.force:
            logging.info("⏭ %s 已于 %s 推送成功，本次直接结束（要重推加 --force）",
                         td, done.get("pushed_at"))
            return 0

    logging.info("===== 游资动向 %s =====", td)

    snapshot = eastmoney.fetch_snapshot(td, use_cache=not args.no_cache)
    result = engine.analyze(snapshot, prev_stocks=_prev_stocks(td))

    # 观察池：只累积未收录席位的上榜轨迹，不动 tier、不改统计口径。
    # 失败不能拖垮主流程——它是辅助信息，不是报告的必要条件。
    if not args.no_watch:
        try:
            pool, newly = watchlist.merge(
                watchlist.load(), td, result.get("other_seats") or [], watchlist.trade_days(td))
            watchlist.annotate(result, pool, td, newly)
            watchlist.save(pool)
            w = result["watch"]
            logging.info("观察池 %s 条｜待确认候选 %s 家（今日新晋 %s）",
                         w["pool_size"], w["candidate_total"], w["new_count"])
            for c in w["candidates"]:
                if c.get("is_new"):
                    logging.info("  ⚑ %s 累计上榜 %s 日、净买入 %s，确认后加入 "
                                 "data/youzi_seats.json", c["alias"], c["days"],
                                 render.fmt_amt(c["net_total"]))
        except Exception as e:  # noqa: BLE001
            logging.warning("观察池更新失败（不影响报告）: %s", e)

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
    # 按真正发出去的 JSON 序列化算体积（str(dict) 的引号/空格不同，会低估或高估）
    size = len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    logging.info("飞书卡片 %s 字节", size)

    if args.dry_run or args.no_push:
        logging.info("已跳过推送。卡片预览：")
        logging.info(render.to_text(result))
        return 0

    # 再查一次台账：上面那次是「省算力」，这次是「防并发」——
    # Worker 与仓库 cron 若几乎同时启动，两个进程都可能在对方写台账之前通过第一次检查
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
