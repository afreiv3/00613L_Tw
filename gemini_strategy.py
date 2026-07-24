#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemini 雙層閘門策略 + 動態風控模擬盤（紙上交易）
-------------------------------------------------
第一層（量化主審，一票否決）：盤中量化 4 項加總 >= 35（滿分60），且大盤站上季線(60MA)。
  未過 → 目標水位 0%（強制清倉/絕對觀望）。
第二層（AI 情緒濾網，反向控水位）：AI 三項歸一化成 0–100 情緒指數：
  <30 極度恐慌 → 80%（逆向加碼）／30–75 健康 → 50%（標準持有）／>75 狂熱 → 20%（輕倉防守）。
動態風控：
  - 移動止盈：資產自歷史高點回撤 10% → 全數清倉、重設追蹤起點。
  - 連續 3 個交易日 AI 情緒指數 > 85（狂熱）→ 強制把目標水位壓回 20%。
  - 目標水位自動再平衡：曝險與目標差 > 5% 才交易（避免高頻摩擦）。

資料檔：ai_factors_gemini.json（讀，gemini_preopen.py 產生）；
        paper_state_gemini.json / paper_trades_gemini.json（寫）。
⚠️ 純模擬，非投資建議。
"""
import json
import datetime as dt

import trade_window

AI_FILE = "ai_factors_gemini.json"
STATE_FILE = "paper_state_gemini.json"
TRADES_FILE = "paper_trades_gemini.json"

START_CAPITAL = 1_000_000
TIER1_GATE = 35          # 量化主審門檻（滿分 60）
TRAILING_DD = 0.10       # 移動止盈：自高點回撤 10%
FRENZY_LEVEL = 85        # AI 情緒指數 > 85 視為狂熱
FRENZY_DAYS = 3          # 連續狂熱天數 → 壓回 20%
REBAL_MIN = 0.05         # 曝險變動 > 5% 才交易
FEE_RATE = 0.001425      # 手續費（單邊）
TAX_RATE = 0.001         # 證交稅（賣出）


def load_ai_factors(today):
    """讀 Gemini 盤前因子。回傳 (factors_dict, meta, ok)。缺/非今日 → 中性。"""
    try:
        with open(AI_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == today and isinstance(d.get("factors"), dict):
            return d["factors"], d, True
    except Exception:
        pass
    neutral = {"sector": 10.0, "macro": 5.0, "trend": 5.0}
    meta = {"factors": neutral, "summary": "（今日 Gemini 盤前判讀尚未產生，暫以中性計）",
            "news": [], "stale": True}
    return neutral, meta, False


def evaluate(quant_pts, market_ok, ai_factors, asset_trend_ok=None):
    """進出場分離設計：
      進場(空手)：量化≥35 + 大盤站季線 + 00631L 站上20MA（買在趨勢＋好價位）。
      出場(持有)：不再因量化<35 就賣；只在『趨勢轉壞』才出場——大盤跌破季線、
                  或 00631L 跌破20MA、或移動止盈(回撤10%)。趨勢沒壞就續抱。
      market_ok / asset_trend_ok：None=資料未知(不否決，避免誤殺)。
    最終目標水位在 run() 依『是否持有』決定，這裡只算情緒水位與進場/趨勢旗標。"""
    tier1 = sum(quant_pts.get(k, 0) for k in ("pattern", "entry", "volume", "vwap"))
    ai_total = ai_factors.get("sector", 10) + ai_factors.get("macro", 5) + ai_factors.get("trend", 5)
    ai_index = round(ai_total / 40 * 100, 1)

    # 情緒決定「在場內時」的目標水位（反向）
    if ai_index < 30:
        sent_target, slabel, szone = 0.8, "🟢 逆向加碼（極度恐慌）", "green"
    elif ai_index <= 75:
        sent_target, slabel, szone = 0.5, "🟡 理性持倉（情緒健康）", "amber"
    else:
        sent_target, slabel, szone = 0.2, "🟠 輕倉防守（極度狂熱）", "amber"

    trend_ok = (market_ok is not False) and (asset_trend_ok is not False)  # 趨勢是否完好
    entry_ok = (tier1 >= TIER1_GATE) and trend_ok                          # 進場要好價位＋趨勢
    return {"tier1": round(tier1), "ai_index": ai_index,
            "sent_target": sent_target, "sent_label": slabel, "sent_zone": szone,
            "trend_ok": trend_ok, "entry_ok": entry_ok,
            "market_ok": market_ok, "asset_trend_ok": asset_trend_ok}


def load_state():
    """讀模擬盤狀態。檔案不存在（首次執行）才給全新初始值；
    檔案存在但解析失敗（例如 git 衝突標記污染）視為嚴重錯誤、直接往上拋——
    絕不能靜默回傳全新狀態並讓呼叫端 save_state() 覆蓋掉真實歷史。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"cash": float(START_CAPITAL), "shares": 0.0, "cost_basis": 0.0,
                "start_capital": float(START_CAPITAL),
                "max_asset": float(START_CAPITAL), "frenzy_days": 0,
                "last_date": "", "trades": 0}


