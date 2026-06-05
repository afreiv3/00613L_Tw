#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
00631L 進場品質監控 + Telegram 通知
-------------------------------------------------
每次執行：抓數據 -> 用代理規則算「進場品質分數」(0-100) -> 條件成立就推 Telegram。

重要誠實聲明（請務必理解後再用）：
  檢查表有 7 項，其中數項本質是「質化判斷」，程式只能用『可量化代理規則』逼近，
  必然比你親眼看盤少細膩度。每一項下方都標了它實際在算什麼。
  分數是『把注意力放對地方』的輔助，不是可信的勝率。這不是投資建議，
  進場與否與所有後果由你自負。

資料來源：Yahoo Finance 非官方 chart endpoint（免費、無需金鑰，但可能被限流或改版）。
"""

import os
import sys
import json
import time
import datetime as dt
import re
import random
import urllib.request
import urllib.error

# ----------------------------- 設定 -----------------------------
SYMBOL          = "00631L.TW"     # 標的
ALERT_THRESHOLD = 70              # 進場區門檻（>=70 才發進場通知）
STATE_FILE      = "state.json"    # 當日去重用
TG_TOKEN        = os.environ.get("TG_TOKEN", "")
TG_CHAT         = os.environ.get("TG_CHAT", "")

# 檢查表權重（與你的工具一致）
W = {
    "pattern": 20,   # 型態健康
    "sector":  20,   # 最相關板塊偏強（費半/Nasdaq）
    "entry":   15,   # 進場點接近支撐（vs 追高）
    "volume":  15,   # 量能型態合理
    "vwap":    10,   # 站穩關鍵價（VWAP）
    "macro":   10,   # 無系統性風險發酵（油價/殖利率）
    "trend":   10,   # 大盤結構完好（加權 vs 季線）
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; entry-monitor/1.0)"}


# ----------------------------- 抓資料工具 -----------------------------
def _get(url):
    """帶退避重試 + query2 備援，緩解 Yahoo 從雲端 IP 偶發 403/429。"""
    hosts = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    last = None
    for attempt in range(4):
        u = url.replace("query1.finance.yahoo.com", hosts[attempt % 2])
        try:
            req = urllib.request.Request(u, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1) + random.random())
    raise last


def yahoo_chart(symbol, interval, rng):
    """回傳 Yahoo chart 的 result[0]，失敗回 None。"""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval={interval}&range={rng}")
    try:
        data = _get(url)
        return data["chart"]["result"][0]
    except Exception as e:
        print(f"[warn] fetch {symbol} 失敗: {e}", file=sys.stderr)
        return None


def daily_change_pct(symbol):
    """隔夜/最近一日漲跌幅 %（用日線最後兩根收盤）。失敗回 None。"""
    res = yahoo_chart(symbol, "1d", "5d")
    if not res:
        return None
    try:
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) < 2:
            return None
        return (closes[-1] / closes[-2] - 1) * 100
    except Exception:
        return None


def intraday(symbol):
    """回傳今日 5 分 K 的 dict：open/current/high/low/prev_close/vwap/elapsed_frac/cum_vol。"""
    res = yahoo_chart(symbol, "5m", "1d")
    if not res:
        return None
    try:
        meta = res["meta"]
        q = res["indicators"]["quote"][0]
        ts = res["timestamp"]
        opens  = q["open"];  highs = q["high"]
        lows   = q["low"];   closes = q["close"]; vols = q["volume"]

        # 過濾 None
        rows = [(o, h, l, c, v) for o, h, l, c, v in zip(opens, highs, lows, closes, vols)
                if None not in (o, h, l, c, v)]
        if not rows:
            return None

        first_open = rows[0][0]
        cur        = rows[-1][3]
        day_high   = max(r[1] for r in rows)
        day_low    = min(r[2] for r in rows)
        cum_vol    = sum(r[4] for r in rows)
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")

        # VWAP（典型價 * 量 加權）
        tp_vol = sum(((r[1] + r[2] + r[3]) / 3) * r[4] for r in rows)
        vwap   = tp_vol / cum_vol if cum_vol else cur

        # 已開盤時間比例（台股 09:00-13:30 = 270 分）
        elapsed_min = (ts[-1] - ts[0]) / 60 + 5
        frac = max(0.05, min(1.0, elapsed_min / 270))

        return {
            "open": first_open, "current": cur, "high": day_high, "low": day_low,
            "prev_close": prev_close, "vwap": vwap, "frac": frac, "cum_vol": cum_vol,
        }
    except Exception as e:
        print(f"[warn] 解析 {symbol} 盤中失敗: {e}", file=sys.stderr)
        return None


def avg_daily_volume(symbol, days=20):
    res = yahoo_chart(symbol, "1d", "2mo")
    if not res:
        return None
    try:
        vols = [v for v in res["indicators"]["quote"][0]["volume"] if v]
        vols = vols[-(days + 1):-1]  # 不含今天
        return sum(vols) / len(vols) if vols else None
    except Exception:
        return None


def ma(symbol, n=60):
    res = yahoo_chart(symbol, "1d", "6mo")
    if not res:
        return None, None
    try:
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) < n:
            n = len(closes)
        return sum(closes[-n:]) / n, closes[-1]
    except Exception:
        return None, None


# ----------------------------- 計分（代理規則） -----------------------------
def score_all():
    notes = {}
    pts = {}

    # 取盤中
    px = intraday(SYMBOL)

    # 1) 型態健康（算的是：跳空幅度 + 是否吐回過半漲幅）
    if px and px["prev_close"]:
        gap = (px["open"] / px["prev_close"] - 1) * 100
        if px["open"] > px["prev_close"]:
            giveback = (px["open"] - px["current"]) / max(1e-9, px["open"] - px["prev_close"])
        else:
            giveback = 0
        if gap > 1.5 and giveback >= 0.5:
            pts["pattern"] = 0
            notes["pattern"] = f"開高({gap:+.1f}%)走低、吐回{giveback*100:.0f}%漲幅 → 不利"
        elif px["current"] >= px["open"]:
            pts["pattern"] = W["pattern"]
            notes["pattern"] = f"現價守在開盤之上(開{gap:+.1f}%) → 有利"
        else:
            pts["pattern"] = W["pattern"] / 2
            notes["pattern"] = f"開{gap:+.1f}%、小幅回落，型態中性"
    else:
        pts["pattern"] = W["pattern"] / 2
        notes["pattern"] = "盤中資料缺，型態以中性計"

    # 2) 最相關板塊偏強（算的是：隔夜費半 + Nasdaq）
    sox = daily_change_pct("^SOX")
    ndx = daily_change_pct("^IXIC")
    if sox is not None and ndx is not None:
        if sox >= 0.5 and ndx >= 0:
            pts["sector"] = W["sector"]; tag = "有利"
        elif sox < 0 and ndx < 0:
            pts["sector"] = 0; tag = "不利"
        else:
            pts["sector"] = W["sector"] / 2; tag = "分歧"
        notes["sector"] = f"費半{sox:+.2f}%、Nasdaq{ndx:+.2f}% → {tag}"
    else:
        pts["sector"] = W["sector"] / 2
        notes["sector"] = "美股資料缺，以中性計"

    # 3) 進場點接近支撐（算的是：現價落在今日區間的哪一段）
    if px and px["high"] > px["low"]:
        pos = (px["current"] - px["low"]) / (px["high"] - px["low"])  # 0=貼低 1=貼高
        if pos <= 0.33:
            pts["entry"] = W["entry"]; notes["entry"] = f"現價近今日低點(區間{pos*100:.0f}%) → 接近支撐"
        elif pos >= 0.66:
            pts["entry"] = 0; notes["entry"] = f"現價在區間上緣({pos*100:.0f}%) → 屬追高"
        else:
            pts["entry"] = W["entry"] / 2; notes["entry"] = f"現價在區間中段({pos*100:.0f}%)"
    else:
        pts["entry"] = W["entry"] / 2
        notes["entry"] = "區間資料缺，以中性計"

    # 4) 量能型態合理（算的是：今日量推估 vs 近20日均量）
    avgv = avg_daily_volume(SYMBOL)
    if px and avgv:
        projected = px["cum_vol"] / px["frac"]
        ratio = projected / avgv
        if ratio > 1.8:
            pts["volume"] = 0; notes["volume"] = f"推估量約均量{ratio:.1f}倍 → 爆量(疑換手/出貨)"
        elif 0.7 <= ratio <= 1.4:
            pts["volume"] = W["volume"]; notes["volume"] = f"推估量約均量{ratio:.1f}倍 → 正常"
        else:
            pts["volume"] = W["volume"] / 2; notes["volume"] = f"推估量約均量{ratio:.1f}倍 → 偏離"
    else:
        pts["volume"] = W["volume"] / 2
        notes["volume"] = "量能資料缺，以中性計"

    # 5) 站穩關鍵價（算的是：現價 vs VWAP）
    if px:
        diff = (px["current"] / px["vwap"] - 1) * 100
        if diff >= 0:
            pts["vwap"] = W["vwap"]; notes["vwap"] = f"現價在VWAP之上({diff:+.2f}%) → 有利"
        elif diff > -0.3:
            pts["vwap"] = W["vwap"] / 2; notes["vwap"] = f"現價貼近VWAP({diff:+.2f}%)"
        else:
            pts["vwap"] = 0; notes["vwap"] = f"現價在VWAP之下({diff:+.2f}%) → 弱勢"
    else:
        pts["vwap"] = W["vwap"] / 2
        notes["vwap"] = "VWAP 資料缺，以中性計"

    # 6) 無系統性風險發酵（算的是：隔夜油價 + 美債殖利率 急變）
    wti = daily_change_pct("CL=F")
    tnx = daily_change_pct("^TNX")
    if wti is not None and tnx is not None:
        if wti > 3 or tnx > 3:
            pts["macro"] = 0; tag = "風險升溫"
        elif abs(wti) < 1.5 and abs(tnx) < 1.5:
            pts["macro"] = W["macro"]; tag = "平穩"
        else:
            pts["macro"] = W["macro"] / 2; tag = "略有波動"
        notes["macro"] = f"油價{wti:+.1f}%、10Y殖利率{tnx:+.1f}% → {tag}"
    else:
        pts["macro"] = W["macro"] / 2
        notes["macro"] = "油價/殖利率資料缺，以中性計"

    # 7) 大盤結構完好（算的是：加權 vs 季線MA60）
    ma60, twii = ma("^TWII", 60)
    if ma60 and twii:
        d = (twii / ma60 - 1) * 100
        if d >= 0:
            pts["trend"] = W["trend"]; notes["trend"] = f"加權在季線之上({d:+.1f}%) → 結構完好"
        elif d > -1:
            pts["trend"] = W["trend"] / 2; notes["trend"] = f"加權貼近季線({d:+.1f}%)"
        else:
            pts["trend"] = 0; notes["trend"] = f"加權跌破季線({d:+.1f}%) → 結構轉弱"
    else:
        pts["trend"] = W["trend"] / 2
        notes["trend"] = "加權資料缺，以中性計"

    total = round(sum(pts.values()))
    return total, pts, notes, px


def zone(score):
    if score >= 70:
        return "🟢 進場區（確定性較高）"
    if score >= 40:
        return "🟡 觀望區（方向未定）"
    return "🔴 不進區（環境不利）"


# ----------------------------- Telegram -----------------------------
def _chat_ids():
    """TG_CHAT 可填多個，用逗號/空白/分號分隔；可放個人 id、群組 id 或頻道 @name / -100…。"""
    return [c.strip() for c in re.split(r"[,\s;]+", TG_CHAT or "") if c.strip()]


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[error] 缺 TG_TOKEN / TG_CHAT，無法發送", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    ok_all = True
    for cid in _chat_ids():
        payload = json.dumps({
            "chat_id": cid, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20)
        except Exception as e:
            ok_all = False
            print(f"[error] Telegram 發送失敗 chat={cid}: {e}", file=sys.stderr)
    return ok_all


# ----------------------------- 狀態（當日去重） -----------------------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def zone_code(s):
    return "green" if s >= 70 else ("amber" if s >= 40 else "red")


def write_dashboard(record, path="dashboard_data.json", keep=240):
    """維護一份滾動歷史 JSON 給網頁面板讀。"""
    hist = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            hist = json.load(f).get("history", [])
    except Exception:
        pass
    hist.append(record)
    hist = hist[-keep:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"updated": f"{record['date']} {record['time']} (Taipei)",
                   "history": hist}, f, ensure_ascii=False, indent=2)


def append_archive(record, prefix="archive_quant"):
    """永久紀錄：按年切檔、不截斷。面板平時不抓它，只有點『載入歷史』才讀，頁面保持快速。"""
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


# ----------------------------- 主程式 -----------------------------
def main():
    today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%Y-%m-%d")  # 台北日期
    now_hm = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%H:%M")
    state = load_state()

    total, pts, notes, px = score_all()
    px_str = f"{px['current']:.2f}" if px else "—"
    line_notes = "\n".join(f"• {notes[k]}" for k in
                           ["pattern", "sector", "entry", "volume", "vwap", "macro", "trend"])

    header = (f"<b>00631L 進場監控</b>  {today} {now_hm}\n"
              f"現價 <b>{px_str}</b> ｜ 分數 <b>{total}/100</b>\n"
              f"{zone(total)}\n\n{line_notes}")

    is_new_day = state.get("date") != today
    if is_new_day:
        state = {"date": today, "morning_sent": False, "alerted": False}

    # 晨間簡報：當日第一次執行
    if not state.get("morning_sent"):
        send_telegram("☀️ <b>[量化] 晨間簡報</b>\n" + header)
        state["morning_sent"] = True

    # 進場通知：分數進入進場區，且當日尚未發過
    if total >= ALERT_THRESHOLD and not state.get("alerted"):
        send_telegram("⚡️ <b>[量化] 出現進場區訊號</b>\n" + header +
                      "\n\n⚠️ 代理規則訊號，非投資建議；請自行確認盤面與風控。")
        state["alerted"] = True

    save_state(state)

    # 寫面板資料（滾動約1個月）＋ 永久年度存檔
    rec = {
        "date": today, "time": now_hm,
        "price": round(px["current"], 2) if px else None,
        "score": total, "zone": zone_code(total),
        "factors": {k: {"pts": pts[k], "max": W[k], "note": notes[k]} for k in W},
    }
    write_dashboard(rec)
    append_archive(rec)

    print(f"{today} {now_hm} score={total} alerted={state.get('alerted')}")


if __name__ == "__main__":
    main()
