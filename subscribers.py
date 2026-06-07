#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推播收件人名單：合併 TG_CHAT（環境變數，你本人）＋ subscribers.json（自動訂閱者）。
subscribers.json 由 Vercel webhook 在使用者 /start 時寫入。各推播腳本共用此模組。"""
import re
import json

SUBS_FILE = "subscribers.json"


def _file_ids():
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            return [str(c).strip() for c in json.load(f).get("chat_ids", []) if str(c).strip()]
    except Exception:
        return []


def all_chat_ids(tg_chat_env):
    """回傳去重後的 chat_id 清單（保持順序）：TG_CHAT 在前、訂閱者在後。"""
    env = [c.strip() for c in re.split(r"[,\s;]+", tg_chat_env or "") if c.strip()]
    return list(dict.fromkeys(env + _file_ids()))
