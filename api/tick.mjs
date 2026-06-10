/**
 * 外部排程觸發點（給 cron-job.org 等可靠排程每 15 分鐘打一次）
 * --------------------------------------------------------------
 * 打這個網址 → 用 GH_PAT 對 repo 發 repository_dispatch(manual_refresh)
 * → 觸發 monitor.yml 重算。比 GitHub 內建 cron 可靠很多。
 *
 * 安全：設了 CRON_SECRET 時，必須帶 ?key=<CRON_SECRET> 才放行（擋亂打）。
 * 只在台股盤中時段（台北 09:00–13:30、週一~五）才真的觸發，其餘回 skip。
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

  // 只在台股盤中觸發（台北 = UTC+8）。TICK_FORCE=1 可略過。
  if (process.env.TICK_FORCE !== "1") {
    const now = new Date(Date.now() + 8 * 3600 * 1000); // 台北
    const day = now.getUTCDay();           // 0=日 6=六
    const mins = now.getUTCHours() * 60 + now.getUTCMinutes();
    const open = day >= 1 && day <= 5 && mins >= 9 * 60 && mins <= 13 * 60 + 35;
    if (!open) {
      return res.status(200).json({ ok: true, skipped: "非盤中時段" });
    }
  }

  const r = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/dispatches`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${PAT}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "tick-cron",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ event_type: "manual_refresh" }),
  });
  if (r.ok) return res.status(200).json({ ok: true, triggered: true });
  const detail = await r.text().catch(() => "");
  return res.status(502).json({ ok: false, status: r.status, detail: detail.slice(0, 200) });
}
