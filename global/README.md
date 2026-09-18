# Jason-memory 全局共用版

讓**同一位使用者、同一台電腦上的不同本機 AI 工具**共用記憶。
全局偏好供所有專案使用，個別專案的需求另外保存，避免互相套錯規則。
記憶仍是可閱讀的 Markdown 筆記、四種類型及索引，不需要 Git、GitHub、
伺服器或額外的雲端同步帳號。

這是另一個獨立版本，不會取代原本的專案版 ZIP，也不會自動搬動既有記憶。

## 安裝一次，之後在各專案使用

需要 Python 3.9+，以及能讀寫本機檔案、執行 Python、載入使用者層級指令的 AI 工具。
網頁聊天室若沒有本機檔案權限，不能只靠這個 ZIP 共用記憶。

1. 解壓 `jason-memory-global.zip`，打開內層 `jason-memory-global` 資料夾。
2. Windows 執行 `INSTALL.cmd`，預設安裝 Codex 與 Claude Code 的記憶入口。
3. 在各 AI 工具開啟真正要工作的專案，重新啟動工作階段／建立新對話，讓使用者層級規則載入。
4. 正常說明需求；例如「所有專案回報都先列結論」可保存為全局偏好。
   AI 保存成功後應同輪通報內容、原因、適用範圍及筆記連結。

**全局版需要這一次安裝。** 它與解壓即用的專案版不同：
安裝器將執行檔複製到固定位置，再把入口加入所選 AI 的使用者規則。
安裝後可以移動或刪除下載的解壓資料夾，既有入口仍指向固定安裝位置。
使用者層級規則是否載入仍取決於 AI 工具；不保證所有 AI 都會自動遵守。

非 Windows，或希望指定選項，可在解壓後的資料夾執行：

```text
python global/install.py --dry-run
python global/install.py --agent both
python global/install.py --agent codex
python global/install.py --agent claude
```

依環境將 `python` 換成 `python3` 或 `py -3`。`--dry-run` 只列出計畫，完全不建立或修改檔案。

## 共用位置與專案隔離

預設使用使用者家目錄下的 `.jason-memory-global`：

```text
.jason-memory-global/
  framework/               固定的協議、寫入工具與健檢工具
  GENERIC_INSTRUCTIONS.md   其他 AI 的使用者層級入口
  memory/
    shared/                所有已接入 AI 共用的全局筆記及索引
    projects/<識別碼>/       依專案根目錄隔離的筆記及索引
```

安裝時即建立缺少的 `memory/shared/MEMORY.md`；既有索引及筆記不會被重設。
Claude Code 的管理入口會直接匯入這份共用索引；路徑由安裝器依實際位置計算，
支援同事的使用者目錄、中文及空白，不寫死任何人的名稱。
個別專案索引仍在首次 `context` 時建立；不把所有專案索引匯入全局入口。
每輪第一則可見回覆前，AI 先執行 `context` 刷新全局與目前專案索引，再讀取
適用的開場、格式及任務筆記；匯入的啟動快照不取代跨 AI 更新檢查。
簡單問答也適用。正常召回不說「我來讀取記憶」「已讀取記憶」「本次無需記憶」；
實際保存、修改、封存才通報，讀寫失敗才說明問題。
同一專案在不同 AI 必須使用相同的實體專案根目錄；不同專案使用不同索引。
專案根目錄搬動或更名後會得到新的識別碼，需另行搬移對應專案記憶；全局偏好不受影響。
全局需求的專案例外只寫入該專案，當次要求不自動變成永久規則。

需要自訂位置時，例如：

```text
python global/install.py --home "D:/JasonMemory" --agent both
```

各 AI 必須使用**同一個 `--home`**。改用另一個 home 不會自動搬移舊記憶。
這是同機檔案共用，沒有跨電腦同步。記憶為明文，請選擇自己有權存取的本機資料夾；
不要保存密碼、token、cookie 等機密值。

## Codex、Claude Code 與其他 AI

