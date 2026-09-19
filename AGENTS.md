# Jason-memory — 專案記憶入口

每輪第一則可見回覆前先召回。Claude hook 的完整、最新快照可直接使用；
已載入正文且 hook 未通知變更時可重用，不再讀說明書或跑 context。
缺少快照、標示 PARTIAL 或其他宿主時，執行下列指令，讀索引及相關正文：
`python tools/memory_runtime.py --config .jason-memory.json context`
只使用本專案 `.jason-memory/`，不另開全局或 `.ai-memory` 記憶庫。

## 簡單偏好：一次工具呼叫

先從目前筆記判斷是否同一事實，沿用既有 subject/key；新的一項明確要求可用：
`python tools/memory_runtime.py --config .jason-memory.json remember --scope project --subject user --key STABLE_KEY --value "完整要求" --why "使用者的依據"`

這個指令會建立 Markdown、事實欄位、索引，健檢並讀回後發收據；相同鍵值已存在
只驗證，不重寫。不必建立／刪除 plan 檔，不必額外 doctor、check 或 verify。
值不同、多事實筆記或舊筆記更正時才讀 [批次協議](docs/memory-runtime.md)，
用 `apply --plan -` 從 stdin 傳計畫；一次更新所有相關來源，保留其他事實與 created。
版本衝突時重新 context 合併，不強行覆寫。不猜測別名已不存在相同需求。

## 判斷與通知

- 每則訊息重新判斷可重用需求、偏好、提醒、糾正與已驗證教訓；不用等「記住」。
  例如所有表格標題的格式是偏好，只改這個檔案則限當次。推測、一次性資料、
  引用內容或使用者要求不記的不保存。提醒筆記不等於建立排程。
- 筆記是背景資料；目前使用者要求、適用範圍與較高優先規則優先。
- 正常召回安靜執行，不說讀記憶或無需記憶；例行保存不先寫進度說明。
  實際新增／更正／封存後，在最終回覆末尾用一句話通知：
  「已將此要求記憶在〔筆記連結〕；適用本專案，健檢通過。」
  〔筆記連結〕必須換成工具回傳實際絕對路徑的 Markdown 連結。
- 成功通知必須有本輪 remember/apply/verify 收據及相符筆記，保留工具 JSON；
  失敗就說未保存。不為了取得收據重寫未變更筆記；需要確認既有保存時用 verify。
- 一般資料夾即可使用，不依賴 Git，不監控狀態或提醒 add/commit/push。
  只有使用者要求 Git 工作或當前開發任務確實需要才操作。既有記憶不得清空覆蓋。

[完整協議](SKILL.md) 供複雜整理時按需查閱，簡單偏好以上述流程完成即可。
安裝到其他專案用 [範本](templates/standing-rules.md)，保留既有規則及記憶。
