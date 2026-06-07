#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemini 盤前 AI 情緒判讀
-------------------------------------------------
RSS 抓國際財經新聞（CNBC / Reuters / 半導體相關）→ 呼叫 Gemini API 結構化打分
→ 寫 ai_factors_gemini.json（含 date，供 monitor.py 的 Gemini 策略讀取）。

零依賴：只用標準庫 urllib + xml（不需 requests / google-generativeai）。
由 GitHub Actions 盤前排程執行；需環境變數 GEMINI_API_KEY。

⚠️ 情緒分數為輔助判讀，非投資建議。
"""
import os
import sys
import re
import json
import datetime as dt
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

OUT_FILE = "ai_factors_gemini.json"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
API_KEY = os.environ.get("GEMINI_API_KEY", "")

# RSS 來源（免費免金鑰）。Google News RSS 較穩定，可指定來源與時間範圍。
RSS = {
    "CNBC": "https://search.cnbc.com/rs/search/allview.xml?source=CNBC.com&id=15839069",
    "Reuters": "https://news.google.com/rss/search?q=when:1d+source:Reuters+finance+markets&hl=en-US&gl=US&ceid=US:en",
    "Semis": "https://news.google.com/rss/search?q=when:1d+(semiconductor+OR+Nasdaq+OR+%22Fed%22)+stock+market&hl=en-US&gl=US&ceid=US:en",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; 00631L-gemini/1.0)"}

PROMPT = """You are an expert quantitative trader focused on Taiwan's 00631L (a 2x leveraged Taiwan 50 ETF).
Analyze the following global financial news from the past 24h to score TODAY's Taiwan pre-market environment.

News headlines:
{news}

Scoring guidelines:
- sector (0-20): strength of highly correlated sectors — Philadelphia Semiconductor (SOX) and Nasdaq overnight performance. High = strong bullish tech momentum; low = bearish tech sell-off.
- macro (0-10): systemic risk factors — crude oil, US Treasury yields, geopolitics, rate-hike odds. High = calm/stable; low = rising global risk.
- trend (0-10): structural health of the broad market (holding above key moving averages). High = intact uptrend; low = broken structure.
Also give a concise one-sentence summary in Traditional Chinese (zh-TW).
Respond using ONLY the provided JSON schema."""


def fetch_news():
    items = []
    for src, url in RSS.items():
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                root = ET.fromstring(r.read())
            for it in root.findall(".//item")[:8]:
                title = (it.findtext("title") or "").strip()
                desc = re.sub("<[^>]+>", "", it.findtext("description") or "").strip()
                if title:
                    items.append(f"[{src}] {title}" + (f" - {desc[:160]}" if desc else ""))
        except Exception as e:
            print(f"[warn] RSS {src} 失敗: {e}", file=sys.stderr)
    return items


def call_gemini(news_text):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{MODEL}:generateContent?key={API_KEY}")
    # 注意：Gemini REST 用駝峰命名、schema 型別需大寫（OBJECT/NUMBER/STRING）。
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(news=news_text)}]}],
        "generationConfig": {
            "temperature": 0.4,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "sector": {"type": "NUMBER"},
                    "macro": {"type": "NUMBER"},
                    "trend": {"type": "NUMBER"},
                    "summary": {"type": "STRING"},
                },
                "required": ["sector", "macro", "trend", "summary"],
            },
        },
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code}: {detail[:600]}")
    text = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
    if text.startswith("```"):            # 萬一仍包了 markdown 圍欄
        text = re.sub(r"^```[a-zA-Z]*", "", text).strip().rstrip("`").strip()
    return json.loads(text)


def _clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return (lo + hi) / 2
    return max(lo, min(hi, v))


def main():
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)  # 台北
    today, now_hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")

    news = fetch_news()
    print(f"抓到 {len(news)} 則新聞")

    if news and API_KEY:
        try:
            res = call_gemini("\n".join(news))
        except Exception as e:
            print(f"[error] Gemini 判讀失敗: {e}", file=sys.stderr)
            res = {"sector": 10.0, "macro": 5.0, "trend": 5.0,
                   "summary": "（Gemini 判讀失敗，暫以中性計）"}
    else:
        if not API_KEY:
            print("[error] 缺 GEMINI_API_KEY", file=sys.stderr)
        res = {"sector": 10.0, "macro": 5.0, "trend": 5.0,
               "summary": "（無新聞或無金鑰，暫以中性計）"}

    factors = {"sector": _clamp(res.get("sector"), 0, 20),
               "macro": _clamp(res.get("macro"), 0, 10),
               "trend": _clamp(res.get("trend"), 0, 10)}
    ai_index = round((factors["sector"] + factors["macro"] + factors["trend"]) / 40 * 100, 1)
    out = {
        "date": today, "generated_at": f"{today} {now_hm}", "model": MODEL,
        "factors": factors, "ai_sentiment_index": ai_index,
        "summary": res.get("summary", ""), "news": news[:10],
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"已寫入 {OUT_FILE}: {factors} 情緒指數={ai_index}")


if __name__ == "__main__":
    main()