def save_state(s):
    s["updated"] = (dt.datetime.now(dt.timezone.utc) +
                    dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def _append(rec):
    hist = []
    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            hist = json.load(f).get("history", [])
    except Exception:
        pass
    hist.append(rec)
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": f"{rec['date']} {rec['time']}", "history": hist},
                  f, ensure_ascii=False, indent=2)


def _summary(s, price, ev):
    shares = s.get("shares", 0.0)
    mkt = shares * price if price else 0
    equity = s["cash"] + mkt
    start = s.get("start_capital", START_CAPITAL)
    cost = s.get("cost_basis", 0.0)                       # 目前持股的總成本
    avg_cost = (cost / shares) if shares > 0 else None    # 平均成本
    unreal = (mkt - cost) if shares > 0 else 0            # 未實現損益
    unreal_pct = (unreal / cost * 100) if cost > 0 else 0
    return {
        "cash": round(s["cash"]), "shares": round(shares, 1),
        "avg_cost": round(avg_cost, 2) if avg_cost else None,
        "unrealized": round(unreal), "unrealized_pct": round(unreal_pct, 2),
        "equity": round(equity), "market_value": round(mkt),
        "position_pct": round(mkt / equity * 100, 1) if equity else 0,
        "return_pct": round((equity / start - 1) * 100, 2) if start else 0,
        "trades": s.get("trades", 0), "max_asset": round(s.get("max_asset", start)),
        "frenzy_days": s.get("frenzy_days", 0),
        "tier1": ev.get("tier1"), "ai_index": ev.get("ai_index"),
        "target": ev.get("target"), "zone": ev.get("zone"),
        "zone_label": ev.get("zone_label"), "gate_open": ev.get("gate_open"),
    }


