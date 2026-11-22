"""报告渲染：Markdown 归档 + 飞书交互卡片。

配色遵循 A 股习惯：上涨/买入用红，下跌/卖出用绿。
"""
from __future__ import annotations

from datetime import datetime

RED = "#d54941"
GREEN = "#2ea121"
GREY = "#8f959e"


def fmt_amt(v: float, sign: bool = True) -> str:
    """金额格式化：亿/万自动切换。"""
    if v is None:
        return "-"
    neg = v < 0
    a = abs(v)
    if a >= 1e8:
        s = f"{a / 1e8:.2f}亿"
    elif a >= 1e4:
        s = f"{a / 1e4:.0f}万"
    else:
        s = f"{a:.0f}"
    if sign:
        return ("-" if neg else "+") + s
    return s


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def _mk(prefix: str, text: str) -> str:
    """飞书 markdown 着色。"""
    return f"<font color='{prefix}'>{text}</font>"


def _feishu_amt(v: float) -> str:
    return _mk("red" if v > 0 else "green", fmt_amt(v))


CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
          "十一", "十二", "十三", "十四", "十五", "十六"]


class _Counter:
    """按实际输出的章节递增编号，避免条件章节缺失导致跳号。"""

    def __init__(self) -> None:
        self.i = 0

    def next(self) -> str:
        self.i += 1
        return CN_NUM[min(self.i, len(CN_NUM)) - 1]


def _tag_text(node: dict) -> str:
    tags = [t for t in (node.get("tags") or []) if t and t != "一线游资"]
    if node.get("tier") == "top":
        tags = ["一线"] + tags[:1]
    return "·".join(tags[:2])


def _stock_brief(stocks: list[dict], limit: int = 3, colored: bool = False) -> str:
    out = []
    for s in stocks[:limit]:
        name = s.get("name") or s.get("code")
        chg = s.get("change", 0)
        if colored:
            c = "red" if chg > 0 else "green"
            out.append(f"{name}{_mk(c, f'{chg:+.1f}%')}")
        else:
            out.append(f"{name}({chg:+.1f}%)")
    return " ".join(out)


# ---------------------------------------------------------------- Markdown

