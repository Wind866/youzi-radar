"""飞书自定义机器人推送，支持签名校验与多群分发。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

MAX_CARD_BYTES = 28_000  # 飞书卡片上限 30KB，留出余量


def _sign(secret: str) -> dict:
    ts = str(int(time.time()))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return {"timestamp": ts, "sign": base64.b64encode(digest).decode("utf-8")}


def _webhooks() -> list[str]:
    raw = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not raw:
        return []
    return [u.strip() for u in raw.replace("\n", ",").split(",") if u.strip()]


def _trim(payload: dict) -> dict:
    """卡片超限时成对删除尾部区块（div + 分隔线），保留头部与免责声明。"""
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= MAX_CARD_BYTES:
        return payload
    els = payload["card"]["elements"]
    while len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_CARD_BYTES:
        idx = [i for i, e in enumerate(els) if e.get("tag") == "hr"]
        if not idx or idx[-1] < 2:
            break
        last = idx[-1]
        del els[last - 1:last + 1]
    return payload


def send(card_payload: dict, dry_run: bool = False) -> bool | None:
    """推送到所有配置的飞书群。

    返回 True=全部成功，False=配了但推送失败，None=未配置（视为跳过，不算失败）。
    """
    urls = _webhooks()
    if not urls:
        log.warning("未配置 FEISHU_WEBHOOK，跳过推送（报告已在本地生成）")
        return None
    if dry_run:
        log.info("dry-run：跳过实际推送")
        return True

    secret = os.getenv("FEISHU_SECRET", "").strip()
    payload = _trim(card_payload)
    if secret:
        payload = {**_sign(secret), **payload}

    all_ok = True
    for url in urls:
        ok = False
        for attempt in range(3):
            try:
                resp = requests.post(url, json=payload, timeout=20)
                data = resp.json()
                code = data.get("code", data.get("StatusCode", -1))
                if code == 0:
                    ok = True
                    break
                log.error("飞书返回错误 code=%s msg=%s", code, data.get("msg"))
                # 关键词校验失败等业务错误重试无意义
                if code in (19021, 19024, 9499):
                    break
            except Exception as e:  # noqa: BLE001
                log.warning("飞书推送失败(%s/3): %s", attempt + 1, e)
                time.sleep(2 * (attempt + 1))
        if ok:
            log.info("飞书推送成功 %s", url[:48] + "...")
        else:
            all_ok = False
    return all_ok
