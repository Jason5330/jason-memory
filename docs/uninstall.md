# 一鍵永久清除

先關閉正在使用記憶的 AI 工作階段，再雙擊套件根目錄的 **UNINSTALL.bat**。
不需 Git、網路或系統管理員；需要 Python 3.9+。不要只搬走 BAT，請保留套件中的
`tools/purge_memory.py`、`tools/hook_settings.py` 與 `uninstall-manifest.json`。

**雙擊就會永久刪除，沒有資源回收筒或自動備份。** 不想刪除時不要執行。
只想查看刪除清單，從命令列執行 `UNINSTALL.bat --dry-run`，不會改動檔案。

預設一次處理：

- BAT 所在資料夾：依 `.jason-memory.json` 清除本專案記憶庫，包括索引、筆記、
  封存、收據與 hook 狀態；移除 Jason-memory hooks 與可辨識的框架檔案。
- 目前 Windows 使用者：清除 `.jason-memory-global` 內的框架、全局記憶，
  **以及該全局庫保存的所有專案記憶**。也會辨識全局入口記載的自訂安裝路徑。
  自訂安裝目錄原有的其他檔案會保留，只刪除框架已安裝的檔案與專用記憶庫。
- Codex、Claude Code：移除 Jason-memory 管理區塊、hooks 及可辨識的安裝備份；
  保留其他規則、設定、hooks、登入資訊。支援 `CODEX_HOME`、`CLAUDE_CONFIG_DIR`。

不會掃描其他硬碟或專案資料夾。不處理其他副本、舊 ZIP、AI 聊天紀錄、Claude
原生記憶庫、雲端資料或 Git 歷史。另一份獨立專案版的記憶，要在那份資料夾另外清除。
腳本不硬編碼使用者名稱，同事解壓後可用相同步驟執行。

移除框架檔案前會比對發行清單與內容雜湊。自行修改過的檔案、無標記的混合規則、
無法確認來源的舊備份會保留，並列在 `remaining`；此時回傳碼為 2，**不宣稱完整清除**。
損壞設定、越界路徑或符號連結／junction 會在預檢時阻擋；執行途中遇到權限或
並行寫入錯誤，會停止並回報錯誤，可能已清除部分檔案，請處理後重跑。

清除工具本身（BAT、兩支 Python、此說明、manifest）保留，方便查看結果或重跑。
空資料夾、一般工作檔案都保留。確認完成後，可以自行刪除這些清除工具。
既有 AI 對話仍可能保有已載入的內容；重新開啟 AI 並使用新對話才會卸載。

自訂目標範例（有空白的路徑請加雙引號）：

```bat
UNINSTALL.bat --dry-run --home "D:\SharedMemory" --project "D:\MyProject"
UNINSTALL.bat --home "D:\SharedMemory" --project "D:\MyProject"
```

`--project` 指定的資料夾須包含對應套件 manifest 才能清除框架原始檔。
自訂全局目錄須有框架的安裝識別設定，不能用此工具刪除任意資料夾。
`--user-home` 可指定隔離使用者目錄並忽略宿主環境變數，供測試使用；
`--codex-home`、`--claude-home` 可另外指定 AI 設定目錄。

這與舊版 `global/install.py --uninstall` 不同：舊指令只移除入口並保留記憶。
