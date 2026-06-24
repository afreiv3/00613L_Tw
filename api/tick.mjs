/**
 * 外部排程觸發點（給 cron-job.org 等可靠排程每 15 分鐘打一次）
 * --------------------------------------------------------------
 * 打這個網址 → 用 GH_PAT 依台北時段觸發對應 workflow，比 GitHub 內建 cron 可靠很多：
 *   盤前 08:00–08:59 → workflow_dispatch 觸發 gemini_preopen（Gemini 盤前判讀）
 *   開盤初 09:00–09:20 → 補觸發一次 gemini_preopen 當保險（盤前漏打時兜底）＋ monitor
 *   盤中 09:00–13:35 → repository_dispatch(manual_refresh) 觸發 monitor 重算
 *
 * 安全：設了 CRON_SECRET 時，必須帶 ?key=<CRON_SECRET> 才放行（擋亂打）。
 * 只在台股盤前/盤中（週一~五）才真的觸發，其餘回 skip。
 *
 * Vercel 環境變數：GH_PAT（已設）、CRON_SECRET（自訂，建議設）、
 *   GH_OWNER/GH_REPO（預設 afreiv3 / 00631L_Tw）、TICK_FORCE=1 可忽略時段限制（測試用）。
 */
export default async function handler(req, res) {
  const OWNER = process.env.GH_OWNER || "afreiv3";
  const REPO = process.env.GH_REPO || "00631L_Tw";
  const PAT = process.env.GH_PAT || "";
  const SECRET = process.env.CRON_SECRET || "";

  // 驗證金鑰（有設才檢查）
  const key = (req.query && req.query.key) || "";
  if (SECRET && key !== SECRET) {
    return res.status(403).json({ ok: false, error: "forbidden" });
  }
  if (!PAT) {
    return res.status(500).json({ ok: false, error: "缺 GH_PAT" });
  }

  // 台北時間（UTC+8）判斷該做什麼：盤前跑 Gemini 判讀、盤中跑 monitor。
  const now = new Date(Date.now() + 8 * 3600 * 1000);
  const day = now.getUTCDay();                       // 0=日 6=六
  const mins = now.getUTCHours() * 60 + now.getUTCMinutes();
  const weekday = day >= 1 && day <= 5;
  const force = process.env.TICK_FORCE === "1";

  // 盤前窗（08:00–08:59 台北，放寬以提高 cron-job.org 命中率）：觸發 Gemini 盤前判讀。
  const preopen = weekday && mins >= 8 * 60 && mins <= 8 * 60 + 59;
  // 開盤初保險（09:00–09:20 台北）：盤前若沒打到（GitHub 內建 cron 常延遲數小時），
  // 在開盤後第一時間補觸發一次 Gemini 判讀，確保盤中決策用得到「當天」的 AI 因子。
  const earlyMkt = weekday && mins >= 9 * 60 && mins <= 9 * 60 + 20;
  // 盤中（09:00–13:35 台北）：觸發 monitor 重算
  const market = weekday && mins >= 9 * 60 && mins <= 13 * 60 + 35;

  if (!force && !preopen && !market) {
    return res.status(200).json({ ok: true, skipped: "非盤前/盤中時段" });
  }

  if (preopen && !market) {
    // 盤前：只觸發 Gemini 判讀（每天一次，重複觸發無害——隔日/同日 upsert 覆蓋）
    const r = await dispatchGemini(OWNER, REPO, PAT);
    if (r.ok) return res.status(200).json({ ok: true, triggered: "gemini_preopen" });
    const d = await r.text().catch(() => "");
    return res.status(502).json({ ok: false, status: r.status, detail: d.slice(0, 200) });
  }

  // 開盤初：盡力而為補觸發一次 Gemini 判讀（不讓它的成敗擋住 monitor）。
  if (earlyMkt) {
    await dispatchGemini(OWNER, REPO, PAT).catch(() => {});
  }

  // 盤中：repository_dispatch 觸發 monitor
  const r = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/dispatches`, {
    method: "POST", headers: ghH(PAT),
    body: JSON.stringify({ event_type: "manual_refresh" }),
  });
  const tag = earlyMkt ? "monitor+gemini_preopen(保險)" : "monitor";
  if (r.ok) return res.status(200).json({ ok: true, triggered: tag });
  const detail = await r.text().catch(() => "");
  return res.status(502).json({ ok: false, status: r.status, detail: detail.slice(0, 200) });
}

// workflow_dispatch 觸發 Gemini 盤前判讀 workflow
function dispatchGemini(OWNER, REPO, PAT) {
  return fetch(`https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/gemini_preopen.yml/dispatches`, {
    method: "POST", headers: ghH(PAT),
    body: JSON.stringify({ ref: process.env.GH_BRANCH || "main" }),
  });
}

function ghH(pat) {
  return {
    "Authorization": `Bearer ${pat}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "tick-cron",
    "Content-Type": "application/json",
  };
}
