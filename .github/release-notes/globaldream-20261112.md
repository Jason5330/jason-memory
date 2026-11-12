# 全局版改為背景記憶模式，一次安裝、任何資料夾可用

全局版（`jason-memory-global.zip`）預設改為與專案版相同的背景記憶模式。

**一次安裝**：解壓後雙擊 `INSTALL.cmd`，同時設定
- Claude Code：全局 hooks（`~/.claude/settings.json`），每輪注入記憶並記錄對話。
- Codex：`~/.codex/AGENTS.md` 規則，以及 `config.toml` 的 `notify`（記錄對話）；已有其他 `notify` 不覆蓋。

之後在桌面任何新資料夾用 Claude Code 或 Codex 開啟即可使用，不需每個資料夾各裝一次。

**行為**
- 對話中不寫記憶、不說「已記住」；前台寫入會被拒絕。
- 對話記入各資料夾自己的原始帳本；累積約 20 筆後背景整理。沒有限定專案的一般偏好（回報格式、語言、時間格式等）存到全局共用，所有資料夾套用；專案專屬需求只存在該專案。
- 實測：`claude -p` 背景整理把「以後回報先列結論」存到全局、「本專案報表輸出 xlsx」存到專案、「這次只回一句話」略過。
- 想用舊的「當輪保存並通知」：`python global/install.py --mode foreground`。

**一鍵全部刪除**：全局版 ZIP 內的 `UNINSTALL.bat` 永久刪除框架、全局與所有專案記憶（含原始帳本），並移除 Claude hooks、Codex 規則與 `notify` 設定；可先 `UNINSTALL.bat --dry-run` 預覽。已用端到端測試驗證。

**升級**：重新執行新版 `INSTALL.cmd` 即可，既有筆記保留；開新對話後生效。

附 jason-memory.zip（專案版）、jason-memory-global.zip（全局版）與 SHA256SUMS.txt。