def to_markdown(r: dict) -> str:
    m = r["market"]
    td = r["trade_date"]
    L: list[str] = []
    A = L.append
    sec = _Counter()

    A(f"# A 股游资动向日报 · {td}")
    A("")
    A(f"> 数据源：东方财富龙虎榜 · 生成于 {datetime.now():%Y-%m-%d %H:%M}")
    A("> 口径：已剔除可转债；同一股票多个上榜原因按单日口径去重；金额为前五席位数据")
    A("")

    A(f"## {sec.next()}、资金全景")
    A("")
    A("| 资金类型 | 净额 | 说明 |")
    A("| --- | ---: | --- |")
    A(f"| 龙虎榜前五席位合计 | {fmt_amt(m['total_net'])} | 全部上榜席位净额 |")
    A(f"| 知名游资合计 | {fmt_amt(m['youzi_net'])} | 占全榜买入额 {m['known_buy_ratio'] * 100:.0f}% |")
    A(f"| ├ 一线游资 | {fmt_amt(m['top_net'])} | 陈小群/作手新一/孙哥等 |")
    A(f"| ├ 活跃游资 | {fmt_amt(m['youzi_net'] - m['top_net'] - m['quant_net'] - _foreign(r))} | 有俗称的活跃席位 |")
    A(f"| ├ 量化席位 | {fmt_amt(m['quant_net'])} | 开源五鬼/券商总部等 |")
    A(f"| └ 外资 | {fmt_amt(_foreign(r))} | 高盛/摩根/瑞银等 |")
    A(f"| 机构专用 | {fmt_amt(m['inst_net'])} | 公募私募险资汇总 |")
    A(f"| 北向资金 | {fmt_amt(m['north_net'])} | 沪深股通 |")
    A(f"| 散户大本营 | {fmt_amt(m['retail_net'])} | 拉萨天团等 |")
    A(f"| 其他营业部 | {fmt_amt(m['other_net'])} | 未收录席位 |")
    A("")
    A(f"- 上榜个股 **{m['stock_count']}** 只，游资净买入 **{m['youzi_in_stocks']}** 只、"
      f"净卖出 **{m['youzi_out_stocks']}** 只")
    A(f"- 全市场涨停 **{m['limit_up_count']}** 只，其中游资净买入 **{m['limit_up_youzi']}** 只")
    A("")

    A(f"## {sec.next()}、游资加仓榜")
    A("")
    A("| # | 游资 | 类型 | 净额 | 股票数 | 主攻方向 |")
    A("| ---: | --- | --- | ---: | ---: | --- |")
    for i, n in enumerate(r["top_buy_seats"], 1):
        if n["net"] <= 0:
            continue
        A(f"| {i} | {n['alias']} | {_tag_text(n)} | {fmt_amt(n['net'])} | {n['stock_count']} | "
          f"{_stock_brief(n['stocks'])} |")
    A("")

    if r["huddle"]:
        A(f"## {sec.next()}、抱团合力榜")
        A("")
        A("多路游资同时净买入的个股，家数越多说明分歧越小。")
        A("")
        A("| 股票 | 涨幅 | 游资净额 | 买家数 | 参与游资 |")
        A("| --- | ---: | ---: | ---: | --- |")
        for s in r["huddle"]:
            A(f"| {s['name']}({s['code']}) | {fmt_pct(s['change'])} | {fmt_amt(s['youzi_net'])} | "
              f"{s['in_count']} | {', '.join(b['alias'] for b in s['buyers'][:5])} |")
        A("")

    if r["top_sell_seats"]:
        A(f"## {sec.next()}、游资出货榜")
        A("")
        A("| # | 游资 | 类型 | 净额 | 出货标的 |")
        A("| ---: | --- | --- | ---: | --- |")
        for i, n in enumerate(r["top_sell_seats"], 1):
            A(f"| {i} | {n['alias']} | {_tag_text(n)} | {fmt_amt(n['net'])} | "
              f"{_stock_brief(n['stocks'])} |")
        A("")

    limbo = r.get("limbo") or []
    if limbo:
        A(f"## {sec.next()}、跌停板接货")
        A("")
        A("游资在跌停板上逆势买入，通常是博次日反弹或撬板，风险与弹性都高。")
        A("")
        A("| 股票 | 涨幅 | 游资净额 | 买家 |")
        A("| --- | ---: | ---: | --- |")
        for s in sorted(limbo, key=lambda x: -x["youzi_net"])[:10]:
            A(f"| {s['name']}({s['code']}) | {fmt_pct(s['change'])} | {fmt_amt(s['youzi_net'])} | "
              f"{', '.join(b['alias'] for b in s['buyers'][:4])} |")
        A("")

    if r["divergence"]:
        A(f"## {sec.next()}、机构与游资分歧")
        A("")
        A("| 股票 | 涨幅 | 游资 | 机构 | 北向 | 解读 |")
        A("| --- | ---: | ---: | ---: | ---: | --- |")
        for s in r["divergence"]:
            if s["inst_net"] > 0 and s["youzi_net"] < 0:
                note = "机构接货、游资撤退"
            elif s["inst_net"] < 0 and s["youzi_net"] > 0:
                note = "游资进攻、机构卖出"
            else:
                note = "-"
            A(f"| {s['name']} | {fmt_pct(s['change'])} | {fmt_amt(s['youzi_net'])} | "
              f"{fmt_amt(s['inst_net'])} | {fmt_amt(s['north_net'])} | {note} |")
        A("")

    if r["sectors"]:
        A(f"## {sec.next()}、板块资金流向")
        A("")
        A("| 板块 | 游资净额 | 上榜数 | 龙头 |")
        A("| --- | ---: | ---: | --- |")
        for s in r["sectors"][:10]:
            if abs(s["youzi_net"]) < 1e7:
                continue
            lead = s["stocks"][0] if s["stocks"] else None
            lead_txt = f"{lead['name']}({fmt_amt(lead['net'])})" if lead else "-"
            A(f"| {s['industry']} | {fmt_amt(s['youzi_net'])} | {s['count']} | {lead_txt} |")
        A("")

    lad = r["ladder"]
    if lad["tiers"]:
        A(f"## {sec.next()}、连板梯队")
        A("")
        A(f"涨停 **{lad['total']}** 只，最高 **{lad['max_board']}** 板，炸板 **{lad['broken']}** 只")
        A("")
        for t in lad["tiers"]:
            if t["board"] < 2:
                continue
            names = "、".join(f"{x['name']}({x['industry']})" for x in t["stocks"][:12])
            A(f"- **{t['board']} 板**（{t['count']} 只）：{names}")
        A("")

    if r["quant_seats"]:
        A(f"## {sec.next()}、量化席位动向")
        A("")
        A("| 席位 | 净额 | 股票数 | 标的 |")
        A("| --- | ---: | ---: | --- |")
        for n in r["quant_seats"]:
            A(f"| {n['alias']} | {fmt_amt(n['net'])} | {n['stock_count']} | "
              f"{_stock_brief(n['stocks'])} |")
        A("")

    if r["unknown_seats"]:
        A(f"## {sec.next()}、未收录席位大额买入")
        A("")
        A("尚未进入别名库的营业部，往往是还没被命名的游资，值得持续跟踪。")
        A("")
        A("| 席位 | 净额 | 股票数 | 标的 |")
        A("| --- | ---: | ---: | --- |")
        for n in r["unknown_seats"]:
            if n["net"] <= 0:
                continue
            A(f"| {n['alias']} | {fmt_amt(n['net'])} | {n['stock_count']} | "
              f"{_stock_brief(n['stocks'])} |")
        A("")

    c = r.get("continuity") or {}
    if c.get("available"):
        A(f"## {sec.next()}、连续性追踪")
        A("")
        A(f"基于最近 {c['days']} 个交易日的历史快照。")
        A("")
        if c.get("persistent_seats"):
            A("**持续加仓席位**")
            A("")
            A("| 游资 | 今日净额 | 历史累计 | 连续天数 |")
            A("| --- | ---: | ---: | ---: |")
            for n in c["persistent_seats"]:
                A(f"| {n['alias']} | {fmt_amt(n['net'])} | {fmt_amt(n['total_net'])} | {n['days']} |")
            A("")
        if c.get("persistent_stocks"):
            A("**持续被游资买入的个股**")
            A("")
            A("| 股票 | 今日净额 | 累计净额 | 连续天数 |")
            A("| --- | ---: | ---: | ---: |")
            for s in c["persistent_stocks"]:
                A(f"| {s['name']}({s['code']}) | {fmt_amt(s['today_net'])} | "
                  f"{fmt_amt(s['total_net'])} | {s['days']} |")
            A("")
        if c.get("fresh_seats"):
            A("**今日新进大额席位**：" + "、".join(
                f"{n['alias']}({fmt_amt(n['net'])})" for n in c["fresh_seats"]))
            A("")

    pe = r.get("prev_effect")
    if pe:
        A(f"## {sec.next()}、赚钱效应")
        A("")
        A(f"上一交易日上榜股今日平均涨幅 **{pe['avg']:+.2f}%**，上涨占比 **{pe['up_ratio'] * 100:.0f}%**，"
          f"其中涨停 **{pe['limit_up']}** 只、跌超 5% **{pe['down5']}** 只（样本 {pe['count']} 只）")
        A("")

    A("---")
    A("")
    A("> 席位别名为市场公开流传的俗称，仅用于把同源资金聚合归类，不代表对具体投资者的身份指认。")
    A("> 龙虎榜为盘后披露数据，天然滞后一个交易日，不构成任何投资建议。")
    return "\n".join(L)


