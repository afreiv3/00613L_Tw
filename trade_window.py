#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交易時段與頻率規則（波段操作版，兩套模擬盤共用）。

規則：
  1. 只在台股盤中（台北 09:00–13:30）成交——收盤後的 monitor 只更新分數/畫面，不下單。
  2. 一天最多一次交易動作——當天一旦進出場過，當日鎖住，不再有任何下單。
"""

MARKET_OPEN = 9 * 60        # 09:00
MARKET_CLOSE = 13 * 60 + 30  # 13:30


def in_market_hours(now_hm):
    """now_hm 格式 'HH:MM'。解析失敗則不擋（回 True）。"""
    try:
        h, m = now_hm.split(":")
        mins = int(h) * 60 + int(m)
    except Exception:
        return True
    return MARKET_OPEN <= mins <= MARKET_CLOSE


def can_trade(state, today, now_hm):
    """波段規則：須在盤中，且今天尚未交易過。"""
    if not in_market_hours(now_hm):
        return False
    return state.get("last_trade_date") != today
