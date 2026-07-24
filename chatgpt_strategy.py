#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ChatGPT 策略 v2：水位式單向部位 + 三段停利 + 趨勢追蹤（紙上交易）
-------------------------------------------------------------------
與 Gemini（水位再平衡，A/B 對照組）並行的第二套水位式策略。核心差異：
  Gemini：持倉時持續再平衡到目標水位（會逢低補倉、賺錢就被砍回水位）。
  ChatGPT：單向部位——進場一次後「只出不進」，絕不逢低攤平、絕不把停利賣掉的買回；
           分數轉弱只降水位、不直接清倉；獲利分三段收下，剩餘部位交給趨勢。

進場（空手時，雙層閘門，與 Gemini 相同）：
  tier1 ≥ 35 + 大盤 ^TWII 站上 60MA + 00631L 站上 20MA + 資料完整 + 盤中。
  進場規模＝AI 情緒反向水位（<30→80%／30–75→50%／>75→20%）。

持倉時的判斷優先序（每個 tick 最多執行一個動作）：
  1. 強制風控：00631L 跌破 20MA／^TWII 跌破 60MA／總資產自高點回撤≥10% → 全清。
  2. 第一段停利：浮盈 ≥ +5% 且未做過 → 賣 30%；剩餘部位改由移動停利＋10MA 保護。
  3. 第二段停利：浮盈 ≥ +10% 且未做過 → 賣 30%，進入趨勢追蹤模式。
  4. 移動停利（已收第一段停利後即生效）：自持倉後最高價回落 ≥8% 全清；≥5% 減半（每日一次）。
  5. 趨勢追蹤 10MA：跌破 10MA（但仍在 20MA 上）→ 減半剩餘部位（每日一次）。
  6. 分數／情緒只降水位：實際曝險高於目標 > 5% 才減碼到目標；絕不加碼、絕不買回。

頻率規則：一天最多一次「新進場」；停利、減碼、風控清倉不受此限（盤中即可執行）。

資料檔：ai_factors_gemini.json（共用 Gemini 盤前判讀，確保純策略 A/B）；
        paper_state_chatgpt.json / paper_trades_chatgpt.json（寫）。
