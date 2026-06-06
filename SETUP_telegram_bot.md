# Telegram 問答機器人（Vercel）設定步驟

這隻機器人讓你**傳訊息問它、它回覆你**（`/start`、`/score`、`/judge`、`/refresh`）。
它跟現有每天自動推播的 GitHub Actions **並存、不衝突**。

程式：`api/telegram.mjs`（Vercel 函式）。下面三步做完就會動。

---

## 步驟 1：把 repo 匯入 Vercel

1. 開 https://vercel.com → 用 GitHub 登入
2. **Add New… → Project**
3. 找到 `afreiv3/00631L_Tw` → **Import**
4. Framework Preset 選 **Other**（其他設定都不用改）→ **Deploy**
5. 部署完會給你一個網址，例如 `https://00631l-tw.vercel.app`，**記下來**

> Vercel 會自動把 `api/telegram.mjs` 變成一個網址：`你的網址/api/telegram`，這就是 webhook。

## 步驟 2：在 Vercel 設環境變數

進這個專案 → **Settings → Environment Variables**，新增：

| Name | Value | 必填 |
|------|-------|------|
| `TG_TOKEN` | 你的 Telegram bot token（跟 GitHub secret 同一個） | ✅ |
| `ALLOWED_CHAT` | 你的 chat_id（跟 GitHub 的 `TG_CHAT` 同一個） | 建議 |
| `WEBHOOK_SECRET` | 自訂一串亂碼（防偽造）。若設了，步驟 3 也要在 GitHub 設同樣的 | 選填 |
| `GH_PAT` | fine-grained PAT（只有要用 `/refresh` 才需要） | 選填 |

> `GH_OWNER`、`GH_REPO`、`GH_BRANCH`、`PANEL_URL` 都有預設值，通常不用設。
> 改完環境變數後，到 **Deployments** 對最新一筆按 **Redeploy** 讓它生效。

## 步驟 3：註冊 webhook（告訴 Telegram 把訊息送到 Vercel）

1. GitHub → repo → **Actions** → 左側 **「設定 Telegram Webhook」**
2. 右上 **Run workflow**
3. `vercel_url` 填步驟 1 記下的網址（例 `https://00631l-tw.vercel.app`，結尾不要加 `/`）→ **Run**
4. 跑完點進去看 log，出現 `"ok":true` 就成功了

> 若步驟 2 有設 `WEBHOOK_SECRET`，請先到 GitHub → Settings → Secrets and variables → Actions
> 新增同名 secret `WEBHOOK_SECRET`，值要跟 Vercel 那個**一模一樣**。

---

## 測試

打開 Telegram，對你的機器人輸入 `/start` → 應該秒回自我介紹。
再試 `/score`、`/judge`。

## 常見問題

- **沒反應？** 重跑步驟 3 的 workflow，看 `getWebhookInfo` 的 `last_error_message`。
- **回「未授權的對話」？** `ALLOWED_CHAT` 填錯了，查詢類指令才會擋；`/start`、`/help` 一律放行。
- **`/refresh` 說沒設 GH_PAT？** 正常，那是選用功能；要用再去 Vercel 補 `GH_PAT`。
- **每天的推播會受影響嗎？** 不會。推播走 `sendMessage`，跟 webhook 是兩回事。
