#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
給「盤前/盤後 AI 判讀」用的取數助手。
重用 monitor.py 的抓數工具，輸出一包 JSON 到 stdout，讓雲端 Claude 代理
結合它自己 web search 查到的新聞/事件，對「板塊/系統風險/大盤結構」三項評分。

用法：python fetch_quant.py
只用標準庫（透過 import monitor）。
"""
import json
import datetime as dt
import monitor as m


def taipei_now():
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)


def phase(now):
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return "closed"
    if hm < 9 * 60:
        return "pre-open"
    if hm <= 13 * 60 + 30:
        return "intraday"
    return "post-close"


def main():
    now = taipei_now()
    ma60, twii = m.ma("^TWII", 60)
    data = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "phase": phase(now),
        "symbol": m.SYMBOL,
        "intraday": m.intraday(m.SYMBOL),       # open/current/high/low/prev_close/vwap/frac/cum_vol 或 None
        "avg20_vol": m.avg_daily_volume(m.SYMBOL),
        "sox_pct": m.daily_change_pct("^SOX"),  # 隔夜費半
        "ndx_pct": m.daily_change_pct("^IXIC"),  # 隔夜 Nasdaq
        "tsm_pct": m.daily_change_pct("TSM"),   # 台積電 ADR
        "wti_pct": m.daily_change_pct("CL=F"),  # 油價
        "tnx_pct": m.daily_change_pct("^TNX"),  # 美債 10Y 殖利率(×10 點數)的%變化
        "twii": twii,
        "twii_ma60": ma60,
    }
    print(json.dumps(data, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
