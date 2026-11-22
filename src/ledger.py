"""推送台账：同一交易日只推一次。

Cloudflare Worker 会准点派发 workflow，GitHub 自带的 cron 作为兜底也会
在同一时刻前后触发一次。没有台账就会往群里推两张一模一样的卡片。

台账按「数据日期」判重，而不是按运行时间——同一天的第二次运行会把报告
和快照重新算一遍（无害），但不会再推一次。要强制重推，手动 Run workflow
勾 force，或本地加 --force。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "state" / "push_ledger.json"

KEEP_RECORDS = 60  # 只留最近 60 个交易日，避免文件无限增长

log = logging.getLogger(__name__)


def _load() -> dict:
    try:
        raw = LEDGER_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        log.warning("台账读取失败，按空台账处理: %s", e)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("台账内容损坏，按空台账处理: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def pushed(trade_date: str) -> dict | None:
    """该交易日是否已成功推送过；返回台账记录（含 pushed_at）或 None。"""
    rec = _load().get(str(trade_date))
    return rec if isinstance(rec, dict) else None


def record(trade_date: str, **info) -> None:
    data = _load()
    data[str(trade_date)] = {
        "pushed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **info,
    }
    if len(data) > KEEP_RECORDS:
        for key in sorted(data)[:-KEEP_RECORDS]:
            data.pop(key, None)
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
