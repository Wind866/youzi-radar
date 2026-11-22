"""东方财富数据抓取层。

只依赖 requests，全部为公开接口：
  - 龙虎榜个股汇总   RPT_DAILYBILLBOARD_DETAILSNEW
  - 龙虎榜席位明细   RPT_OPERATEDEPT_TRADE_DETAILS
  - 涨停股池         push2ex getTopicZTPool
  - 个股行业          push2 ulist.np (f100)
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
PUSH2 = "https://push2.eastmoney.com/api/qt/ulist.np/get"
ZT_POOL = "https://push2ex.eastmoney.com/getTopicZTPool"
F10 = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
INDUSTRY_CACHE = Path(__file__).resolve().parent.parent / "data" / "industry_cache.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
    "Accept": "*/*",
}

_session = requests.Session()
_session.headers.update(HEADERS)


def _request(url: str, params: dict, tries: int = 5, timeout: int = 25) -> dict | None:
    """带指数退避的 GET，失败返回 None。"""
    for i in range(tries):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            wait = min(2**i + random.random(), 15)
            log.warning("请求失败(%s/%s) %s: %s", i + 1, tries, url.split("/")[-1], e)
            time.sleep(wait)
    return None


def _datacenter(report: str, flt: str, sort: str = "", sort_type: str = "-1",
                page_size: int = 500, max_pages: int = 40) -> list[dict]:
    """分页拉取某个报表的全部数据。"""
    rows: list[dict] = []
    page = 1
    while page <= max_pages:
        params = {
            "reportName": report,
            "columns": "ALL",
            "pageNumber": page,
            "pageSize": page_size,
            "source": "WEB",
            "client": "WEB",
            "filter": flt,
        }
        if sort:
            params["sortColumns"] = sort
            params["sortTypes"] = sort_type
        data = _request(DATACENTER, params)
        if not data or not data.get("success"):
            if page == 1:
                log.error("报表 %s 查询失败: %s", report, (data or {}).get("message"))
            break
        result = data.get("result") or {}
        batch = result.get("data") or []
        rows.extend(batch)
        pages = result.get("pages") or 1
        if page >= pages or not batch:
            break
        page += 1
        time.sleep(0.25)
    return rows


# ---------------------------------------------------------------- 交易日

def latest_trade_date(ref: date | None = None, max_back: int = 15) -> str:
    """返回 <= ref 的最近一个有龙虎榜数据的交易日，格式 YYYY-MM-DD。"""
    ref = ref or date.today()
    end = ref.strftime("%Y-%m-%d")
    start = (ref - timedelta(days=max_back)).strftime("%Y-%m-%d")
    rows = _datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        flt=f"(TRADE_DATE<='{end}')(TRADE_DATE>='{start}')",
        sort="TRADE_DATE",
        sort_type="-1",
        page_size=1,
        max_pages=1,
    )
    if rows:
        return str(rows[0]["TRADE_DATE"])[:10]
    raise RuntimeError(f"{start} ~ {end} 区间内取不到龙虎榜数据")


# ---------------------------------------------------------------- 龙虎榜

def billboard_stocks(trade_date: str) -> list[dict]:
    """当日龙虎榜个股。同一只股票可能因多个上榜原因出现多条，此处保留原始记录。"""
    return _datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        flt=f"(TRADE_DATE='{trade_date}')",
        sort="BILLBOARD_NET_AMT",
        sort_type="-1",
    )


def seat_trades(trade_date: str) -> list[dict]:
    """当日全部营业部席位买卖明细，一条 = 某席位在某只股票上的一次上榜。"""
    return _datacenter(
        "RPT_OPERATEDEPT_TRADE_DETAILS",
        flt=f"(TRADE_DATE='{trade_date}')",
        sort="NET_AMT",
        sort_type="-1",
    )


def _scope_rank(explanation: str | None) -> int:
    """0=单日口径，1=多日累计口径。两种口径的金额不可相加。"""
    return 1 if (explanation and "连续" in explanation) else 0


def seat_detail(trade_date: str, name_map: dict[str, str] | None = None) -> list[dict]:
    """完整的龙虎榜席位明细（含机构专用、沪深股通）。

    合并买入前5表与卖出前5表后做两步清洗：
      1. 同一只股票只保留单日口径的上榜原因，避免与"连续三日"口径重复计算；
      2. 同一席位同一股票去重（两张表重复收录）。机构专用席位编码恒为 '0'，
         同股多条代表不同机构，改用买卖金额作为区分键。
    """
    name_map = name_map or {}
    rows: list[dict] = []
    for report in ("RPT_BILLBOARD_DAILYDETAILSBUY", "RPT_BILLBOARD_DAILYDETAILSSELL"):
        rows.extend(_datacenter(report, flt=f"(TRADE_DATE='{trade_date}')",
                               sort="NET", sort_type="-1"))
    if not rows:
        return []

    allowed: dict[str, set] = {}
    for r in rows:
        code = str(r.get("SECURITY_CODE"))
        allowed.setdefault(code, set())
        if _scope_rank(r.get("EXPLANATION")) == 0:
            allowed[code].add(r.get("EXPLANATION"))

    seen: dict[tuple, dict] = {}
    for r in rows:
        code = str(r.get("SECURITY_CODE"))
        allowed_set = allowed.get(code) or set()
        if allowed_set and r.get("EXPLANATION") not in allowed_set:
            continue
        dept_code = str(r.get("OPERATEDEPT_CODE") or "")
        if dept_code and dept_code != "0":
            key = (code, "D", dept_code)
        else:
            key = (code, "I", str(r.get("OPERATEDEPT_NAME")),
                   round(_num(r.get("BUY")), 2), round(_num(r.get("SELL")), 2))
        if key in seen:
            continue
        seen[key] = {
            "OPERATEDEPT_CODE": r.get("OPERATEDEPT_CODE"),
            "OPERATEDEPT_NAME": r.get("OPERATEDEPT_NAME") or "",
            "SECURITY_CODE": code,
            "SECURITY_NAME_ABBR": name_map.get(code, ""),
            "ACT_BUY": _num(r.get("BUY")),
            "ACT_SELL": _num(r.get("SELL")),
            "NET_AMT": _num(r.get("NET")),
            "CHANGE_RATE": _num(r.get("CHANGE_RATE")),
            "EXPLANATION": r.get("EXPLANATION") or "",
            "RISE_PROB_3D": r.get("RISE_PROBABILITY_3DAY"),
            "ACTIVE_3D": r.get("TOTAL_BUYER_SALESTIMES_3DAY"),
        }
    return list(seen.values())


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- 涨停池

def limit_up_pool(trade_date: str) -> list[dict]:
    """涨停股池，含连板数、封板时间、炸板次数、所属行业。"""
    ymd = trade_date.replace("-", "")
    data = _request(
        ZT_POOL,
        {
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt",
            "Pageindex": 0,
            "pagesize": 600,
            "sort": "fbt:asc",
            "date": ymd,
        },
    )
    if not data or not data.get("data"):
        log.warning("涨停池为空: %s", trade_date)
        return []
    return data["data"].get("pool") or []


# ---------------------------------------------------------------- 行业

def _secid(code: str) -> str:
    code = str(code)
    if code.startswith(("60", "68", "50", "51", "58", "56", "11", "90")):
        return f"1.{code}"
    return f"0.{code}"


def _industry_batch(codes: list[str]) -> dict[str, str]:
    data = _request(
        PUSH2,
        {"fltt": 2, "secids": ",".join(_secid(c) for c in codes), "fields": "f12,f14,f100"},
        tries=2,
        timeout=12,
    )
    out: dict[str, str] = {}
    if not data or not data.get("data"):
        return out
    for item in data["data"].get("diff") or []:
        code = str(item.get("f12", ""))
        ind = item.get("f100")
        if code and ind and ind != "-":
            out[code] = str(ind)
    return out


def _load_industry_cache() -> dict[str, str]:
    if INDUSTRY_CACHE.exists():
        try:
            return json.loads(INDUSTRY_CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_industry_cache(cache: dict[str, str]) -> None:
    try:
        INDUSTRY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        INDUSTRY_CACHE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True),
            encoding="utf-8", newline="\n")
    except OSError as e:
        log.warning("行业缓存写入失败: %s", e)


def _f10_market(code: str) -> str:
    if code.startswith(("60", "68", "90", "11", "50", "51", "58")):
        return "SH"
    if code.startswith(("4", "8", "92")):
        return "BJ"
    return "SZ"


def _f10_industry(code: str) -> str | None:
    """F10 备用行业源。EM2016 形如「机械设备-通用设备-制冷空调设备」，取第二级与 f100 对齐。"""
    data = _request(F10, {"code": f"{_f10_market(code)}{code}"}, tries=2, timeout=10)
    if not data:
        return None
    for item in data.get("jbzl") or []:
        em = item.get("EM2016") or ""
        parts = [p for p in em.split("-") if p]
        if len(parts) >= 2:
            return parts[1]
    return None


def industries(codes: list[str], budget: int = 90, push_limit: int = 120) -> dict[str, str]:
    """code -> 行业名称。行业变化极慢，采用本地缓存累积。

    主源用 F10 的 EM2016 二级行业，保证全市场口径一致；F10 未覆盖的再用 push2 批量兜底。
    两个源都不可用时，个股的行业标签会缺，不影响报告其他部分产出。
    """
    cache = _load_industry_cache()
    need = [c for c in dict.fromkeys(str(x) for x in codes) if c and c not in cache]
    if not need:
        return {str(c): cache[str(c)] for c in codes if str(c) in cache}

    log.info("行业缓存命中 %s / 待补 %s", len(cache), len(need))
    start = time.time()
    fetched: dict[str, str] = {}

    for i, code in enumerate(need[:200]):
        if time.time() - start > budget:
            log.warning("行业补抓超时，本轮已获取 %s 个", len(fetched))
            break
        val = _f10_industry(code)
        if val:
            fetched[code] = val
        time.sleep(0.12)

    still = [c for c in need if c not in fetched][:push_limit]
    if still:
        log.info("F10 未覆盖 %s 只，改用 push2 兜底", len(still))
        for i in range(0, len(still), 30):
            if time.time() - start > budget * 1.4:
                break
            fetched.update(_industry_batch(still[i:i + 30]))
            time.sleep(0.2)

    if fetched:
        cache.update(fetched)
        _save_industry_cache(cache)
    if need and not fetched:
        log.warning("行业接口不可用，个股行业标签将缺失（已缓存的 %s 个仍可用）", len(cache))

    return {str(c): cache[str(c)] for c in codes if str(c) in cache}


# ---------------------------------------------------------------- 快照

def fetch_snapshot(trade_date: str | None = None, use_cache: bool = True) -> dict[str, Any]:
    """抓齐某交易日全部原始数据。本地调试默认读缓存。"""
    td = trade_date or latest_trade_date()
    cache_file = CACHE_DIR / f"{td}.json"
    if use_cache and cache_file.exists():
        log.info("读取本地缓存 %s", cache_file.name)
        return json.loads(cache_file.read_text(encoding="utf-8"))

    log.info("交易日: %s", td)

    stocks = billboard_stocks(td)
    if not stocks:
        raise RuntimeError(f"{td} 龙虎榜个股为空，可能尚未披露")

    name_map = {str(s["SECURITY_CODE"]): (s.get("SECURITY_NAME_ABBR") or "") for s in stocks}
    seats = seat_detail(td, name_map)
    zt = limit_up_pool(td)

    if not seats:
        raise RuntimeError(f"{td} 席位明细为空")

    codes = [s["SECURITY_CODE"] for s in stocks] + [z["c"] for z in zt if z.get("c")]
    ind = industries(codes)

    missing = [c for c in {str(s["SECURITY_CODE"]) for s in stocks} if c not in ind]
    if missing and len(missing) / max(len(stocks), 1) > 0.1:
        log.warning("行业缺失 %s 只，板块统计可能不完整", len(missing))

    log.info("个股 %s 条 / 席位 %s 条 / 涨停 %s 只 / 行业 %s 个",
             len(stocks), len(seats), len(zt), len(ind))

    snapshot = {
        "trade_date": td,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stocks": stocks,
        "seats": seats,
        "limit_up": zt,
        "industries": ind,
    }
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(snapshot, ensure_ascii=False),
                              encoding="utf-8", newline="\n")
    except OSError as e:
        log.warning("缓存写入失败: %s", e)
    return snapshot


def previous_trade_date(trade_date: str) -> str:
    """上一个交易日。"""
    ref = datetime.strptime(trade_date, "%Y-%m-%d").date() - timedelta(days=1)
    return latest_trade_date(ref)
