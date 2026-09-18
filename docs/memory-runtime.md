# 讀寫一致、跨筆記更正與 Claude hooks

記憶仍是 Markdown 加索引，四種類型不變。專案版仍保存在
`.jason-memory/MEMORY.md` 及同目錄內筆記；全局版仍是使用者家目錄下
`.jason-memory-global/memory/shared` 及 `memory/projects/<專案雜湊>`。
不需 Git，不清空舊記憶，也不把記憶搬到 Claude 原生 auto-memory。

## 唯一路徑與每輪召回

專案版以根目錄 `.jason-memory.json` 指定 `memory_root`；全局版安裝時產生
`framework/memory-config.json`。讀取、寫入、hook 都使用同一份設定，拒絕
路徑逃逸和重新導向。不同 AI 必須使用同一設定及相同專案根目錄。
同時安裝兩版時，明確的專案 `.jason-memory.json` 優先，全局 hook 在該專案
略過，不同時注入另一套記憶。既有 `.ai-memory` 不會自動猜測、搬移或刪除。

每則訊息先取得最新 `context`。Claude hook 已完整提供且未變更的正文可重用；
若標示 PARTIAL SNAPSHOT，仍須讀取遺漏的相關筆記才回覆。正常讀取不通報。
hook 注入內容不是對話文字，但仍占上下文 token；超過 8,000 字元明確標示省略。

```text
python tools/memory_runtime.py --config .jason-memory.json context
python tools/memory_runtime.py --config .jason-memory.json audit
```

全局版使用安裝入口的 runtime/config 絕對路徑，另帶 `--project 專案根目錄`。
context 回傳各 scope 的 root、revision、索引、筆記正文和事實檢查結果。

## 寫入與同一事實的更正

1. 搜尋所有有效筆記，不只檔名或索引。用相同 subject/predicate 辨識同一事實。
2. 保留既有檔名、created 與其他仍有效事實；將所有相關舊值一併更正。
   例如稱呼筆記也含職業時，同步改職業並保留稱呼。不可只新增第三份筆記。
3. 新增或修改 user／feedback 時必須有非空事實區塊；新增或修改可重用事實時，在正文加 `jason-facts` JSON 區塊，並同步正文與
   description。subject 使用穩定識別，不因職業、稱呼或偏好值改變而更名。
   相同 scope 的一個 subject/predicate 表示一個現行值；有條件的規範將條件
   寫入穩定 predicate（例如 `report.header.color:monthly-report`），不要混為矛盾。
4. 在記憶庫外準備 JSON 計畫，以 context 的整庫 revision 套用。工具自動更新
   索引、跑兩支結構健檢及事實檢查；衝突或版本過時即拒絕，不繞過工具硬寫。

筆記中的事實區塊示例（示例不是使用者的真實身份）：

````markdown
```jason-facts
[{"subject":"user:anna","predicate":"occupation","value":"醫生"},
 {"subject":"user:anna","predicate":"preferred-name","value":"ANNA"}]
```
````

計畫格式如下；updates 的值是完整 Markdown，包括原有必要 frontmatter。
用程式 JSON 序列化，不要手動拼接跳脫字元。

```json
{
  "expected_revision": "context 回傳的該 scope revision",
  "updates": {"user/job.md": "完整 Markdown 字串", "user/name.md": "完整 Markdown 字串"},
  "retire": []
}
```

```text
python tools/memory_runtime.py --config .jason-memory.json apply --scope project --plan plan.json
```

全局通用要求才用 `--scope global`；專案例外放 project，不改全局原值。
失敗後重新 context 並依新正文合併，不只替換 revision 重送舊計畫。
退役將相對筆記路徑加入 retire，工具保留 archive 歷史，不等於永久清除。
plan 可能含私人內容，完成後清除自己建立的暫存檔，不發布或提交到儲存庫。

### 成功宣告需要寫入／驗證證據

`apply` 在交易後重新讀回筆記、確認內容等於計畫、驗證索引，才回傳 `receipt`
與 `receipt_root`。保留完整工具 JSON 輸出，不能用自己拼的「成功」文字代替。
收據保存在該記憶庫 `.receipts/`，是驗證中繼資料，不是新的使用者記憶。

若要求已存在，不必重寫；需要向使用者確認「已保存」時，先讀回驗證：

```text
python tools/memory_runtime.py --config .jason-memory.json verify --scope project --note feedback/report-order.md
```

全局版同樣帶入口提供的 config、project 與 scope。可重複 `--note` 驗證多份筆記。
`verify` 回傳正文與收據，不變更筆記或索引。比對正文確實符合使用者要求後才確認。
一般讀取不必 verify，也不新增讀取通知。