⚠️ 純模擬，非投資建議。
"""
import json
import datetime as dt

import trade_window

AI_FILE = "ai_factors_gemini.json"      # 共用 Gemini 盤前 AI 判讀（純策略 A/B）
STATE_FILE = "paper_state_chatgpt.json"
TRADES_FILE = "paper_trades_chatgpt.json"

START_CAPITAL = 1_000_000
TIER1_GATE = 35              # 量化主審門檻（滿分 60）

# 三段停利
STAGE1_PROFIT_TRIGGER = 0.05   # 第一段：浮盈 +5%
STAGE1_SELL_RATIO = 0.30       # 賣出當前持倉 30%
STAGE2_PROFIT_TRIGGER = 0.10   # 第二段：浮盈 +10%
STAGE2_SELL_RATIO = 0.30       # 再賣出當前持倉 30%

# 趨勢追蹤 / 移動停利
BELOW_10MA_SELL_RATIO = 0.50   # 跌破 10MA：減半剩餘部位
TRAILING_REDUCE_FROM_HIGH = 0.05  # 自持倉後最高價回落 5% → 減半
TRAILING_EXIT_FROM_HIGH = 0.08    # 自持倉後最高價回落 8% → 全清

# 強制風控
EQUITY_FORCED_DD = 0.10        # 總資產自歷史高點回撤 10% → 全清
FRENZY_LEVEL = 85              # AI 情緒指數 > 85 視為狂熱
FRENZY_DAYS = 3               # 連續狂熱天數 → 目標水位壓回 20%

REBAL_MIN = 0.05              # 曝險高於目標 > 5% 才減碼（避免高頻摩擦）
FEE_RATE = 0.001425           # 手續費（單邊）
TAX_RATE = 0.001             # 證交稅（賣出）


def load_ai_factors(today):
    """讀盤前 AI 因子（共用 Gemini）。回傳 (factors_dict, meta, ok)。缺/非今日 → 中性。"""
    try:
        with open(AI_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == today and isinstance(d.get("factors"), dict):
            return d["factors"], d, True
    except Exception:
        pass
    neutral = {"sector": 10.0, "macro": 5.0, "trend": 5.0}
    meta = {"factors": neutral, "summary": "（今日盤前判讀尚未產生，暫以中性計）",
            "news": [], "stale": True}
    return neutral, meta, False


def evaluate(quant_pts, market_ok, ai_factors, ma20_ok=None, ma10_ok=None):
    """算分與旗標。趨勢條件分三層：market_ok(大盤60MA)／ma20_ok(00631L 20MA)／ma10_ok(10MA)。
    None＝資料未知（不否決進場以外的判斷，避免誤殺）。"""
    tier1 = sum(quant_pts.get(k, 0) for k in ("pattern", "entry", "volume", "vwap"))
    ai_total = ai_factors.get("sector", 10) + ai_factors.get("macro", 5) + ai_factors.get("trend", 5)
    ai_index = round(ai_total / 40 * 100, 1)

    # 情緒反向決定「目標水位」（持倉時當作上限／只降不升）
    if ai_index < 30:
        sent_target, slabel, szone = 0.8, "🟢 重倉（極度恐慌·逆向）", "green"
    elif ai_index <= 75:
        sent_target, slabel, szone = 0.5, "🟡 標準持倉（情緒健康）", "amber"
    else:
        sent_target, slabel, szone = 0.2, "🟠 輕倉防守（極度狂熱）", "amber"

    # 進場需：量化好價位 + 大盤站季線 + 00631L 站 20MA + 資料完整
    data_ok = (market_ok is not None) and (ma20_ok is not None)
    entry_ok = (tier1 >= TIER1_GATE) and (market_ok is True) and (ma20_ok is True)
    return {"tier1": round(tier1), "ai_index": ai_index,
            "sent_target": sent_target, "sent_label": slabel, "sent_zone": szone,
            "entry_ok": entry_ok, "data_ok": data_ok,
            "market_ok": market_ok, "ma20_ok": ma20_ok, "ma10_ok": ma10_ok}


def load_state():
    """讀模擬盤狀態。檔案不存在（首次執行）才視為空、補全新預設值；
    檔案存在但解析失敗（例如 git 衝突標記污染）視為嚴重錯誤、直接往上拋——
    絕不能靜默當成『從沒交易過』並讓呼叫端 save_state() 覆蓋掉真實歷史。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {}
    # 預設值（向後相容：缺欄位就補）
    d = {
        "cash": float(START_CAPITAL), "shares": 0.0, "cost_basis": 0.0,
        "start_capital": float(START_CAPITAL),
        "highest_equity": float(START_CAPITAL), "highest_price_since_entry": 0.0,
        "entry_date": "",
        "profit_stage_1_done": False, "profit_stage_2_done": False,
        "breakeven_mode": False, "trailing_mode": False,
        "current_target_exposure": 0.0, "realized_pnl": 0.0,
        "frenzy_days": 0, "last_date": "",
        "last_entry_date": "", "last_trade_date": "",
        "last_reduce_date": "",   # 10MA 減碼 / 移動停利減碼：每日一次
        "last_action": "", "last_action_reason": "", "trades": 0,
    }
    d.update(s)
    return d


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


def _avg_cost(s):
    sh = s.get("shares", 0.0)
    return (s.get("cost_basis", 0.0) / sh) if sh > 0 else None


def _sell(s, shares_to_sell, price):
    """賣出指定股數，按比例攤提成本、累計已實現損益。回傳實際賣出股數。"""
    sh = min(shares_to_sell, s["shares"])
    if sh <= 0:
        return 0.0
    old = s["shares"]
    proceeds = sh * price * (1 - FEE_RATE - TAX_RATE)
    cost_portion = s.get("cost_basis", 0.0) * (sh / old) if old else 0.0
    s["cash"] += proceeds
    s["shares"] -= sh
    s["cost_basis"] = s.get("cost_basis", 0.0) - cost_portion
    s["realized_pnl"] = s.get("realized_pnl", 0.0) + (proceeds - cost_portion)
    if s["shares"] <= 1e-6:            # 全清 → 重設部位狀態
        s["shares"] = 0.0
        s["cost_basis"] = 0.0
        s["highest_price_since_entry"] = 0.0
        s["profit_stage_1_done"] = False
        s["profit_stage_2_done"] = False
        s["breakeven_mode"] = False
        s["trailing_mode"] = False
        s["entry_date"] = ""
    return sh


