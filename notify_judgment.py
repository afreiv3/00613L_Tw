#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""讀 judgment_log.json 最新一筆 AI 判讀，發一則 Telegram。
由 .github/workflows/notify.yml 在雲端 AI 代理 push 後觸發。只用標準庫。"""
import os
import re
import sys
import json
import urllib.request

LOG = "judgment_log.json"
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")


def chat_ids():
    return [c.strip() for c in re.split(r"[,\s;]+", TG_CHAT or "") if c.strip()]


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
    score = r.get("ai_score")
    score_str = f"｜AI環境分 {score}/40" if score is not None else ""
    price = r.get("price")
    price_str = f"｜現價 {price}" if price is not None else ""
    lines = "\n".join("• " + x for x in r.get("lines", []))
    news = r.get("news", [])
    news_block = ("\n\n📰 新聞重點：\n" + "\n".join("· " + n for n in news)) if news else ""
    text = (f"{head} {r.get('date','')} {r.get('time','')}{price_str}{score_str}\n\n"
            f"{r.get('summary','')}\n\n{lines}{news_block}\n\n"
            "⚠️ 盤面分析非投資建議；不輸出勝率%。")
    send(text)
    print(f"已推播 {r.get('date')} {r.get('time')} phase={r.get('phase')}")


if __name__ == "__main__":
    main()