Claude 的 PostToolUse 只接收宿主工具輸出的收據；Stop 再核對本輪收據、實際檔案
內容、索引及回覆連結。舊對話的收據、模型直接編造的 JSON、不存在的筆記，
以及「空白索引健檢通過」都不構成某個偏好已保存的證據。
同輪多次保存可以合併通知；其他筆記使索引更新時，會重新驗證索引，不強迫重寫。

每次**新增、更新、封存或刪除**成功，必須在同輪最終回覆末尾通報內容、
範圍與可點選的實際筆記路徑，例如：
「已將『每次先打招呼』記憶在 [問候偏好](實際絕對路徑)；適用本專案，健檢通過。」
可合併多筆，但不可只說「了解」。失敗說明未保存；讀取、無變更不發空白通知。
若只是索引修復，連結索引；退役連結封存位置，說明已從有效記憶移除。

## hooks 的實際作用

| 事件 | 行為 |
|---|---|
| SessionStart（包括 compact） | 從設定路徑載入最新筆記正文，供第一句回覆前使用 |
| UserPromptSubmit | 每輪檢查版本；另一個 AI 改動後重新注入最新有效記憶 |
| PreToolUse | 拒絕可辨識的平行記憶路徑、Write/Edit 直接改庫，要求經 runtime 寫入 |
| PostToolUse | 收集成功工具回傳且符合磁碟內容的本輪收據；回饋可辨識的事實衝突 |
| Stop | 檢查有變更卻沒通知，以及無證據卻宣稱已保存；要求補處理一次，仍失敗則停止並顯示驗證失敗警告 |

專案 ZIP 附 `.claude/settings.json`，需要 shell 能找到 Python 3.9+。
建議第一次執行 `INSTALL.cmd`，固定目前 Python 執行檔路徑；不是重建記憶。
升級已有專案時**不要覆蓋它的 settings.json**；更新工具與入口後執行
`python tools/install_hooks.py` 合併，只增刪 Jason 自己的事件，保留其他設定並備份。
全局版 `INSTALL.cmd` 會同樣合併 Claude 使用者 settings，路徑來自目前登入帳號。
移動專案／Python 後重新執行安裝。`--dry-run` 只預覽；`--uninstall` 只移除事件
（全局版另移除受管理入口），不刪筆記、歷史或框架。安裝後開新 Claude 對話，
依宿主正常流程信任專案並在 `/hooks` 確認載入，不繞過信任或權限限制。

Codex 及其他 AI 沿常駐規則呼叫同一 runtime；這套 Claude 事件不會自動變成
其他宿主的 hook。尚未接入的工具不保證自動召回。

## 保證與限制

- 使用 runtime 的同庫操作經 OS 檔案鎖、整庫樂觀版本及可重播交易保護。
  中斷後下一次 runtime 操作先恢復，再讀取；磁碟多檔案替換本身不是一次原子操作。
  手動編輯、直接 shell 寫入、舊工具讀取不享有一致快照保證。共享網路磁碟不在驗證範圍。
- 每次改動保留舊正文於 archive，索引由新 description 產生。歷史不是現行要求。
  備份不等於加密；不保存密碼等機密值，不自動雲端同步。
- 事實識別欄位能確定性檢出同鍵不同值；另有少量舊中文職業敘述辨識。
  `semantic_review_needed` 表示無結構事實的舊筆記仍需人工／AI 核對，
  不代表必須清空，也不宣稱任何自由文字矛盾皆可偵測。結構欄位仍須與正文一致。
- hooks 降低漏載入與漏通報，不能保證模型理解或遵守所有語意。Stop 無法收回
  已顯示文字；只補救一次，仍失敗用宿主 `systemMessage`／`stopReason` 顯示警告，
  不把第二次失敗當成驗證通過。宿主必須支援且啟用這些事件與輸出。
  成功宣告辨識涵蓋常見繁簡中文／英文，略過引用、範例及否定句；不是任意語言
  的完整語意判斷器。收據證明檔案與索引、工具觀測一致，不保證內容準確代表
  使用者意圖；模型仍須比對正文。其他 AI 改掉相關內容時需重新驗證。
- `.receipts/` 只含驗證資訊，隨操作增加；與 `.hook-state`、`.writer.lock` 一樣，
  不能當成使用者筆記。這不是防惡意程式偽造檔案的安全機制，也不會替沒有本機
  工具權限的模型繞過授權。非 Claude 宿主須由其入口執行相同驗證或另接事件。
- PreToolUse 是工作流程護欄，不能解析所有 shell、Python 或第三方工具的副作用，
  不是安全沙盒。停用 hooks、宿主超時或未信任設定時不會強制執行。
- 沒有背景監聽、額外模型 API 或 Git 指令；是否值得記憶仍由 AI 判斷。

事件格式依 [Claude Code 官方 hooks 文件](https://code.claude.com/docs/en/hooks)。
