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

import paper_trade
import gemini_strategy
import chatgpt_strategy
import subscribers

# ----------------------------- 設定 -----------------------------
SYMBOL          = "00631L.TW"     # 標的
ALERT_THRESHOLD = 70              # 進場區門檻（>=70 才發進場通知）
STATE_FILE      = "state.json"    # 當日去重用
AI_FACTORS_FILE = "ai_factors.json"  # 盤前 AI 判讀（看新聞）當天定案的三項因子
PANEL_URL       = "https://afreiv3.github.io/00631L_Tw/dashboard.html"  # 推播附面板連結
TG_TOKEN        = os.environ.get("TG_TOKEN", "")
TG_CHAT         = os.environ.get("TG_CHAT", "")

# 因子分兩組：盤中量化（隨價量變動）＋ 盤前 AI（看新聞，當天固定）
QUANT_KEYS = ["pattern", "entry", "volume", "vwap"]   # 共 60 分，monitor.py 每 30 分算
AI_KEYS    = ["sector", "macro", "trend"]             # 共 40 分，盤前 AI 寫進 ai_factors.json

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

        # 最新一根 K 的台北日期：休市日 Yahoo 會回傳前一交易日的資料，
        # 用它和「今天」比對即可判斷今天是否真的開盤（見 main() 休市防呆）。
        last_date = (dt.datetime.fromtimestamp(ts[-1], dt.timezone.utc)
                     + dt.timedelta(hours=8)).strftime("%Y-%m-%d")

        return {
            "open": first_open, "current": cur, "high": day_high, "low": day_low,
            "prev_close": prev_close, "vwap": vwap, "frac": frac, "cum_vol": cum_vol,
            "date": last_date,
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
def score_quant():
    """只算『盤中量化』四項（型態/進場點/量能/VWAP），共 60 分。
    板塊/系統風險/大盤結構三項改由盤前 AI 判讀（看新聞）寫進 ai_factors.json。"""
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

    return pts, notes, px


def zone(score):
    if score >= 70:
        return "🟢 進場區（確定性較高）"
    if score >= 40:
        return "🟡 觀望區（方向未定）"
    return "🔴 不進區（環境不利）"


# ----------------------------- Telegram -----------------------------
def _chat_ids():
    """收件人 = TG_CHAT（你本人）＋ subscribers.json（自動訂閱者）。"""
    return subscribers.all_chat_ids(TG_CHAT)


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


def load_ai_factors(today):
    """讀盤前 AI 當天定案的三項因子（板塊/系統風險/大盤結構）。
    回傳 (factors_dict, meta_dict, ok)。讀不到或非今天 → 三項以中性(半分)頂著，不讓整套壞掉。"""
    try:
        with open(AI_FACTORS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == today and isinstance(d.get("factors"), dict) \
           and all(k in d["factors"] for k in AI_KEYS):
            meta = {"summary": d.get("summary", ""), "news": d.get("news", []),
                    "generated_at": d.get("generated_at", ""), "stale": False}
            return d["factors"], meta, True
    except Exception:
        pass
    neutral = {k: {"pts": W[k] / 2, "max": W[k], "note": "盤前 AI 因子缺—以中性計"} for k in AI_KEYS}
    meta = {"summary": "（今日盤前 AI 判讀尚未產生，板塊/系統風險/大盤結構暫以中性計）",
            "news": [], "generated_at": "", "stale": True}
    return neutral, meta, False


def _upsert_by_date(hist, record):
    """一天一筆：同一天就更新最新狀態(覆蓋)，不同天才新增。波段操作看日線即可，避免每15分一列。"""
    if hist and hist[-1].get("date") == record.get("date"):
        hist[-1] = record
    else:
        hist.append(record)
    return hist


def write_dashboard(record, path="dashboard_data.json", keep=240):
    """維護一份滾動歷史 JSON 給網頁面板讀（一天一筆）。"""
    hist = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            hist = json.load(f).get("history", [])
    except Exception:
        pass
    _upsert_by_date(hist, record)
    hist = hist[-keep:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"updated": f"{record['date']} {record['time']} (Taipei)",
                   "history": hist}, f, ensure_ascii=False, indent=2)


def append_archive(record, prefix="archive_quant"):
    """永久紀錄：按年切檔、一天一筆（同日更新）。面板平時不抓它，只有點『載入歷史』才讀。"""
    year = record["date"][:4]
    path = f"{prefix}_{year}.json"
    hist = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            hist = json.load(f).get("history", [])
    except Exception:
        pass
    _upsert_by_date(hist, record)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"year": year, "count": len(hist), "history": hist},
                  f, ensure_ascii=False, indent=2)


