/**
 * 00631L Telegram 機器人（Vercel Serverless Function）
 * ----------------------------------------------------------------
 * 你傳指令 → 這支函式讀 GitHub raw 上最新 JSON → 回覆你。
 * 這是「接收＋回覆」的那一隻；每天主動推播的是 GitHub Actions，兩者並存不衝突。
 *
 * 由 worker.js（原 Cloudflare 草稿）移植而來，改成 Vercel 的 req/res 介面。
 *
 * 指令：
 *   /start 或 /help → 自我介紹＋指令說明
 *   /score 或 /s    → 最新「量化代理分數」＋七因子（讀 dashboard_data.json）
 *   /judge 或 /j    → 最新「AI 判讀」摘要（讀 judgment_log.json，若有）
 *   /refresh 或 /r  → 觸發 GitHub Actions 重算一次（需 GH_PAT，走 repository_dispatch）
 *
 * Vercel 環境變數（Project → Settings → Environment Variables）：
 *   TG_TOKEN        Telegram bot token（必填）
 *   WEBHOOK_SECRET  自訂字串；setWebhook 時帶同一個，用來擋偽造請求（選填）
 *   ALLOWED_CHAT    只允許這個 chat_id 使用查詢（你的 TG_CHAT，選填但建議設）
 *   GH_OWNER        GitHub 帳號（預設 afreiv3）
 *   GH_REPO         repo 名（預設 00631L_Tw）
 *   GH_BRANCH       分支（預設 main）
 *   GH_PAT          （選填，/refresh 才需要）fine-grained PAT，授權該 repo 的 Contents:read + Actions
 */

const CFG = {
  TG_TOKEN: process.env.TG_TOKEN || "",
  WEBHOOK_SECRET: process.env.WEBHOOK_SECRET || "",
  ALLOWED_CHAT: process.env.ALLOWED_CHAT || "",
  GH_OWNER: process.env.GH_OWNER || "afreiv3",
  GH_REPO: process.env.GH_REPO || "00631L_Tw",
  GH_BRANCH: process.env.GH_BRANCH || "main",
  GH_PAT: process.env.GH_PAT || "",
};
const PANEL_URL = process.env.PANEL_URL ||
  "https://afreiv3.github.io/00631L_Tw/dashboard.html";

const GREETING =
  "👋 嗨！我是 00631L 盤面助手。\n\n" +
  "我每天自動幫你盯「元大台灣50正2（00631L）」：\n" +
  "☀️ 盤前 — AI 讀美股／台股新聞，給環境評分\n" +
  "📊 盤中 — 每 30 分量化評分，總分達 70 才主動通知買點\n" +
  "🌙 盤後 — 當日總結\n\n" +
  "你也可以隨時問我：\n" +
  "/C Claude 策略現況（總分70制）\n" +
  "/G Gemini 策略現況（雙層閘門）\n" +
  "/score 量化七項因子明細\n" +
  "/judge 最新 AI 判讀摘要\n" +
  "/refresh 立刻重算一次\n" +
  "/stop 取消自動推播\n" +
  "/help 指令說明\n\n" +
  "📈 完整面板 → " + PANEL_URL + "\n\n" +
  "⚠️ 代理規則訊號，非投資建議。";

export default async function handler(req, res) {
  // 健康檢查 / 瀏覽器直接打開
  if (req.method !== "POST") {
    return res.status(200).send("00631L bot webhook OK");
  }
  // 驗證 Telegram 來源（有設 WEBHOOK_SECRET 才檢查）
  if (CFG.WEBHOOK_SECRET &&
      req.headers["x-telegram-bot-api-secret-token"] !== CFG.WEBHOOK_SECRET) {
    return res.status(403).send("forbidden");
  }

  const update = req.body || {};
  const msg = update.message || update.edited_message;
  if (!msg || !msg.text) return res.status(200).send("ok");
  const chatId = String(msg.chat.id);

  // 公開機器人：/start /help /score /judge 任何人都能用。
  // 只有 /refresh（會觸發重算的管理動作）限擁有者（ALLOWED_CHAT），沒設則一律放行。
  const cmd = msg.text.trim().toLowerCase().split(/\s+/)[0].replace(/@.*$/, "");
  const isOwner = !CFG.ALLOWED_CHAT || chatId === String(CFG.ALLOWED_CHAT);

  try {
    if (cmd === "/c") {
      await tg(chatId, await fmtClaude());
    } else if (cmd === "/g") {
      await tg(chatId, await fmtGemini());
    } else if (cmd === "/score" || cmd === "/s") {
      await tg(chatId, await fmtQuant());
    } else if (cmd === "/judge" || cmd === "/j") {
      await tg(chatId, await fmtJudge());
    } else if (cmd === "/refresh" || cmd === "/r") {
      await tg(chatId, isOwner ? await triggerRefresh() : "🔒 /refresh 僅限管理者使用。");
    } else if (cmd === "/start" || cmd === "/help") {
      await tg(chatId, GREETING);
      if (cmd === "/start") {
        const r = await subscribe(chatId, true);
        if (r === "added") {
          await tg(chatId, "✅ 已開啟自動推播！盤前分析與買賣通知會自動傳給你。輸入 /stop 可隨時取消。");
        } else if (r === "already") {
          await tg(chatId, "（你已在訂閱名單中，會持續收到推播）");
        } else if (r === null) {
          await tg(chatId, "⚠️ 訂閱功能尚未啟用：伺服器讀不到 GH_PAT（請確認 Vercel 已設 GH_PAT 並 Redeploy）。");
        } else {
          await tg(chatId, "⚠️ 訂閱失敗 → " + String(r).replace(/^ERR:/, "") +
            "\n（403＝權限不足、404＝沒選到此 repo）");
        }
      }
    } else if (cmd === "/stop" || cmd === "/unsubscribe") {
      const r = await subscribe(chatId, false);
      await tg(chatId, r === "removed"
        ? "🔕 已取消訂閱，不再收到自動推播。隨時可再 /start 開啟。"
        : "你目前沒有在訂閱名單中。");
    } else {
      await tg(chatId, "不認得的指令。試 /start、/C、/G、/score、/judge。");
    }
  } catch (e) {
    await tg(chatId, "查詢失敗：" + (e.message || e));
  }
  return res.status(200).send("ok");
}

