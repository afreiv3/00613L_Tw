/**
 * 00631L Telegram 即時查詢 Worker（Cloudflare Workers）
 * ----------------------------------------------------------------
 * 你傳指令 → Worker 讀 GitHub raw 上最新 JSON → 秒回。
 *
 * 指令：
 *   /score 或 /s   → 最新「量化代理分數」+ 七因子（讀 dashboard_data.json）
 *   /judge 或 /j   → 最新「LLM 判讀」摘要（讀 judgment_log.json，若有）
 *   /refresh 或 /r → 觸發 GitHub Actions 重算一次（需 GH_PAT，走 repository_dispatch）
 *   /help          → 說明
 *
 * Cloudflare 環境變數（Settings → Variables，敏感者用 Encrypt）：
 *   TG_TOKEN        Telegram bot token
 *   WEBHOOK_SECRET  自訂字串；setWebhook 時帶同一個，用來擋偽造請求
 *   GH_OWNER        GitHub 帳號
 *   GH_REPO         repo 名
 *   GH_BRANCH       例如 main
 *   ALLOWED_CHAT    只允許這個 chat_id 使用（你的 TG_CHAT）
 *   GH_PAT          （選用，/refresh 才需要）fine-grained PAT，授權該 repo 的 "Contents: read" + "Actions" 視需求
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok"); // 健康檢查
    // 驗證 Telegram 來源
    if (env.WEBHOOK_SECRET &&
        request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try { update = await request.json(); } catch { return new Response("bad json", { status: 400 }); }

    const msg = update.message || update.edited_message;
    if (!msg || !msg.text) return new Response("ok");
    const chatId = String(msg.chat.id);

    // 限定只有你能用
    if (env.ALLOWED_CHAT && chatId !== String(env.ALLOWED_CHAT)) {
      await tg(env, chatId, "未授權的對話。");
      return new Response("ok");
    }

    const cmd = msg.text.trim().toLowerCase().split(/\s+/)[0].replace(/@.*$/, "");

    try {
      if (cmd === "/score" || cmd === "/s") {
        await tg(env, chatId, await fmtQuant(env));
      } else if (cmd === "/judge" || cmd === "/j") {
        await tg(env, chatId, await fmtJudge(env));
      } else if (cmd === "/refresh" || cmd === "/r") {
        await tg(env, chatId, await triggerRefresh(env));
      } else if (cmd === "/start" || cmd === "/help") {
        await tg(env, chatId,
          "00631L 查詢機器人\n/score 量化分數＋因子\n/judge LLM 判讀摘要\n/refresh 觸發重算\n\n⚠️ 代理規則訊號，非投資建議。");
      } else {
        await tg(env, chatId, "不認得的指令。試 /score、/judge、/refresh。");
      }
    } catch (e) {
      await tg(env, chatId, "查詢失敗：" + (e.message || e));
    }
    return new Response("ok");
  }
};

const ZONE = { green: "🟢 進場區", amber: "🟡 觀望區", red: "🔴 不進區" };
const FACTORS = {
  pattern: "型態健康", sector: "板塊偏強", entry: "近支撐",
  volume: "量能合理", vwap: "站穩關鍵價", macro: "無系統風險", trend: "大盤結構"
};
const ORDER = ["pattern", "sector", "entry", "volume", "vwap", "macro", "trend"];

function rawURL(env, file) {
  return `https://raw.githubusercontent.com/${env.GH_OWNER}/${env.GH_REPO}/${env.GH_BRANCH}/${file}?t=${Date.now()}`;
}
async function getJSON(env, file) {
  const r = await fetch(rawURL(env, file), { cf: { cacheTtl: 0 } });
  if (!r.ok) throw new Error(`讀不到 ${file} (${r.status})`);
  return r.json();
}

async function fmtQuant(env) {
  const d = await getJSON(env, "dashboard_data.json");
  const last = (d.history || []).slice(-1)[0];
  if (!last) return "尚無量化資料。";
  const lines = ORDER.map(k => {
    const f = last.factors?.[k]; if (!f) return null;
    const mark = f.pts >= f.max ? "✓" : (f.pts > 0 ? "½" : "✗");
    return `${mark} ${FACTORS[k]} ${f.pts}/${f.max}${f.note ? " — " + f.note : ""}`;
  }).filter(Boolean).join("\n");
  return `📊 [量化] ${last.date} ${last.time}\n現價 ${last.price ?? "—"}｜${last.score}/100｜${ZONE[last.zone] || ""}\n\n${lines}\n\n⚠️ 代理規則訊號，非投資建議。`;
}

async function fmtJudge(env) {
  let d;
  try { d = await getJSON(env, "judgment_log.json"); }
  catch { return "尚無 LLM 判讀資料（judgment_log.json 不存在）。"; }
  const last = (d.history || []).slice(-1)[0];
  if (!last) return "尚無 LLM 判讀資料。";
  const lines = (last.lines || []).map(t => "• " + t).join("\n");
  return `🧠 [判讀] ${last.date} ${last.time}\n${ZONE[last.zone] || ""}｜${last.score}/100\n\n${last.summary || ""}\n\n${lines}\n\n⚠️ 盤面分析非投資建議；不輸出勝率%。`;
}

async function triggerRefresh(env) {
  if (!env.GH_PAT) return "未設定 GH_PAT，無法觸發重算。";
  const r = await fetch(`https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}/dispatches`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GH_PAT}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "tg-worker",
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ event_type: "manual_refresh" })
  });
  return r.ok ? "🔄 已觸發重算，約 1 分鐘後用 /score 查最新。" : `觸發失敗 (${r.status})`;
}

async function tg(env, chatId, text) {
  await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true })
  });
}
