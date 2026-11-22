"""席位识别：把营业部全名映射成市场俗称与资金类型。"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

SEATS_FILE = Path(__file__).resolve().parent.parent / "data" / "youzi_seats.json"

# tier 排序：数字越小越靠前（用于报告里挑重点）
TIER_ORDER = {
    "top": 0,      # 一线游资
    "hot": 1,      # 活跃游资
    "quant": 2,    # 量化席位
    "foreign": 3,  # 外资
    "north": 4,    # 北向
    "inst": 5,     # 机构专用
    "retail": 6,   # 散户大本营
    "other": 7,    # 未收录
}

# 判断是否属于"游资类"资金（用于游资总览口径）
YOUZI_TIERS = {"top", "hot", "quant", "foreign"}

_NOISE = re.compile(
    r"(证券股份有限公司|证券有限责任公司|股份有限公司|有限责任公司|有限公司|证券营业部|营业部|分公司|证券)"
)


def _short(name: str) -> str:
    """把营业部全名压缩成易读简称。"""
    s = _NOISE.sub("", name or "")
    s = s.replace("（", "(").replace("）", ")")
    return s or (name or "未知席位")


@lru_cache(maxsize=1)
def load_rules() -> list[dict]:
    raw = json.loads(SEATS_FILE.read_text(encoding="utf-8"))
    return raw.get("rules", [])


def identify(seat_name: str) -> dict:
    """识别席位，返回 alias / tier / tags / matched。未命中时 alias 为简称。"""
    name = seat_name or ""
    for rule in load_rules():
        orgs = rule.get("orgs") or []
        if orgs and not any(o in name for o in orgs):
            continue
        kws = rule.get("match") or []
        if not any(k in name for k in kws):
            continue
        return {
            "alias": rule["alias"],
            "tier": rule.get("tier", "other"),
            "tags": rule.get("tags") or [],
            "rule_id": rule.get("id"),
            "matched": True,
        }
    return {"alias": _short(name), "tier": "other", "tags": [], "rule_id": None, "matched": False}


def is_youzi(info: dict) -> bool:
    return info["tier"] in YOUZI_TIERS


def is_hot_money(info: dict) -> bool:
    """一线 + 活跃游资（不含量化、外资）。"""
    return info["tier"] in {"top", "hot"}


def tier_label(tier: str) -> str:
    return {
        "top": "一线游资",
        "hot": "活跃游资",
        "quant": "量化席位",
        "foreign": "外资",
        "north": "北向",
        "inst": "机构",
        "retail": "散户",
        "other": "其他",
    }.get(tier, "其他")


def short_name(seat_name: str) -> str:
    return _short(seat_name)
