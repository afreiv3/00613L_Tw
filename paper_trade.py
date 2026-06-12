#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模擬盤（紙上交易）：用 monitor 的合併總分自動模擬下單，純記錄、不碰真錢。

規則（v1，2026-06-06 定）：
  進場：空手且總分 >= 70 → 買進；投入比例依分數分檔（以「目前現金」計）
        70-79 → 30%、80-89 → 50%、90+ → 70%（槓桿商品刻意不全壓）
  出場：持有時，停損 -8% / 停利 +15% / 總分 < 50，三者先到先走
  其他：起始資金 100 萬；一次只持有一個部位；含手續費與證交稅（貼近真實）。

⚠️ 這是用來「驗證評分系統準不準」的工具，不是投資建議。
"""
import json
import datetime as dt  # noqa: F401  (保留給未來擴充)

import trade_window

STATE_FILE = "paper_state.json"
TRADES_FILE = "paper_trades.json"

START_CAPITAL = 1_000_000   # 起始虛擬資金
BUY_THRESHOLD = 70          # 進場門檻
EXIT_SCORE = 50             # 總分跌破此值 → 訊號轉弱出場
STOP_LOSS = -0.08           # 停損 -8%
TAKE_PROFIT = 0.15          # 停利 +15%
FEE_RATE = 0.001425         # 券商手續費（單邊）
TAX_RATE = 0.001            # 證交稅（ETF 賣出，0.1%）


def _tier_fraction(score):
    """依分數決定投入現金比例。"""
    if score >= 90:
        return 0.70
    if score >= 80:
        return 0.50
    return 0.30  # 70-79


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"cash": float(START_CAPITAL), "position": None,
                "start_capital": float(START_CAPITAL), "realized_pnl": 0.0,
                "trades": 0, "wins": 0}


def save_state(s):
    s["updated"] = (dt.datetime.now(dt.timezone.utc) +
                    dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def _append_trade(rec):
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


def _summary(s, price):
    pos = s.get("position")
    mkt = pos["shares"] * price if (pos and price) else 0
    equity = s["cash"] + mkt
    start = s.get("start_capital", START_CAPITAL)
    ret_pct = (equity / start - 1) * 100 if start else 0
    trades = s.get("trades", 0)
    win_rate = (s.get("wins", 0) / trades * 100) if trades else 0
    return {
        "cash": round(s["cash"]),
        "equity": round(equity),
        "market_value": round(mkt),
        "return_pct": round(ret_pct, 2),
        "holding": bool(pos),
        "position": pos,
        "trades": trades,
        "wins": s.get("wins", 0),
        "win_rate": round(win_rate, 1),
        "realized_pnl": round(s.get("realized_pnl", 0.0)),
    }


def run(today, now_hm, total, price):
    """用當下總分與現價跑一次模擬下單。
    回傳 (event, summary, trade)：event 為 'BUY'/'SELL'/None；trade 為該筆紀錄或 None。"""
    s = load_state()
    if price is None or price <= 0:
        return None, _summary(s, price), None   # 沒有有效價格就不動作

    event = None
    trade = None
    pos = s.get("position")

    # 波段規則：只在盤中、且今天還沒交易過，才允許下單（收盤後只更新畫面）
    if not trade_window.can_trade(s, today, now_hm):
        save_state(s)
        return None, _summary(s, price), None

    if pos:
        # ---- 持有中：檢查出場 ----
        chg = price / pos["entry_price"] - 1
        reason = None
        if chg <= STOP_LOSS:
            reason = f"停損 {chg*100:+.1f}%"
        elif chg >= TAKE_PROFIT:
            reason = f"停利 {chg*100:+.1f}%"
        elif total < EXIT_SCORE:
            reason = f"訊號轉弱（{total}<{EXIT_SCORE}）"
        if reason:
            gross = pos["shares"] * price
            proceeds = gross - gross * FEE_RATE - gross * TAX_RATE
            pnl = proceeds - pos["cost"]
            s["cash"] += proceeds
            s["realized_pnl"] = s.get("realized_pnl", 0.0) + pnl
            s["trades"] = s.get("trades", 0) + 1
            if pnl > 0:
                s["wins"] = s.get("wins", 0) + 1
            s["position"] = None
            event = "SELL"
            trade = {"date": today, "time": now_hm, "action": "SELL",
                     "price": round(price, 2), "shares": pos["shares"],
                     "amount": round(proceeds), "score": total, "reason": reason,
                     "pnl": round(pnl), "pnl_pct": round(chg * 100, 2)}
            _append_trade(trade)
    elif total >= BUY_THRESHOLD:
        # ---- 空手：檢查進場 ----
        frac = _tier_fraction(total)
        budget = s["cash"] * frac
        shares = int(budget // price)
        if shares > 0:
            gross = shares * price
            cost = gross + gross * FEE_RATE   # 買進成本含手續費
            if cost <= s["cash"]:
                s["cash"] -= cost
                s["position"] = {"shares": shares, "entry_price": round(price, 2),
                                 "entry_date": today, "entry_time": now_hm,
                                 "cost": round(cost, 2), "entry_score": total,
                                 "frac": frac}
                event = "BUY"
                trade = {"date": today, "time": now_hm, "action": "BUY",
                         "price": round(price, 2), "shares": shares,
                         "amount": round(cost), "score": total,
                         "reason": f"總分{total}≥{BUY_THRESHOLD}，投入{int(frac*100)}%"}
                _append_trade(trade)

    if event:                       # 今天動作過 → 鎖住，當日不再交易
        s["last_trade_date"] = today
    save_state(s)
    return event, _summary(s, price), trade


if __name__ == "__main__":
    # 手動煙霧測試（不寫檔，只示範一次進場/出場流程）
    print("paper_trade 自測：", _tier_fraction(72), _tier_fraction(85), _tier_fraction(95))
