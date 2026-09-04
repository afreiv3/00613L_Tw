#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude 升級版策略：波段單向部位 + 收盤確認出場 + 三段停利（紙上交易）
-------------------------------------------------------------------
設計目標：風險調整後穩健成長，不是追求單次爆發報酬。
專門修正量化／ChatGPT／Gemini 三套舊策略已經被交易紀錄證實的弱點：
  - 量化策略：出場只看評分（落後指標），沒有停損保護，賺賠比 <1。
  - ChatGPT 策略：出場條件用「盤中即時價格」比對均線，股價在均線附近
    小幅抖動就反覆巴進巴出——3個月 44 筆交易中有 11 天當天買進當天又被
    出場，摩擦成本吃掉本金 2.67%。
  - Gemini 策略：沒有主動停利機制，浮盈曾衝到 +7.47% 又全部吐回去。

核心差異（相對 ChatGPT／Gemini）：
  1. 用 trade_window.can_trade()（跟 Gemini／量化同一條規則）——一天最多
     一個動作，不管進場／出場／停利／減碼都算，杜絕當日反覆進出。
  2. 均線出場條件必須「連續 2 個交易日」都跌破才確認出場，過濾掉單日
     盤中雜訊造成的假突破。強制風控（資產回撤 10%）不受此限，維持即時。
  3. 出場後至少間隔 2 個交易日才允許重新進場（冷卻期），避免出場後
     訊號一轉正就立刻又衝進去、變相縮短出場確認期的意義。
  4. 單向部位＋三段停利（沿用 ChatGPT 策略中被驗證回撤最小的機制）：
     進場後絕不逢低攤平；浮盈 +5% 先收 30%，+10% 再收 30%，剩餘部位
     用移動停利保護（自高點回落 8% 全清、回落 5% 減半）。
  5. 情緒轉狂熱只降水位、不加碼、不買回（沿用 ChatGPT 的紀律）。

進場（空手時，雙層閘門，與 Gemini／ChatGPT 相同、已驗證的核心邏輯）：
  tier1 ≥ 35 + 大盤 ^TWII 站上 60MA + 00631L 站上 20MA + 資料完整 + 盤中。
  進場規模＝AI 情緒反向水位（<30→80%／30–75→50%／>75→20%）。

資料檔：ai_factors_gemini.json（共用盤前判讀，維持三套策略 A/B/C 對照公平）；
        paper_state_claude.json / paper_trades_claude.json（寫）。