| 選項 | 安裝位置／做法 |
|---|---|
| `--agent codex` | 預設 `~/.codex/AGENTS.md`；存在非空的 `AGENTS.override.md` 時使用該檔案 |
| `--agent claude` | 預設 `~/.claude/CLAUDE.md`，並合併 `~/.claude/settings.json` 的 Jason hooks |
| `--agent both` | 安裝上述兩者，指向同一份記憶 |
| `--agent generic` | 只安裝框架並產生 `GENERIC_INSTRUCTIONS.md`，不修改 AI 工具設定 |

Codex 會尊重 `CODEX_HOME`，Claude Code 會尊重 `CLAUDE_CONFIG_DIR`。
入口選用依據：[Codex 的 AGENTS.md 載入規則](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、
[Claude Code 的使用者記憶規則](https://code.claude.com/docs/en/memory)。
也可使用 `--codex-home`、`--claude-home` 指定設定目錄。
`--user-home` 是隔離測試或明確指定使用者目錄的選項；提供後會忽略上述環境變數，
避免測試誤寫真實設定。明確給定的 `--codex-home`、`--claude-home` 仍優先。

其他本機 AI 先執行 `--agent generic`，再將安裝位置中的
`GENERIC_INSTRUCTIONS.md` 內容合併到**該工具官方支援的使用者／全局規則**。
不要覆蓋原有規則。安裝器不猜測未知工具的設定位置；若工具不支援全局規則，
需要依該工具的方式在各工作區接入，不能宣稱已全局啟用。

**既有專案規則需要人工整合。** 舊專案的 `AGENTS.md`、`CLAUDE.md` 或其他常駐規則，
可能仍指定舊記憶庫或有更高優先的限制。安裝器不會修改這些專案檔案，
全局入口也不能越過它們。請先備份，再合併記憶入口、處理衝突；
原有記憶不會自動遷移或刪除。

## 驗證是否接通

在 AI A 的專案 A 說「所有專案都先用一句話回報結論」，確認它通報已保存至全局。
再用 AI B 開啟不同的專案 B／新對話，請它讀取共用記憶並說明適用的回報偏好。
接著在專案 A 指定一個**只限該專案**的例外，確認 B 不會收到這個例外。
測試時不要使用私人機密資料。

若 AI 沒讀取入口、無法執行 Python，或要求檔案存取權限，先按該工具的支援方式處理；
沒有成功保存或召回時，不能只憑回答語氣認定已接通。兩個 AI 的安全限制與授權不會互相繞過。
所有筆記變更經由共用寫入工具序列化並健檢；直接手動改檔不受其並行保護。

## 更新與卸載

重新執行安裝會更新框架及同一段入口，不重複新增、不覆寫記憶。
升級此版請重新執行 `INSTALL.cmd`，再重新啟動 Claude Code 工作階段。
管理區塊會移到規則檔最前面，其他內容保留；卸載可移除該區塊與其分隔符。
每次實際修改既有 AI 設定前，會在旁邊建立 `.jason-memory-<識別碼>.bak` 備份，
原有非管理區塊內容會保留。遇到損壞或重複管理區塊時會停止，請先檢查及備份再修正。

```text
python global/install.py --agent both --uninstall
```

若曾指定自訂 `--home` 或 AI 設定目錄，卸載時也帶上相同選項。
卸載只移除所選 AI 設定中的管理區塊，**保留全部記憶、框架及備份**。
其他 AI 手動貼上的通用入口需自行移除；`GENERIC_INSTRUCTIONS.md` 也會保留。

本 ZIP 僅含白名單框架檔案、空白索引範本及授權；不含私人筆記、帳號設定、
憑證、Git 歷史或開發機器上的全局規則。

[本版測試方法與結果](https://github.com/Jason5330/jason-memory/blob/master/tests/global-validation.md)
區分安裝器測試、真實 AI 共用測試與尚未涵蓋的宿主條件。

[啟動匯入修補的測試與剩餘限制](https://github.com/Jason5330/jason-memory/blob/master/tests/recall-validation.md)。
啟動匯入不保證模型每次完整遵守；本版提供 Claude hooks，仍不保證模型完全遵守。

新版安裝會合併 Claude settings.json 的 Jason hooks，保留其他事件與既有記憶。
跨筆記更正、版本交易及保存通知詳見 [runtime 協議](docs/memory-runtime.md)。