const ZONE = { green: "🟢 進場區", amber: "🟡 觀望區", red: "🔴 不進區" };
const FACTORS = {
  pattern: "型態健康", sector: "板塊偏強", entry: "近支撐",
  volume: "量能合理", vwap: "站穩關鍵價", macro: "無系統風險", trend: "大盤結構",
};
const ORDER = ["pattern", "sector", "entry", "volume", "vwap", "macro", "trend"];

function rawURL(file) {
  return `https://raw.githubusercontent.com/${CFG.GH_OWNER}/${CFG.GH_REPO}/${CFG.GH_BRANCH}/${file}?t=${Date.now()}`;
}
async function getJSON(file) {
  const r = await fetch(rawURL(file), { headers: { "Cache-Control": "no-cache" } });
  if (!r.ok) throw new Error(`讀不到 ${file} (${r.status})`);
  return r.json();
}

const nf = (n) => (n == null ? "—" : Number(n).toLocaleString());

async function fmtClaude() {
  const d = await getJSON("dashboard_data.json");
  const last = (d.history || []).slice(-1)[0];
  if (!last) return "尚無 Claude 策略資料。";
  const p = last.paper;
  let s = `🟦 [Claude 策略] ${last.date} ${last.time}\n` +
          `現價 ${last.price ?? "—"}｜總分 ${last.score}/100｜${ZONE[last.zone] || ""}\n` +
          `（量化${last.quant_sum ?? "—"}＋AI${last.ai_sum ?? "—"}；門檻70 進場、停損8%/停利15%）`;
  if (p) {
    s += `\n\n💰 模擬盤（虛擬100萬）\n` +
         `總資產 ${nf(p.equity)}｜報酬 ${p.return_pct >= 0 ? "+" : ""}${p.return_pct}%\n` +
         `持倉 ${p.holding ? (p.position.shares + " 股 @ " + p.position.entry_price) : "空手"}｜` +
         `交易 ${p.trades} 次｜勝率 ${p.trades ? p.win_rate + "%" : "—"}`;
  }
  return s + `\n📊 → ${PANEL_URL}\n⚠️ 非投資建議。`;
}

async function fmtGemini() {
  let d;
  try { d = await getJSON("dashboard_data_gemini.json"); }
  catch { return "尚無 Gemini 策略資料（尚未產生）。"; }
  const last = (d.history || []).slice(-1)[0];
  if (!last) return "尚無 Gemini 策略資料。";
  const p = last.paper;
  const mk = last.market_ok === true ? "站上季線✅" : (last.market_ok === false ? "跌破季線❌" : "資料不足");
  let s = `🟩 [Gemini 策略] ${last.date} ${last.time}\n` +
          `現價 ${last.price ?? "—"}\n` +
          `第一層 量化主審 → ${last.gate_open ? "視窗開啟" : "視窗關閉"}\n` +
          `• 盤中量化 ${last.tier1 ?? "—"}/60（需≥35）\n` +
          `• 大盤趨勢 ${mk}\n` +
          `第二層 AI情緒指數 ${last.ai_index ?? "—"}/100\n` +
          `→ 目標水位 ${last.target != null ? Math.round(last.target * 100) + "%" : "—"}｜${last.zone_label || ""}`;
  if (last.summary) s += `\n🧠 ${last.summary}`;
  if (p) {
    s += `\n\n💰 動態模擬盤（虛擬100萬）\n` +
         `總資產 ${nf(p.equity)}｜報酬 ${p.return_pct >= 0 ? "+" : ""}${p.return_pct}%\n` +
         `目前水位 ${p.position_pct}%｜再平衡 ${p.trades} 次`;
  }
  return s + `\n📊 → ${PANEL_URL}\n⚠️ 非投資建議。`;
}