def _record(s, events, today, now_hm, action, sh, price, target, reason_code, reason_txt):
    s["trades"] = s.get("trades", 0) + 1
    s["last_action"] = action
    s["last_action_reason"] = reason_code
    rec = {"date": today, "time": now_hm, "action": action, "price": round(price, 2),
           "shares": round(sh, 1), "target_pct": int(round(target * 100)),
           "reason_code": reason_code, "reason": reason_txt}
    _append(rec)
    events.append(rec)


def _summary(s, price, ev):
    shares = s.get("shares", 0.0)
    mkt = shares * price if price else 0
    equity = s["cash"] + mkt
    start = s.get("start_capital", START_CAPITAL)
    cost = s.get("cost_basis", 0.0)
    avg_cost = _avg_cost(s)
    unreal = (mkt - cost) if shares > 0 else 0
    unreal_pct = (unreal / cost * 100) if cost > 0 else 0
    hi = s.get("highest_price_since_entry", 0.0)
    dd_from_high = ((1 - price / hi) * 100) if (hi and price) else 0
    return {
        "cash": round(s["cash"]), "shares": round(shares, 1),
        "avg_cost": round(avg_cost, 2) if avg_cost else None,
        "avg_entry_price": round(avg_cost, 2) if avg_cost else None,
        "unrealized": round(unreal), "unrealized_pct": round(unreal_pct, 2),
        "unrealized_return_pct": round(unreal_pct, 2),
        "realized_pnl": round(s.get("realized_pnl", 0.0)),
        "equity": round(equity), "total_equity": round(equity),
        "market_value": round(mkt),
        "position_pct": round(mkt / equity * 100, 1) if equity else 0,
        "actual_exposure": round(mkt / equity, 3) if equity else 0,
        "target_exposure": round(s.get("current_target_exposure", 0.0), 3),
        "return_pct": round((equity / start - 1) * 100, 2) if start else 0,
        "trades": s.get("trades", 0),
        "highest_equity": round(s.get("highest_equity", start)),
        "highest_price_since_entry": round(hi, 2) if hi else None,
        "drawdown_from_position_high_pct": round(dd_from_high, 2),
        "position_status": "holding" if shares > 0 else "flat",
        "profit_stage_1_done": s.get("profit_stage_1_done", False),
        "profit_stage_2_done": s.get("profit_stage_2_done", False),
        "breakeven_mode": s.get("breakeven_mode", False),
        "trailing_mode": s.get("trailing_mode", False),
        "frenzy_days": s.get("frenzy_days", 0),
        "last_action": s.get("last_action", ""),
        "last_action_reason": s.get("last_action_reason", ""),
        "tier1": ev.get("tier1"), "ai_index": ev.get("ai_index"),
        "target": ev.get("target"), "zone": ev.get("zone"),
        "zone_label": ev.get("zone_label"), "gate_open": ev.get("gate_open"),
    }


