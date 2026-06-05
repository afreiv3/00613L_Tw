#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第二層：LLM 判讀。重用 monitor.py 抓的同一批數據，交給 Claude 做質化判讀，
輸出 judgment_log.json（dashboard.html 的「人腦判讀」卡讀這支檔）。

與 monitor.py 的差別：monitor 用固定代理規則；本檔讓模型對第 1、3、5 等質化項
做真正的判斷與一句理由。鐵則：不輸出勝率%；查不到的數據標明、以中性計。

環境變數：ANTHROPIC_API_KEY（必需）、MODEL（選用，預設見下）。
依賴：pip install anthropic
"""
import os, sys, json, datetime as dt
import anthropic
import monitor as m   # 重用同目錄的 monitor.py 抓數工具

MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")  # 可換 claude-opus-4-6 等
LOG = "judgment_log.json"


def taipei_now():
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)
    return t


def phase(now):
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return "closed"
    if hm < 9 * 60:
        return "pre-open"
    if hm <= 13 * 60 + 30:
        return "intraday"
    return "post-close"


def collect():
    """把 monitor 的抓數結果整理成一包 dict 給模型。查不到就 None。"""
    px = m.intraday(m.SYMBOL)
    ma60, twii = m.ma("^TWII", 60)
    data = {
        "symbol": m.SYMBOL,
        "intraday": px,  # open/current/high/low/prev_close/vwap/frac/cum_vol 或 None
        "avg20_vol": m.avg_daily_volume(m.SYMBOL),
        "sox_pct": m.daily_change_pct("^SOX"),
        "ndx_pct": m.daily_change_pct("^IXIC"),
        "tsm_pct": m.daily_change_pct("TSM"),
        "wti_pct": m.daily_change_pct("CL=F"),
        "tnx_pct": m.daily_change_pct("^TNX"),
        "twii": twii, "twii_ma60": ma60,
    }
    return data


PROMPT = """你是紀律型操盤台分析師，標的 00631L（元大台灣50正2，約四成台積電、2倍槓桿）。
依下列「已抓到的數據」對七項評分。每項：有利=滿分、中性/待觀察=半分、不利=0。
權重：型態健康20、板塊偏強20、近支撐15、量能合理15、站穩關鍵價(VWAP)10、無系統風險10、大盤結構10。
對「型態健康/近支撐/站穩關鍵價」三項用真正判斷、每項一句理由，不要只套公式。

鐵則：
- 絕對不准輸出任何「勝率%」。
- 任何數據為 null＝查不到，該項以中性(半分)計並在 note 標「資料缺—以中性計」。
- 這是盤面分析、非投資建議。

階段：{phase}（pre-open 時型態/近支撐/站穩關鍵價標待開盤、給半分暫定）。
數據(JSON)：
{data}

只輸出 JSON，格式：
{{"score": <int 0-100>, "zone": "green|amber|red",
 "summary": "<一句總結：環境偏多或偏空、今日是否確定性較高的進場點>",
 "lines": ["型態：...","板塊：...","進場點：...","量能：...","站穩關鍵價：...","系統風險：...","大盤結構：..."],
 "factors": {{"pattern":{{"pts":<int>,"max":20,"note":"..."}},"sector":{{"pts":<int>,"max":20,"note":"..."}},
   "entry":{{"pts":<int>,"max":15,"note":"..."}},"volume":{{"pts":<int>,"max":15,"note":"..."}},
   "vwap":{{"pts":<int>,"max":10,"note":"..."}},"macro":{{"pts":<int>,"max":10,"note":"..."}},
   "trend":{{"pts":<int>,"max":10,"note":"..."}}}}}}
zone 規則：score>=70 green、40-69 amber、<40 red。"""


def ask_claude(ph, data):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = PROMPT.format(phase=ph, data=json.dumps(data, ensure_ascii=False, default=str))
    resp = client.messages.create(
        model=MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # 容錯：抓出第一個 { 到最後一個 }
    s, e = text.find("{"), text.rfind("}")
    return json.loads(text[s:e + 1])


def append_log(record, keep=120):
    hist = []
    try:
        with open(LOG, "r", encoding="utf-8") as f:
            hist = json.load(f).get("history", [])
    except Exception:
        pass
    hist.append(record)
    hist = hist[-keep:]
    with open(LOG, "w", encoding="utf-8") as f:
        json.dump({"updated": f"{record['date']} {record['time']} (Taipei)", "history": hist},
                  f, ensure_ascii=False, indent=2)


def append_archive(record, prefix="archive_judge"):
    """判讀永久存檔：按年切、不截斷。面板的歷史區點擊才載入。"""
    year = record["date"][:4]
    path = f"{prefix}_{year}.json"
    hist = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            hist = json.load(f).get("history", [])
    except Exception:
        pass
    hist.append(record)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"year": year, "count": len(hist), "history": hist},
                  f, ensure_ascii=False, indent=2)


def main():
    now = taipei_now()
    ph = phase(now)
    if ph == "closed":
        print("週末，略過判讀。")
        return
    data = collect()
    j = ask_claude(ph, data)
    px = data["intraday"]
    record = {
        "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"),
        "phase": "pre-open" if ph == "pre-open" else "intraday",
        "price": round(px["current"], 2) if px else None,
        "score": int(j["score"]), "zone": j["zone"],
        "summary": j.get("summary", ""), "lines": j.get("lines", []),
        "factors": j.get("factors", {}),
    }
    append_log(record)
    append_archive(record)

    # 推一則 Telegram（開頭固定 🧠 [判讀]，與 [量化] 區分）
    if os.environ.get("TG_TOKEN") and os.environ.get("TG_CHAT"):
        m.TG_TOKEN = os.environ["TG_TOKEN"]; m.TG_CHAT = os.environ["TG_CHAT"]
        zlabel = {"green": "🟢 進場區", "amber": "🟡 觀望區", "red": "🔴 不進區"}.get(j["zone"], "")
        body = (f"🧠 [判讀] {record['date']} {record['time']}\n"
                f"現價 {record['price']}｜{record['score']}/100｜{zlabel}\n\n"
                f"{record['summary']}\n\n" + "\n".join("• " + x for x in record["lines"]) +
                "\n\n⚠️ 盤面分析非投資建議；不輸出勝率%。")
        m.send_telegram(body)

    print(f"判讀完成 score={record['score']} zone={record['zone']}")


if __name__ == "__main__":
    main()
