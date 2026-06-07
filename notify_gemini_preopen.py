#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""讀 ai_factors_gemini.json，發一則「盤前 Gemini 判讀」Telegram。只用標準庫。
由 gemini_preopen.yml 在產生盤前因子後觸發；token 只待在 GitHub secret。"""
import os
import re
import sys
import json
import urllib.request

import subscribers

F = "ai_factors_gemini.json"
PANEL_URL = "https://afreiv3.github.io/00631L_Tw/dashboard.html"
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")


def chat_ids():
    return subscribers.all_chat_ids(TG_CHAT)


def send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[error] 缺 TG_TOKEN / TG_CHAT", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for cid in chat_ids():
        payload = json.dumps({"chat_id": cid, "text": text,
                              "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20)
        except Exception as e:
            print(f"[error] Telegram 失敗 chat={cid}: {e}", file=sys.stderr)


def main():
    try:
        with open(F, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        print(f"[error] 讀不到 {F}: {e}", file=sys.stderr)
        return
    fa = d.get("factors", {})
    text = (f"☀️ [Gemini 盤前判讀] {d.get('date', '')}\n"
            f"AI情緒指數 {d.get('ai_sentiment_index', '—')}/100"
            f"（板塊{fa.get('sector', '—')}/系統{fa.get('macro', '—')}/結構{fa.get('trend', '—')}）\n"
            f"{d.get('summary', '')}\n"
            f"📊 完整 → {PANEL_URL}")
    send(text)
    print(f"已推播 Gemini 盤前 {d.get('date')}")


if __name__ == "__main__":
    main()