# ----------------------------- 主程式 -----------------------------
def main():
    today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%Y-%m-%d")  # 台北日期
    now_hm = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%H:%M")
    state = load_state()

    # 盤中量化 4 項（60）＋ 盤前 AI 定案 3 項（40）→ 合併總分
    qpts, qnotes, px = score_quant()

    # 休市防呆：台股國定假日（如端午節）落在週一~週五時，GitHub/cron 仍會照常觸發，
    # 而 Yahoo 在非交易日會回傳「前一交易日」的價量。若盤中資料日期 ≠ 今天，
    # 代表今天根本沒開盤 → 不下單、不寫盤面數據、不推播，避免產生幽靈交易。
    # （盤前新聞評估由 gemini_preopen.py 另外產生，不受此影響，仍可照常找新聞。）
    if px and px.get("date") and px["date"] != today:
        print(f"{today} {now_hm} 休市（最新盤中資料為 {px['date']}）→ 跳過下單與面板寫入")
        return

    ai_f, ai_meta, ai_ok = load_ai_factors(today)
    factors = {}
    for k in QUANT_KEYS:
        factors[k] = {"pts": qpts[k], "max": W[k], "note": qnotes[k]}
    for k in AI_KEYS:
        factors[k] = ai_f[k]                       # 已是 {pts,max,note}
    total = round(sum(f["pts"] for f in factors.values()))
    quant_sum = round(sum(qpts.values()))
    ai_sum = round(sum(ai_f[k]["pts"] for k in AI_KEYS))

    px_str = f"{px['current']:.2f}" if px else "—"
    # Telegram 只放精簡重點（分數＋一句環境＋面板連結）；完整七項與新聞看面板
    brief = f"現價 <b>{px_str}</b>｜<b>{total}/100</b>（量化{quant_sum}＋AI{ai_sum}）｜{zone(total)}"
    if ai_meta.get("summary"):
        brief += f"\n🧠 {ai_meta['summary']}"
    brief += f"\n📊 完整七項與新聞 → {PANEL_URL}"

    is_new_day = state.get("date") != today
    if is_new_day:
        state = {"date": today, "full_sent": False, "alerted": False,
                 "g_full_sent": False, "c_full_sent": False}

    # 開盤後第一次執行：推一份完整合併評分（不論分數）
    if not state.get("full_sent"):
        send_telegram(f"📊 <b>[合併評分] {today} {now_hm}</b>\n" + brief)
        state["full_sent"] = True
    # 進場通知：合併總分進入進場區，且當日尚未發過
    elif total >= ALERT_THRESHOLD and not state.get("alerted"):
        send_telegram(f"⚡️ <b>[訊號] 達進場區 {total}/100</b>  {today} {now_hm}\n" + brief)
        state["alerted"] = True

    save_state(state)

    # 模擬盤（紙上交易）：用合併總分自動下單，純記錄、不碰真錢。買賣事件推 Telegram。
    # 用 try/except 隔離：帳本檔案若損毀（如 git 衝突標記污染），寧可跳過這一輪、
    # 發警告通知，也不能讓 load_state() 的 fallback 悄悄把真實歷史覆蓋成全新 100 萬。
    price_now = round(px["current"], 2) if px else None
    paper = None   # 若下方 try 失敗，保留 None：後面寫 rec 時安全（前端 renderPaper 已能處理空值）
    try:
        paper_event, paper, trade = paper_trade.run(today, now_hm, total, price_now)
        if paper_event == "BUY":
            send_telegram(
                f"🟢 <b>[模擬買進] {today} {now_hm}</b>\n"
                f"價 <b>{price_now}</b>｜分數 {total}｜投入 {int(trade['amount']):,} 元\n"
                f"總資產 {paper['equity']:,}｜報酬 {paper['return_pct']:+.2f}%\n"
                f"📊 模擬績效 → {PANEL_URL}")
        elif paper_event == "SELL":
            send_telegram(
                f"🔴 <b>[模擬賣出] {today} {now_hm}</b>\n"
                f"價 <b>{price_now}</b>｜{trade['reason']}\n"
                f"本筆損益 <b>{trade['pnl']:+,}</b>（{trade['pnl_pct']:+.2f}%）\n"
                f"總資產 {paper['equity']:,}｜報酬 {paper['return_pct']:+.2f}%\n"
                f"📊 模擬績效 → {PANEL_URL}")
    except Exception as e:
        print(f"[WARN] paper_trade 本輪跳過（狀態讀取異常，避免覆蓋歷史）: {e}")
        send_telegram(f"⚠️ <b>[Claude模擬盤] 狀態檔案讀取異常，本輪跳過寫入</b>\n"
                       f"{type(e).__name__}: {e}\n請檢查 paper_state.json 是否損毀。")

    # ===== 共用：大盤／00631L 均線（Gemini、ChatGPT 都要用，故放在各自 try 區塊外）=====
    twii_ma60, twii_close = ma("^TWII", 60)
    market_ok = (twii_close >= twii_ma60) if (twii_ma60 and twii_close) else None
    # 00631L 自身 20MA 當趨勢依據（出場看趨勢用）；10MA 給 ChatGPT 趨勢追蹤用
    self_ma20, _self_close = ma(SYMBOL, 20)
    asset_trend_ok = (price_now >= self_ma20) if (self_ma20 and price_now) else None
    self_ma10, _ = ma(SYMBOL, 10)
    ma10_ok = (price_now >= self_ma10) if (self_ma10 and price_now) else None

    # ===== Gemini 雙層閘門策略（與 Claude 並行；共用上面的量化數據 qpts）=====
    try:
        g_ai, g_meta, g_ok = gemini_strategy.load_ai_factors(today)
        g_ev = gemini_strategy.evaluate(qpts, market_ok, g_ai, asset_trend_ok)
        g_events, g_paper = gemini_strategy.run(today, now_hm, g_ev, price_now)
        # run() 已把最終決策(目標/區間/開窗)放進 g_paper

        # 當日第一次：推 Gemini 策略現況
        if not state.get("g_full_sent"):
            g_tgt = g_paper.get("target") or 0
            g_brief = (f"🤖 <b>[Gemini策略] {today} {now_hm}</b>\n"
                       f"量化主審 {g_paper['tier1']}/60｜AI情緒 {g_paper['ai_index']}/100｜"
                       f"目標水位 {int(g_tgt*100)}%\n{g_paper.get('zone_label','')}")
            if g_meta.get("summary"):
                g_brief += f"\n🧠 {g_meta['summary']}"
            g_brief += f"\n📊 詳情 → {PANEL_URL}"
            send_telegram(g_brief)
            state["g_full_sent"] = True
            save_state(state)

        # Gemini 模擬盤的再平衡／止盈事件推播
        for tr in g_events:
            act = "🟢 買進" if tr["action"] == "BUY" else "🔴 賣出"
            send_telegram(
                f"{act} <b>[Gemini模擬] {today} {now_hm}</b>\n"
                f"價 <b>{tr['price']}</b>｜{tr['reason']}\n"
                f"目標水位 {tr['target_pct']}%｜總資產 {g_paper['equity']:,}"
                f"（{g_paper['return_pct']:+.2f}%）\n📊 → {PANEL_URL}")

        g_rec = {
            "date": today, "time": now_hm, "price": price_now,
            "tier1": g_paper["tier1"], "ai_index": g_paper["ai_index"], "target": g_paper.get("target"),
            "zone": g_paper.get("zone"), "zone_label": g_paper.get("zone_label"),
            "gate_open": g_paper.get("gate_open"), "market_ok": market_ok,
            "asset_trend_ok": asset_trend_ok,
            "ai_ok": g_ok, "summary": g_meta.get("summary", ""), "news": g_meta.get("news", []),
            "paper": g_paper,
        }
        write_dashboard(g_rec, path="dashboard_data_gemini.json")
        append_archive(g_rec, prefix="archive_gemini")
    except Exception as e:
        print(f"[WARN] gemini_strategy 本輪跳過（狀態讀取異常，避免覆蓋歷史）: {e}")
        send_telegram(f"⚠️ <b>[Gemini策略] 狀態檔案讀取異常，本輪跳過寫入</b>\n"
                       f"{type(e).__name__}: {e}\n請檢查 paper_state_gemini.json 是否損毀。")

    # ===== ChatGPT 策略（A/B：單向部位＋三段停利＋趨勢追蹤；共用上面的量化/AI 數據）=====
    try:
        c_ai, c_meta, c_ok = chatgpt_strategy.load_ai_factors(today)
        c_ev = chatgpt_strategy.evaluate(qpts, market_ok, c_ai, ma20_ok=asset_trend_ok, ma10_ok=ma10_ok)
        c_events, c_paper = chatgpt_strategy.run(today, now_hm, c_ev, price_now)

        if not state.get("c_full_sent"):
            c_tgt = c_paper.get("target") or 0
            c_brief = (f"🟧 <b>[ChatGPT策略] {today} {now_hm}</b>\n"
                       f"量化主審 {c_paper['tier1']}/60｜AI情緒 {c_paper['ai_index']}/100｜"
                       f"目標水位 {int(c_tgt*100)}%\n{c_paper.get('zone_label','')}")
            if c_meta.get("summary"):
                c_brief += f"\n🧠 {c_meta['summary']}"
            c_brief += f"\n📊 詳情 → {PANEL_URL}"
            send_telegram(c_brief)
            state["c_full_sent"] = True
            save_state(state)

        for tr in c_events:
            act = "🟢 買進" if tr["action"] == "BUY" else "🔴 賣出"
            send_telegram(
                f"{act} <b>[ChatGPT模擬] {today} {now_hm}</b>\n"
                f"價 <b>{tr['price']}</b>｜{tr['reason']}\n"
                f"目標水位 {tr['target_pct']}%｜總資產 {c_paper['equity']:,}"
                f"（{c_paper['return_pct']:+.2f}%）\n📊 → {PANEL_URL}")

        c_rec = {
            "date": today, "time": now_hm, "price": price_now,
            "tier1": c_paper["tier1"], "ai_index": c_paper["ai_index"], "target": c_paper.get("target"),
            "zone": c_paper.get("zone"), "zone_label": c_paper.get("zone_label"),
            "gate_open": c_paper.get("gate_open"), "market_ok": market_ok,
            "asset_trend_ok": asset_trend_ok, "ma10_ok": ma10_ok,
            "ai_ok": c_ok, "summary": c_meta.get("summary", ""), "news": c_meta.get("news", []),
            "paper": c_paper,
        }
        write_dashboard(c_rec, path="dashboard_data_chatgpt.json")
        append_archive(c_rec, prefix="archive_chatgpt")
    except Exception as e:
        print(f"[WARN] chatgpt_strategy 本輪跳過（狀態讀取異常，避免覆蓋歷史）: {e}")
        send_telegram(f"⚠️ <b>[ChatGPT策略] 狀態檔案讀取異常，本輪跳過寫入</b>\n"
                       f"{type(e).__name__}: {e}\n請檢查 paper_state_chatgpt.json 是否損毀。")

    # 寫面板資料（滾動約1個月）＋ 永久年度存檔
    rec = {
        "date": today, "time": now_hm,
        "price": price_now,
        "score": total, "zone": zone_code(total),
        "quant_sum": quant_sum, "ai_sum": ai_sum, "ai_ok": ai_ok,
        "factors": {k: factors[k] for k in W},
        "paper": paper,
    }
    write_dashboard(rec)
    append_archive(rec)

    print(f"{today} {now_hm} total={total} (quant={quant_sum}+ai={ai_sum}) "
          f"ai_ok={ai_ok} full_sent={state.get('full_sent')} alerted={state.get('alerted')}")


if __name__ == "__main__":
    main()
