#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""讀 judgment_log.json 最新一筆 AI 判讀，發一則 Telegram。
由 .github/workflows/notify.yml 在雲端 AI 代理 push 後觸發。只用標準庫。"""
import os
import re
import sys
import json
import urllib.request

import subscribers

LOG = "judgment_log.json"
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
        with open(LOG, "r", encoding="utf-8") as f:
            hist = json.load(f).get("history", [])
    except Exception as e:
        print(f"[error] 讀不到 {LOG}: {e}", file=sys.stderr)
        return
    if not hist:
        print("judgment_log 無紀錄，略過")
        return
    r = hist[-1]
    head = "☀️ [盤前 AI 分析]" if r.get("phase") == "pre-open" else "🌙 [盤後總結]"
    zlabel = {"green": "🟢", "amber": "🟡", "red": "🔴"}.get(r.get("zone"), "")
    score = r.get("score")
    score_str = f"{zlabel} 環境分 {score}/100" if score is not None else ""
    # 精簡：只放一句總結＋分數，完整七項與新聞看面板
    text = (f"{head} {r.get('date','')} {r.get('time','')}\n"
            f"{score_str}\n"
            f"{r.get('summary','')}\n"
            f"📊 完整分析與新聞 → {PANEL_URL}")
    send(text)
    print(f"已推播 {r.get('date')} {r.get('time')} phase={r.get('phase')}")


if __name__ == "__main__":
    main()