def _foreign(r: dict) -> float:
    return sum(n["net"] for n in r["seat_agg"].values() if n["tier"] == "foreign")


# ---------------------------------------------------------------- 飞书

def to_feishu_card(r: dict) -> dict:
    m = r["market"]
    td = r["trade_date"]
    net = m["total_net"]
    head_tpl = "red" if net >= 0 else "green"

    def div(text: str) -> dict:
        return {"tag": "div", "text": {"tag": "lark_md", "content": text}}

    els: list[dict] = []

    # 资金全景
    els.append(div(
        f"**全榜净额** {_feishu_amt(net)}　**知名游资** {_feishu_amt(m['youzi_net'])}"
        f"（占买入额 {m['known_buy_ratio'] * 100:.0f}%）\n"
        f"一线 {_feishu_amt(m['top_net'])}　量化 {_feishu_amt(m['quant_net'])}　"
        f"外资 {_feishu_amt(_foreign(r))}\n"
        f"机构 {_feishu_amt(m['inst_net'])}　北向 {_feishu_amt(m['north_net'])}　"
        f"散户 {_feishu_amt(m['retail_net'])}"
    ))
    els.append({"tag": "hr"})
    els.append(div(
        f"上榜 **{m['stock_count']}** 只　游资净买入 **{m['youzi_in_stocks']}** 只　"
        f"净卖出 **{m['youzi_out_stocks']}** 只　涨停 **{m['limit_up_count']}** 只"
    ))
    els.append({"tag": "hr"})

    # 游资加仓榜
    lines = ["**🔥 游资加仓榜**"]
    rank = 0
    for n in r["top_buy_seats"]:
        if n["net"] <= 0 or rank >= 6:
            break
        rank += 1
        stocks = "、".join(f"{s['name']}" for s in n["stocks"][:3])
        tag = _tag_text(n)
        lines.append(f"{rank}. **{n['alias']}**{f'（{tag}）' if tag else ''} "
                     f"{_feishu_amt(n['net'])}　{n['stock_count']}只\n　　{stocks}")
    els.append(div("\n".join(lines)))
    els.append({"tag": "hr"})

    # 抱团
    if r["huddle"]:
        lines = ["**🤝 抱团合力**（多路资金同时买入）"]
        for s in r["huddle"][:3]:
            c = "red" if s["change"] > 0 else "green"
            lines.append(f"· **{s['name']}** {_mk(c, fmt_pct(s['change']))} "
                         f"游资 {_feishu_amt(s['youzi_net'])}　{s['in_count']}家\n"
                         f"　{', '.join(b['alias'] for b in s['buyers'][:4])}")
        els.append(div("\n".join(lines)))
        els.append({"tag": "hr"})

    # 出货
    sells = [n for n in r["top_sell_seats"] if n["net"] < 0][:3]
    if sells:
        lines = ["**⚠️ 游资出货**"]
        for n in sells:
            lines.append(f"· **{n['alias']}** {_feishu_amt(n['net'])}　"
                         f"{'、'.join(s['name'] for s in n['stocks'][:3])}")
        els.append(div("\n".join(lines)))
        els.append({"tag": "hr"})

    # 连板梯队
    lad = r["ladder"]
    if lad["tiers"]:
        parts = [f"**{t['board']}板** {t['count']}只" for t in lad["tiers"] if t["board"] >= 2][:4]
        top = lad["tiers"][0]["stocks"][0] if lad["tiers"] else None
        txt = f"**📈 连板梯队**　涨停 {lad['total']} 只，最高 {lad['max_board']} 板"
        if parts:
            txt += "\n" + "　".join(parts)
        if top:
            txt += f"\n龙头：**{top['name']}**（{top['industry']}）"
        els.append(div(txt))
        els.append({"tag": "hr"})

    # 板块
    sectors = [s for s in r["sectors"] if abs(s["youzi_net"]) >= 1e7][:3]
    if sectors:
        lines = ["**🧭 板块资金**"]
        for s in sectors:
            lines.append(f"· {s['industry']} {_feishu_amt(s['youzi_net'])}（{s['count']}只）")
        els.append(div("\n".join(lines)))
        els.append({"tag": "hr"})

    # 连续性
    c = r.get("continuity") or {}
    if c.get("available") and (c.get("persistent_stocks") or c.get("persistent_seats")):
        lines = [f"**🔁 连续追踪**（近{c['days'] + 1}日）"]
        for s in (c.get("persistent_stocks") or [])[:3]:
            lines.append(f"· {s['name']} 连续{s['days']}日被游资买入 "
                         f"累计 {_feishu_amt(s['total_net'])}")
        for n in (c.get("persistent_seats") or [])[:2]:
            lines.append(f"· {n['alias']} 连续{n['days']}日加仓 "
                         f"累计 {_feishu_amt(n['total_net'])}")
        els.append(div("\n".join(lines)))
        els.append({"tag": "hr"})

    els.append(div("<font color='grey'>龙虎榜盘后披露，滞后一个交易日；席位别名为市场俗称，"
                   "仅用于资金归类，不构成投资建议。</font>"))

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"游资动向 · {td}"},
                "subtitle": {"tag": "plain_text",
                             "content": f"净流入 {fmt_amt(net)}　游资占买入 {m['known_buy_ratio'] * 100:.0f}%"},
                "template": head_tpl,
            },
            "elements": els,
        },
    }


def to_text(r: dict) -> str:
    """纯文本兜底，用于飞书 post 或日志输出。"""
    m = r["market"]
    lines = [f"游资动向 {r['trade_date']}",
             f"全榜净额 {fmt_amt(m['total_net'])} / 知名游资 {fmt_amt(m['youzi_net'])}",
             "加仓榜：" + "，".join(
                 f"{n['alias']}{fmt_amt(n['net'])}" for n in r["top_buy_seats"][:5] if n["net"] > 0)]
    return "\n".join(lines)
