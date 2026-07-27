# 儀表板即時更新 — 完整設定流程

> 為什麼要這份文件：GitHub 免費版的排程（cron）會被延遲甚至直接丟棄。實測 `*/10` 幾乎不執行，
> data.json 實際更新間隔是 **2–6 小時**，2026-07-27 台股整個盤中更是零次自動更新。
> 這是平台限制，改程式碼救不了。以下三步把「即時」拿回來。

---

## 步驟 1：建 GitHub 權杖（5 分鐘，手機可做）

1. 手機瀏覽器開 GitHub → 右上頭像 → **Settings** → 最下面 **Developer settings**
2. **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
3. 這樣填：
   - Token name：`dashboard-phone`
   - Expiration：**1 year**（到期要重設，行事曆提醒一下）
   - Repository access：**Only select repositories** → 勾 `investment-hub`
   - Permissions → Repository permissions：
     - **Actions：Read and write** ← 必要，沒有這個就無法觸發抓價
     - **Contents：Read and write** ← 建議，自選股編輯會直接生效不必繞路
4. 產生後**立刻複製**（只顯示一次）
5. 回儀表板 → 最下方 **🔑 設定／更新權杖** → 貼上 → 儲存

> 權杖只存在你手機的 localStorage，不會進 repo。之後若看到「⚠️ 權杖失效已清除」，就是過期了，重做步驟 1。

**驗收**：按右上 **⟳ 更新** → 應出現「觸發即時抓價…」→「抓價中…4s、8s…」→ 約 60–90 秒後「✅ 行情已更新」。
若顯示「⚠️ 未設權杖，只能讀存檔」，代表權杖沒存進去或權限漏了 Actions。

---

## 步驟 2：外接排程，取代 GitHub 不可靠的 cron（10 分鐘，一次性）

用免費的 **cron-job.org** 定時去戳 GitHub 的 workflow_dispatch API——它不受 GitHub 排程丟棄的影響。

1. 到 cron-job.org 註冊免費帳號（免費版：50 個排程、最小 1 分鐘間隔）
2. **Create cronjob**，這樣填：

| 欄位 | 值 |
|---|---|
| Title | 台股盤中抓價 |
| URL | `https://api.github.com/repos/lin7125/investment-hub/actions/workflows/refresh.yml/dispatches` |
| Schedule | 自訂：週一–週五，09:00–13:35，每 15 分鐘（時區選 **Asia/Taipei**） |
| Request method | **POST** |
| Request body | `{"ref":"main"}` |

3. **Headers** 分頁加三行（第二個權杖建議另建一組，方便單獨撤銷）：

```
Authorization: Bearer github_pat_你的權杖
Accept: application/vnd.github+json
Content-Type: application/json
```

4. 存檔後按 **TEST RUN** → 回應應該是 **204 No Content**（GitHub 成功但無回傳內容，這是正常的）
5. 複製同一個 job，改成美股時段：週一–週五 **21:30–04:05**，每 30 分鐘

> **安全**：這組權杖權限只有 `investment-hub` 的 Actions，最壞情況是有人幫你觸發抓價，碰不到你其他 repo。
> 建議與手機那組分開，哪天要撤銷互不影響。

---

## 步驟 3：驗收（隔天早上花 30 秒）

打開儀表板，看標題下方那行「數據時間：… （X 分鐘前）」：

| 顏色 | 意思 | 該做什麼 |
|---|---|---|
| 灰色（<20 分） | 正常 | 什麼都不用做 |
| 橘色（20–45 分） | 有點舊 | 按 ⟳ 更新 |
| 紅色（>45 分） | 排程沒跑 | 檢查 cron-job.org 有沒有失敗紀錄 |
| 紅色 +「⚠️ 走備援讀取」 | GitHub API 讀不到（額度或離線） | 通常是沒帶權杖，重設權杖即可 |

盤中時段若持續紅色，到 cron-job.org 看該 job 的執行紀錄：
- **204** = 正常
- **401 / 403** = 權杖過期或權限不足 → 重做步驟 1，記得勾 Actions: Read and write
- **404** = 網址打錯，或權杖沒授權到這個 repo

---

## 已經修好的部分（2026-07-27，不需要你動手）

| 問題 | 原因 | 修法 |
|---|---|---|
| 下拉沒反應 | iOS Safari 原生下拉重載搶走手勢，自訂邏輯還沒跑就被殺 | CSS 加 `overscroll-behavior-y: contain`；另加常駐 **⟳ 更新** 鈕，不再靠手勢 |
| 推了新價卻看到舊的 | 開頁讀 GitHub Pages 上的 data.json，Pages 每次 push 要重新部署，中間服務舊版 | 開頁改走 GitHub API 直讀，繞開 Pages 延遲 |
| 不知道有沒有真的更新 | 只顯示 generatedAt，看不出多舊 | 加「X 分鐘前」與顏色警示 |
| 訊息會騙人 | 沒權杖時顯示「已是最新存檔」，聽起來像成功 | 改成「⚠️ 未設權杖，只能讀存檔 → 點🔑設定才能即時抓價」 |
| 手動更新被排程砍掉 | workflow concurrency `cancel-in-progress: true` | 改成依觸發來源分組、不互相取消 |
| 抓價太慢 | 每次重裝 yfinance 等套件 | 加 pip 快取 + requirements.txt |
| API 額度爆掉 | 未帶權杖時 GitHub API 每小時只有 60 次 | 有權杖就帶上（額度 5000/hr），前景輪詢放寬到 5 分鐘 |

## 這件事的誠實邊界

即使全部設定完成，**這套架構做不到「秒級即時報價」**——資料要經過 GitHub Action 抓取、commit、再被前端讀回，
一輪 60–90 秒。它能達成的是：**你打開 App 或按 ⟳，一分半鐘內看到真實現價；盤中每 15 分鐘自動更新一次。**
對波段與部位管理夠用；要看跳動的即時報價，還是用券商 App。