def run(today, now_hm, ev, price):
    """依評估結果 ev 執行單向部位 + 三段停利 + 趨勢追蹤。回傳 (events_list, summary)。"""
    s = load_state()
    events = []

    # 決定目標水位（情緒水位；連續狂熱壓回 20%）。空手＝看進場條件。
    holding0 = s["shares"] > 0
    target = ev["sent_target"]

    if price is None or price <= 0:
        s["last_action_reason"] = "DATA_INVALID_NO_TRADE"
        ev = dict(ev); ev["target"], ev["zone_label"], ev["zone"], ev["gate_open"] = \
            (target if holding0 else 0.0), "⚪ 資料缺，不動作", "red", False
        save_state(s)
        return events, _summary(s, price, ev)

    # 每日更新「連續狂熱天數」
    if s.get("last_date") != today:
        s["frenzy_days"] = (s.get("frenzy_days", 0) + 1) if ev["ai_index"] > FRENZY_LEVEL else 0
        s["last_date"] = today
    if s.get("frenzy_days", 0) >= FRENZY_DAYS and target > 0.2:
        target = 0.2
    s["current_target_exposure"] = target if (holding0 or ev["entry_ok"]) else 0.0

    # 持倉時即時更新最高價與最高總資產
    total = s["cash"] + s["shares"] * price
    if total > s.get("highest_equity", total):
        s["highest_equity"] = total
    if holding0 and price > s.get("highest_price_since_entry", 0.0):
        s["highest_price_since_entry"] = price

    # 寫面板用的決策標籤
    def _finish(zlabel, zone):
        ev2 = dict(ev)
        eff = s.get("current_target_exposure", 0.0)
        ev2["target"], ev2["zone_label"], ev2["zone"], ev2["gate_open"] = eff, zlabel, zone, s["shares"] > 0
        save_state(s)
        return events, _summary(s, price, ev2)

    in_hours = trade_window.in_market_hours(now_hm)

    # ================= 空手：只看進場（一天最多一次新進場） =================
    if not holding0:
        if not in_hours:
            return _finish("⚪ 觀望（非盤中）", "red")
        if not trade_window.can_enter(s, today, now_hm):
            return _finish("⚪ 今日已進場過", "red")
        if not ev["data_ok"]:
            s["last_action_reason"] = "DATA_INVALID_NO_TRADE"
            return _finish("⚪ 資料不足，不進場", "red")
        if not ev["entry_ok"]:
            return _finish("⚪ 觀望（等量化≥35＋趨勢）", "red")
        # 進場：依情緒水位建立新倉
        buy_cash = min(s["cash"] * target, s["cash"])
        sh = buy_cash / (price * (1 + FEE_RATE))
        if sh > 0:
            s["shares"] = sh
            s["cash"] -= sh * price * (1 + FEE_RATE)
            s["cost_basis"] = sh * price * (1 + FEE_RATE)
            s["entry_date"] = today
            s["highest_price_since_entry"] = price
            s["highest_equity"] = s["cash"] + s["shares"] * price
            s["profit_stage_1_done"] = False
            s["profit_stage_2_done"] = False
            s["breakeven_mode"] = False
            s["trailing_mode"] = False
            s["last_entry_date"] = today
            s["last_trade_date"] = today
            _record(s, events, today, now_hm, "BUY", sh, price, target,
                    "ENTRY_SIGNAL", f"進場建倉至 {int(target*100)}%（{ev['sent_label']}）")
        return _finish(ev["sent_label"], ev["sent_zone"])

    # ================= 持倉：賣出/風控不受「一天一次」限制，但須盤中 =================
    if not in_hours:
        return _finish(ev["sent_label"], ev["sent_zone"])

    avg = _avg_cost(s) or price
    unreal_pct = price / avg - 1
    hi = s.get("highest_price_since_entry", price)
    dd_from_high = 1 - price / hi if hi else 0

    # ---- 1. 強制風控（最高優先，全清）----
    if ev["ma20_ok"] is False:
        sh = _sell(s, s["shares"], price); s["last_trade_date"] = today
        _record(s, events, today, now_hm, "SELL", sh, price, 0.0,
                "EXIT_BELOW_20MA", "趨勢破壞：00631L 跌破 20MA → 全清")
        return _finish("🔴 跌破 20MA → 清倉", "red")
    if ev["market_ok"] is False:
        sh = _sell(s, s["shares"], price); s["last_trade_date"] = today
        _record(s, events, today, now_hm, "SELL", sh, price, 0.0,
                "EXIT_TWII_BELOW_60MA", "趨勢破壞：大盤 ^TWII 跌破 60MA → 全清")
        return _finish("🔴 大盤跌破季線 → 清倉", "red")
    if total / s.get("highest_equity", total) <= (1 - EQUITY_FORCED_DD):
        sh = _sell(s, s["shares"], price); s["last_trade_date"] = today
        _record(s, events, today, now_hm, "SELL", sh, price, 0.0,
                "FORCED_RISK_DRAWDOWN", f"強制風控：總資產自高點回撤≥{int(EQUITY_FORCED_DD*100)}% → 全清")
        return _finish("🔴 資產回撤風控 → 清倉", "red")

    # ---- 2. 第一段停利（+5%，賣 30%）----
    if not s.get("profit_stage_1_done") and unreal_pct >= STAGE1_PROFIT_TRIGGER:
        sh = _sell(s, s["shares"] * STAGE1_SELL_RATIO, price)
        s["profit_stage_1_done"] = True
        s["breakeven_mode"] = True
        s["last_trade_date"] = today
        _record(s, events, today, now_hm, "SELL", sh, price, target,
                "STAGE_1_PROFIT_TAKE", f"第一段停利 +{unreal_pct*100:.1f}%：賣 {int(STAGE1_SELL_RATIO*100)}%，剩餘轉移動停利＋10MA 保護")
        return _finish("🟢 第一段停利（移動停利保護）", "green")

    # ---- 3. 第二段停利（+10%，賣 30%）----
    if s.get("profit_stage_1_done") and not s.get("profit_stage_2_done") \
       and unreal_pct >= STAGE2_PROFIT_TRIGGER:
        sh = _sell(s, s["shares"] * STAGE2_SELL_RATIO, price)
        s["profit_stage_2_done"] = True
        s["trailing_mode"] = True
        s["last_trade_date"] = today
        _record(s, events, today, now_hm, "SELL", sh, price, target,
                "STAGE_2_PROFIT_TAKE", f"第二段停利 +{unreal_pct*100:.1f}%：賣 {int(STAGE2_SELL_RATIO*100)}%，轉趨勢追蹤")
        return _finish("🟢 第二段停利（趨勢追蹤）", "green")

    # ---- 4. 移動停利（已收第一段停利後即生效；以持倉後最高價為基準）----
    if s.get("trailing_mode") or s.get("profit_stage_1_done"):
        if dd_from_high >= TRAILING_EXIT_FROM_HIGH:
            sh = _sell(s, s["shares"], price); s["last_trade_date"] = today
            _record(s, events, today, now_hm, "SELL", sh, price, 0.0,
                    "TRAILING_EXIT", f"移動停利：自最高價回落 {dd_from_high*100:.1f}%（≥{int(TRAILING_EXIT_FROM_HIGH*100)}%）→ 全清")
            return _finish("🔴 移動停利 → 清倉", "red")
        if dd_from_high >= TRAILING_REDUCE_FROM_HIGH and s.get("last_reduce_date") != today:
            sh = _sell(s, s["shares"] * 0.5, price)
            s["last_reduce_date"] = today; s["last_trade_date"] = today
            _record(s, events, today, now_hm, "SELL", sh, price, target,
                    "TRAILING_REDUCE", f"移動停利：自最高價回落 {dd_from_high*100:.1f}%（≥{int(TRAILING_REDUCE_FROM_HIGH*100)}%）→ 減半")
            return _finish("🟠 移動停利減半", "amber")

    # ---- 5. 趨勢追蹤 10MA：跌破 10MA（仍在 20MA 上）→ 減半（每日一次）----
    if ev["ma10_ok"] is False and s.get("last_reduce_date") != today:
        sh = _sell(s, s["shares"] * BELOW_10MA_SELL_RATIO, price)
        s["last_reduce_date"] = today; s["last_trade_date"] = today
        _record(s, events, today, now_hm, "SELL", sh, price, target,
                "REDUCE_BELOW_10MA", f"跌破 10MA（仍在 20MA 上）→ 減 {int(BELOW_10MA_SELL_RATIO*100)}% 降水位")
        return _finish("🟠 跌破10MA 降水位", "amber")

    # ---- 6. 分數／情緒只降水位（實際曝險高於目標才減碼；絕不加碼/買回）----
    actual = (s["shares"] * price) / total if total else 0
    if actual - target > REBAL_MIN:
        sell_value = (actual - target) * total
        sh = _sell(s, sell_value / price, price); s["last_trade_date"] = today
        code = "SCORE_WEAK_NO_ADD" if ev["tier1"] < TIER1_GATE else "AI_RISK_REDUCE_ONLY"
        _record(s, events, today, now_hm, "SELL", sh, price, target,
                code, f"降水位至 {int(target*100)}%（{ev['sent_label']}；不加碼/不買回）")
        return _finish(ev["sent_label"], ev["sent_zone"])

    # 無動作：續抱
    if ev["tier1"] < TIER1_GATE:
        s["last_action_reason"] = "SCORE_WEAK_NO_ADD"
    return _finish(ev["sent_label"], ev["sent_zone"])


if __name__ == "__main__":
    # 煙霧測試（不依賴網路）
    q = {"pattern": 20, "entry": 15, "volume": 0, "vwap": 5}  # tier1=40
    ev = evaluate(q, True, {"sector": 4, "macro": 2, "trend": 2}, ma20_ok=True, ma10_ok=True)
    print("evaluate:", ev)