async function fmtQuant() {
  const d = await getJSON("dashboard_data.json");
  const last = (d.history || []).slice(-1)[0];
  if (!last) return "尚無量化資料。";
  const lines = ORDER.map((k) => {
    const f = last.factors?.[k];
    if (!f) return null;
    const mark = f.pts >= f.max ? "✓" : (f.pts > 0 ? "½" : "✗");
    return `${mark} ${FACTORS[k]} ${f.pts}/${f.max}${f.note ? " — " + f.note : ""}`;
  }).filter(Boolean).join("\n");
  return `📊 [量化] ${last.date} ${last.time}\n現價 ${last.price ?? "—"}｜${last.score}/100｜${ZONE[last.zone] || ""}\n\n${lines}\n\n⚠️ 代理規則訊號，非投資建議。`;
}

async function fmtJudge() {
  let d;
  try { d = await getJSON("judgment_log.json"); }
  catch { return "尚無 AI 判讀資料（judgment_log.json 不存在）。"; }
  const last = (d.history || []).slice(-1)[0];
  if (!last) return "尚無 AI 判讀資料。";
  const lines = (last.lines || []).map((t) => "• " + t).join("\n");
  return `🧠 [判讀] ${last.date} ${last.time}\n${ZONE[last.zone] || ""}｜${last.score}/100\n\n${last.summary || ""}\n\n${lines}\n\n⚠️ 盤面分析非投資建議；不輸出勝率%。`;
}

async function triggerRefresh() {
  if (!CFG.GH_PAT) return "未設定 GH_PAT，無法觸發重算（/score、/judge 仍可用）。";
  const r = await fetch(`https://api.github.com/repos/${CFG.GH_OWNER}/${CFG.GH_REPO}/dispatches`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${CFG.GH_PAT}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "tg-vercel-bot",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ event_type: "manual_refresh" }),
  });
  return r.ok ? "🔄 已觸發重算，約 1 分鐘後用 /score 查最新。" : `觸發失敗 (${r.status})`;
}

// ===== 自動訂閱：把訂閱者 chat_id 存進 repo 的 subscribers.json（需 GH_PAT）=====
const SUBS_FILE = "subscribers.json";
function ghHeaders() {
  return {
    "Authorization": `Bearer ${CFG.GH_PAT}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "tg-vercel-bot",
    "Content-Type": "application/json",
  };
}
async function ghGetSubs() {
  const url = `https://api.github.com/repos/${CFG.GH_OWNER}/${CFG.GH_REPO}/contents/${SUBS_FILE}?ref=${CFG.GH_BRANCH}&t=${Date.now()}`;
  const r = await fetch(url, { headers: ghHeaders() });
  if (r.status === 404) return { ids: [], sha: null };
  if (!r.ok) { let d = ""; try { d = (await r.text()).slice(0, 120); } catch {} throw new Error(`GET ${r.status} ${d}`); }
  const j = await r.json();
  const obj = JSON.parse(Buffer.from(j.content, "base64").toString("utf-8"));
  return { ids: (obj.chat_ids || []).map(String), sha: j.sha };
}
async function ghPutSubs(ids, sha, note) {
  const url = `https://api.github.com/repos/${CFG.GH_OWNER}/${CFG.GH_REPO}/contents/${SUBS_FILE}`;
  const content = JSON.stringify({ chat_ids: ids, updated: new Date().toISOString() }, null, 2);
  const body = {
    message: `subs ${note} [skip ci]`,
    content: Buffer.from(content, "utf-8").toString("base64"),
    branch: CFG.GH_BRANCH,
  };
  if (sha) body.sha = sha;
  const r = await fetch(url, { method: "PUT", headers: ghHeaders(), body: JSON.stringify(body) });
  if (r.ok) return { ok: true };
  let detail = ""; try { detail = (await r.text()).slice(0, 160); } catch {}
  return { ok: false, status: r.status, detail };
}
// add=true 訂閱、false 退訂。回傳 added/removed/already/absent/null(無PAT)/"ERR:..."
async function subscribe(chatId, add) {
  if (!CFG.GH_PAT) return null;
  const id = String(chatId);
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      const { ids, sha } = await ghGetSubs();
      const has = ids.includes(id);
      if (add && has) return "already";
      if (!add && !has) return "absent";
      const next = add ? [...ids, id] : ids.filter((x) => x !== id);
      const put = await ghPutSubs(next, sha, add ? `+${id}` : `-${id}`);
      if (put.ok) return add ? "added" : "removed";
      if (put.status !== 409) return `ERR:寫入 HTTP ${put.status} ${put.detail}`;
      // 409 競態 → 重抓 sha 再試一次
    }
    return "ERR:重試後仍 409 衝突";
  } catch (e) {
    return "ERR:" + (e.message || e);
  }
}

async function tg(chatId, text) {
  await fetch(`https://api.telegram.org/bot${CFG.TG_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }),
  });
}