def run(today, now_hm, ev, price):
    """依評估結果 ev 執行動態再平衡與風控。回傳 (events_list, summary)。"""
    s = load_state()
    events = []
    if price is None or price <= 0:
        return events, _summary(s, price, ev)

    # 每日更新一次「連續狂熱天數」（AI 情緒指數整天固定，故以日為單位）
    if s.get("last_date") != today:
        s["frenzy_days"] = (s.get("frenzy_days", 0) + 1) if ev["ai_index"] > FRENZY_LEVEL else 0
        s["last_date"] = today

    total = s["cash"] + s["shares"] * price
    if total > s.get("max_asset", total):
        s["max_asset"] = total

    # 決定有效目標水位：持有看趨勢(壞了才清)、空手看進場條件(好價位＋趨勢才買)
    holding = s["shares"] > 0
    if holding:
        if not ev["trend_ok"]:
            target, zlabel, zone = 0.0, "🔴 趨勢轉弱→清倉（跌破均線/季線）", "red"
        else:
            target, zlabel, zone = ev["sent_target"], ev["sent_label"], ev["sent_zone"]
    else:
        if ev["entry_ok"]:
            target, zlabel, zone = ev["sent_target"], ev["sent_label"], ev["sent_zone"]
        else:
            target, zlabel, zone = 0.0, "⚪ 觀望（等好買點＋趨勢）", "red"
    # 把最終決策寫回 ev 供面板/推播顯示
    ev = dict(ev)
    ev["target"], ev["zone_label"], ev["zone"], ev["gate_open"] = target, zlabel, zone, target > 0

    # 波段規則：只在盤中、且今天還沒交易過，才允許下單（收盤後只更新分數/畫面）
    if not trade_window.can_trade(s, today, now_hm):
        save_state(s)
        return events, _summary(s, price, ev)

    # 風控 A：移動止盈（自高點回撤達門檻 → 全清）
    if s["shares"] > 0 and total / s["max_asset"] <= (1 - TRAILING_DD):
        sold = s["shares"]
        s["cash"] += sold * price * (1 - FEE_RATE - TAX_RATE)
        s["shares"] = 0.0
        s["cost_basis"] = 0.0
        s["max_asset"] = s["cash"]
        s["trades"] = s.get("trades", 0) + 1
        rec = {"date": today, "time": now_hm, "action": "SELL", "price": round(price, 2),
               "shares": round(sold, 1), "target_pct": 0,
               "reason": f"移動止盈（自高點回撤≥{int(TRAILING_DD*100)}%）"}
        _append(rec)
        events.append(rec)
        s["last_trade_date"] = today
        save_state(s)
        return events, _summary(s, price, ev)

    # 風控 B：連續狂熱 → 目標壓回 20%
    if s.get("frenzy_days", 0) >= FRENZY_DAYS and target > 0.2:
        target = 0.2

    # 目標水位自動再平衡（差距 > 5% 才動作）
    total = s["cash"] + s["shares"] * price
    diff = total * target - s["shares"] * price
    if total > 0 and abs(diff) / total > REBAL_MIN:
        if diff > 0:                                   # 補倉
            buy_cash = min(diff, s["cash"])
            sh = buy_cash / (price * (1 + FEE_RATE))
            if sh > 0:
                s["shares"] += sh
                s["cash"] -= sh * price * (1 + FEE_RATE)
                s["cost_basis"] = s.get("cost_basis", 0.0) + sh * price * (1 + FEE_RATE)
                action, reason = "BUY", f"再平衡至 {int(target*100)}%（{ev['zone_label']}）"
            else:
                sh = 0
        else:                                          # 減碼
            sh = min((-diff) / price, s["shares"])
            if sh > 0:
                old_sh = s["shares"]
                s["cash"] += sh * price * (1 - FEE_RATE - TAX_RATE)
                s["shares"] -= sh
                # 成本按比例減少（保留剩餘持股的平均成本）
                s["cost_basis"] = s.get("cost_basis", 0.0) * (s["shares"] / old_sh) if old_sh else 0.0
                action, reason = "SELL", f"再平衡至 {int(target*100)}%（{ev['zone_label']}）"
        if sh > 0:
            s["trades"] = s.get("trades", 0) + 1
            rec = {"date": today, "time": now_hm, "action": action, "price": round(price, 2),
                   "shares": round(sh, 1), "target_pct": int(target * 100), "reason": reason}
            _append(rec)
            events.append(rec)
            s["last_trade_date"] = today   # 今天動作過 → 鎖住，當日不再交易

    save_state(s)
    return events, _summary(s, price, ev)


if __name__ == "__main__":
    # 煙霧測試（不依賴網路）
    q = {"pattern": 20, "entry": 15, "volume": 0, "vwap": 5}  # tier1=40 >=35
    ev = evaluate(q, True, {"sector": 4, "macro": 2, "trend": 2})  # ai_index=20 <30 → 80%
    print("evaluate:", ev)