⚠️ 純模擬，非投資建議。
"""
import json
import datetime as dt

import trade_window

AI_FILE = "ai_factors_gemini.json"
STATE_FILE = "paper_state_claude.json"
TRADES_FILE = "paper_trades_claude.json"

START_CAPITAL = 1_000_000
TIER1_GATE = 35              # 量化主審門檻（滿分 60，與另兩套策略相同）

# 三段停利（沿用 ChatGPT 策略中已驗證回撤最小的機制）
STAGE1_PROFIT_TRIGGER = 0.05
STAGE1_SELL_RATIO = 0.30
STAGE2_PROFIT_TRIGGER = 0.10
STAGE2_SELL_RATIO = 0.30

# 移動停利
TRAILING_REDUCE_FROM_HIGH = 0.05
TRAILING_EXIT_FROM_HIGH = 0.08

# 強制風控（即時，不需收盤確認——這是最後一道安全網）
EQUITY_FORCED_DD = 0.10

# 均線出場：連續 N 個交易日確認才出場（過濾單日盤中雜訊）
TREND_BREAK_CONFIRM_DAYS = 2

# 出場後冷卻期：至少間隔 N 個交易日才允許重新進場
REENTRY_COOLDOWN_DAYS = 2

FRENZY_LEVEL = 85
FRENZY_DAYS = 3
REBAL_MIN = 0.05
FEE_RATE = 0.001425
TAX_RATE = 0.001


def load_ai_factors(today):
    """讀盤前 AI 因子（共用 Gemini 盤前判讀，維持三套策略對照公平）。"""
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


def evaluate(quant_pts, market_ok, ai_factors, ma20_ok=None):
    """算分與旗標。market_ok(大盤60MA)／ma20_ok(00631L 20MA)。
    None＝資料未知（不否決進場以外的判斷，避免誤殺）。"""
    tier1 = sum(quant_pts.get(k, 0) for k in ("pattern", "entry", "volume", "vwap"))
    ai_total = ai_factors.get("sector", 10) + ai_factors.get("macro", 5) + ai_factors.get("trend", 5)
    ai_index = round(ai_total / 40 * 100, 1)

    if ai_index < 30:
        sent_target, slabel, szone = 0.8, "🟢 重倉（極度恐慌·逆向）", "green"
    elif ai_index <= 75:
        sent_target, slabel, szone = 0.5, "🟡 標準持倉（情緒健康）", "amber"
    else:
        sent_target, slabel, szone = 0.2, "🟠 輕倉防守（極度狂熱）", "amber"

    data_ok = (market_ok is not None) and (ma20_ok is not None)
    entry_ok = (tier1 >= TIER1_GATE) and (market_ok is True) and (ma20_ok is True)
    return {"tier1": round(tier1), "ai_index": ai_index,
            "sent_target": sent_target, "sent_label": slabel, "sent_zone": szone,
            "entry_ok": entry_ok, "data_ok": data_ok,
            "market_ok": market_ok, "ma20_ok": ma20_ok}


def load_state():
    """讀模擬盤狀態。檔案不存在（首次執行）才視為空、補全新預設值；
    檔案存在但解析失敗（例如 git 衝突標記污染）視為嚴重錯誤、直接往上拋——
    絕不能靜默當成『從沒交易過』並讓呼叫端 save_state() 覆蓋掉真實歷史。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {}
    d = {
        "cash": float(START_CAPITAL), "shares": 0.0, "cost_basis": 0.0,
        "start_capital": float(START_CAPITAL),
        "highest_equity": float(START_CAPITAL), "highest_price_since_entry": 0.0,
        "entry_date": "",
        "profit_stage_1_done": False, "profit_stage_2_done": False,
        "trailing_mode": False,
        "current_target_exposure": 0.0, "realized_pnl": 0.0,
        "frenzy_days": 0,
        "ma20_break_days": 0, "market_break_days": 0,
        "cooldown_remaining": 0,
        "last_date": "", "last_trade_date": "",
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
    if s["shares"] <= 1e-6:
        s["shares"] = 0.0
        s["cost_basis"] = 0.0
        s["highest_price_since_entry"] = 0.0
        s["profit_stage_1_done"] = False
        s["profit_stage_2_done"] = False
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
        "unrealized": round(unreal), "unrealized_pct": round(unreal_pct, 2),
        "realized_pnl": round(s.get("realized_pnl", 0.0)),
        "equity": round(equity), "total_equity": round(equity),
        "market_value": round(mkt),
        "position_pct": round(mkt / equity * 100, 1) if equity else 0,
        "target_exposure": round(s.get("current_target_exposure", 0.0), 3),
        "return_pct": round((equity / start - 1) * 100, 2) if start else 0,
        "trades": s.get("trades", 0),
        "highest_equity": round(s.get("highest_equity", start)),
        "highest_price_since_entry": round(hi, 2) if hi else None,
        "drawdown_from_position_high_pct": round(dd_from_high, 2),
        "position_status": "holding" if shares > 0 else "flat",
        "profit_stage_1_done": s.get("profit_stage_1_done", False),
        "profit_stage_2_done": s.get("profit_stage_2_done", False),
        "trailing_mode": s.get("trailing_mode", False),
        "frenzy_days": s.get("frenzy_days", 0),
        "ma20_break_days": s.get("ma20_break_days", 0),
        "market_break_days": s.get("market_break_days", 0),
        "cooldown_remaining": s.get("cooldown_remaining", 0),
        "last_action": s.get("last_action", ""),
        "last_action_reason": s.get("last_action_reason", ""),
        "tier1": ev.get("tier1"), "ai_index": ev.get("ai_index"),
        "target": ev.get("target"), "zone": ev.get("zone"),
        "zone_label": ev.get("zone_label"), "gate_open": ev.get("gate_open"),
    }


def run(today, now_hm, ev, price):
    """依評估結果 ev 執行波段單向部位＋收盤確認出場＋三段停利。回傳 (events, summary)。"""
    s = load_state()
    events = []

    if price is None or price <= 0:
        s["last_action_reason"] = "DATA_INVALID_NO_TRADE"
        ev = dict(ev)
        ev["target"], ev["zone_label"], ev["zone"], ev["gate_open"] = \
            (s.get("current_target_exposure", 0.0) if s["shares"] > 0 else 0.0), \
            "⚪ 資料缺，不動作", "red", False
        save_state(s)
        return events, _summary(s, price, ev)

    # ---- 每日只更新一次的計數器：狂熱天數／均線連續破位天數／冷卻倒數 ----
    if s.get("last_date") != today:
        s["frenzy_days"] = (s.get("frenzy_days", 0) + 1) if ev["ai_index"] > FRENZY_LEVEL else 0
        s["ma20_break_days"] = (s.get("ma20_break_days", 0) + 1) if ev["ma20_ok"] is False else 0
        s["market_break_days"] = (s.get("market_break_days", 0) + 1) if ev["market_ok"] is False else 0
        s["cooldown_remaining"] = max(0, s.get("cooldown_remaining", 0) - 1)
        s["last_date"] = today

    holding0 = s["shares"] > 0
    target = s.get("current_target_exposure", 0.0)
    if holding0:
        target = ev["sent_target"]
        if s.get("frenzy_days", 0) >= FRENZY_DAYS and target > 0.2:
            target = 0.2
    s["current_target_exposure"] = target if holding0 else 0.0

    total = s["cash"] + s["shares"] * price
    if total > s.get("highest_equity", total):
        s["highest_equity"] = total
    if holding0 and price > s.get("highest_price_since_entry", 0.0):
        s["highest_price_since_entry"] = price

    def _finish(zlabel, zone):
        ev2 = dict(ev)
        eff = s.get("current_target_exposure", 0.0)
        ev2["target"], ev2["zone_label"], ev2["zone"], ev2["gate_open"] = eff, zlabel, zone, s["shares"] > 0
        save_state(s)
        return events, _summary(s, price, ev2)

    in_hours = trade_window.in_market_hours(now_hm)

    # ================= 一天最多一個動作（進場／出場／停利／減碼都算）=================
    if not trade_window.can_trade(s, today, now_hm):
        return _finish(ev["sent_label"] if holding0 else "⚪ 今日已交易過", ev["sent_zone"] if holding0 else "red")

    # ================= 空手：只看進場 =================
    if not holding0:
        if not in_hours:
            return _finish("⚪ 觀望（非盤中）", "red")
        if s.get("cooldown_remaining", 0) > 0:
            return _finish(f"⚪ 出場冷卻中（還剩{s['cooldown_remaining']}個交易日）", "red")
        if not ev["data_ok"]:
            s["last_action_reason"] = "DATA_INVALID_NO_TRADE"
            return _finish("⚪ 資料不足，不進場", "red")
        if not ev["entry_ok"]:
            return _finish("⚪ 觀望（等量化≥35＋趨勢）", "red")
        buy_cash = min(s["cash"] * ev["sent_target"], s["cash"])
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
            s["trailing_mode"] = False
            s["current_target_exposure"] = ev["sent_target"]
            s["last_trade_date"] = today
            _record(s, events, today, now_hm, "BUY", sh, price, ev["sent_target"],
                    "ENTRY_SIGNAL", f"波段進場至 {int(ev['sent_target']*100)}%（{ev['sent_label']}）")
        return _finish(ev["sent_label"], ev["sent_zone"])

    # ================= 持倉：須盤中才能執行賣出 =================
    if not in_hours:
        return _finish(ev["sent_label"], ev["sent_zone"])

    avg = _avg_cost(s) or price
    unreal_pct = price / avg - 1
    hi = s.get("highest_price_since_entry", price)
    dd_from_high = 1 - price / hi if hi else 0

    # ---- 1. 強制風控：資產自高點回撤 10%（即時，不需收盤確認，最後防線）----
    if total / s.get("highest_equity", total) <= (1 - EQUITY_FORCED_DD):
        sh = _sell(s, s["shares"], price); s["last_trade_date"] = today
        # +1：出場當天已經在本次呼叫開頭跑過一次「每日遞減」，隔天才是冷卻期
        # 真正的第一天，所以要多留一次遞減額度，才能確保真的擋滿 N 個交易日。
        s["cooldown_remaining"] = REENTRY_COOLDOWN_DAYS + 1
        _record(s, events, today, now_hm, "SELL", sh, price, 0.0,
                "FORCED_RISK_DRAWDOWN", f"強制風控：總資產自高點回撤≥{int(EQUITY_FORCED_DD*100)}% → 全清")
        return _finish("🔴 資產回撤風控 → 清倉", "red")

    # ---- 2. 均線出場：連續 2 個交易日收盤確認跌破，過濾單日盤中雜訊 ----
    if s.get("ma20_break_days", 0) >= TREND_BREAK_CONFIRM_DAYS:
        sh = _sell(s, s["shares"], price); s["last_trade_date"] = today
        # +1：出場當天已經在本次呼叫開頭跑過一次「每日遞減」，隔天才是冷卻期
        # 真正的第一天，所以要多留一次遞減額度，才能確保真的擋滿 N 個交易日。
        s["cooldown_remaining"] = REENTRY_COOLDOWN_DAYS + 1
        _record(s, events, today, now_hm, "SELL", sh, price, 0.0,
                "EXIT_BELOW_20MA_CONFIRMED",
                f"趨勢破壞確認（連續{TREND_BREAK_CONFIRM_DAYS}日）：00631L 跌破 20MA → 全清")
        return _finish("🔴 連續跌破20MA(已確認) → 清倉", "red")
    if s.get("market_break_days", 0) >= TREND_BREAK_CONFIRM_DAYS:
        sh = _sell(s, s["shares"], price); s["last_trade_date"] = today
        # +1：出場當天已經在本次呼叫開頭跑過一次「每日遞減」，隔天才是冷卻期
        # 真正的第一天，所以要多留一次遞減額度，才能確保真的擋滿 N 個交易日。
        s["cooldown_remaining"] = REENTRY_COOLDOWN_DAYS + 1
        _record(s, events, today, now_hm, "SELL", sh, price, 0.0,
                "EXIT_TWII_BELOW_60MA_CONFIRMED",
                f"趨勢破壞確認（連續{TREND_BREAK_CONFIRM_DAYS}日）：大盤 ^TWII 跌破 60MA → 全清")
        return _finish("🔴 大盤連續跌破季線(已確認) → 清倉", "red")

    # ---- 3. 第一段停利（+5%，賣 30%）----
    if not s.get("profit_stage_1_done") and unreal_pct >= STAGE1_PROFIT_TRIGGER:
        sh = _sell(s, s["shares"] * STAGE1_SELL_RATIO, price)
        s["profit_stage_1_done"] = True
        s["last_trade_date"] = today
        _record(s, events, today, now_hm, "SELL", sh, price, target,
                "STAGE_1_PROFIT_TAKE", f"第一段停利 +{unreal_pct*100:.1f}%：賣 {int(STAGE1_SELL_RATIO*100)}%，剩餘轉移動停利保護")
        return _finish("🟢 第一段停利（移動停利保護）", "green")

    # ---- 4. 第二段停利（+10%，賣 30%）----
    if s.get("profit_stage_1_done") and not s.get("profit_stage_2_done") \
       and unreal_pct >= STAGE2_PROFIT_TRIGGER:
        sh = _sell(s, s["shares"] * STAGE2_SELL_RATIO, price)
        s["profit_stage_2_done"] = True
        s["trailing_mode"] = True
        s["last_trade_date"] = today
        _record(s, events, today, now_hm, "SELL", sh, price, target,
                "STAGE_2_PROFIT_TAKE", f"第二段停利 +{unreal_pct*100:.1f}%：賣 {int(STAGE2_SELL_RATIO*100)}%，轉趨勢追蹤")
        return _finish("🟢 第二段停利（趨勢追蹤）", "green")

    # ---- 5. 移動停利（已收第一段停利後即生效）----
    if s.get("profit_stage_1_done"):
        if dd_from_high >= TRAILING_EXIT_FROM_HIGH:
            sh = _sell(s, s["shares"], price); s["last_trade_date"] = today
            # +1：出場當天已經在本次呼叫開頭跑過一次「每日遞減」，隔天才是冷卻期
            # 真正的第一天，所以要多留一次遞減額度，才能確保真的擋滿 N 個交易日。
            s["cooldown_remaining"] = REENTRY_COOLDOWN_DAYS + 1
            _record(s, events, today, now_hm, "SELL", sh, price, 0.0,
                    "TRAILING_EXIT", f"移動停利：自最高價回落 {dd_from_high*100:.1f}%（≥{int(TRAILING_EXIT_FROM_HIGH*100)}%）→ 全清")
            return _finish("🔴 移動停利 → 清倉", "red")
        if dd_from_high >= TRAILING_REDUCE_FROM_HIGH:
            sh = _sell(s, s["shares"] * 0.5, price)
            s["last_trade_date"] = today
            _record(s, events, today, now_hm, "SELL", sh, price, target,
                    "TRAILING_REDUCE", f"移動停利：自最高價回落 {dd_from_high*100:.1f}%（≥{int(TRAILING_REDUCE_FROM_HIGH*100)}%）→ 減半")
            return _finish("🟠 移動停利減半", "amber")

    # ---- 6. 情緒轉狂熱只降水位（實際曝險高於目標才減碼；絕不加碼/買回）----
    actual = (s["shares"] * price) / total if total else 0
    if actual - target > REBAL_MIN:
        sell_value = (actual - target) * total
        sh = _sell(s, sell_value / price, price); s["last_trade_date"] = today
        _record(s, events, today, now_hm, "SELL", sh, price, target,
                "AI_RISK_REDUCE_ONLY", f"降水位至 {int(target*100)}%（{ev['sent_label']}；不加碼/不買回）")
        return _finish(ev["sent_label"], ev["sent_zone"])

    # 無動作：續抱
    return _finish(ev["sent_label"], ev["sent_zone"])


if __name__ == "__main__":
    # 煙霧測試（不依賴網路）
    q = {"pattern": 20, "entry": 15, "volume": 0, "vwap": 5}  # tier1=40
    ev = evaluate(q, True, {"sector": 4, "macro": 2, "trend": 2}, ma20_ok=True)
    print("evaluate:", ev)
