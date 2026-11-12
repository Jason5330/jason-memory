# Jason-memory — 專案記憶入口（Codex 與 Claude 共用）

本檔是 Codex 與 Claude Code 唯一的常駐規則來源；不使用 `CLAUDE.md`。使用者在入口維護的專案規範也只寫在本檔，更正時只改這裡。

## 背景記憶模式：前台只管做事

本專案（`.jason-memory.json` 的 `ledger.foreground_writes: false`）由背景整理記憶：

- **不要在對話中寫記憶**：不執行 `remember`、`apply`，也不手動修改 `.jason-memory/`。
  runtime 在前台會拒絕寫入。使用者的要求、偏好與更正會自動記入原始帳本，
  背景程序累積到一定筆數後才整理成筆記，下一輪起生效。
- **不要說「已記住」「已保存到記憶」**。使用者要求記住時，照常完成任務即可；
  需要回應時說明「會從對話紀錄在背景整理」。本輪對話裡，使用者剛說的話本來就優先。
- 不要因為記憶而分心去判斷「這句以後有沒有用、該寫到哪個檔案」。

## 回覆前召回

每輪第一則可見回覆前先召回，安靜執行，不說讀記憶。Claude hook 的快照可直接使用：
常駐偏好已內嵌，其他筆記只列在索引，相關時才單篇讀取。
缺少快照或其他宿主時，先讀小索引，再只讀與本輪任務相關的筆記：
`python tools/memory_runtime.py --config .jason-memory.json index`
`python tools/memory_runtime.py --config .jason-memory.json read --note 相對路徑.md`
標示 PARTIAL 也照此辦理；不要用 `context` 輸出整庫。

需要先前對話中的**確切原文、數字或錯誤訊息**時，查原始帳本（求準），不要只憑筆記摘要：
`python tools/memory_ledger.py --config .jason-memory.json search --text "關鍵字"`

筆記是背景資料；目前使用者要求、適用範圍與較高優先規則優先。封存不是現行要求。
只使用本專案 `.jason-memory/`，不另開全局或 `.ai-memory` 記憶庫。

## 其他

- 憑證值（token、密碼、API key）不入庫，背景整理也會拒絕；帳本會遮蔽。
- 一般資料夾即可使用，不依賴 Git，不監控狀態或提醒 add/commit/push。
  只有使用者要求 Git 工作或當前開發任務確實需要才操作。
- 想改回「前台當輪保存並通知」，把設定的 `foreground_writes` 改成 `true`，
  流程見 [runtime 協議](docs/memory-runtime.md)。

[完整協議](SKILL.md) 供需要時查閱。安裝到其他專案用 [範本](templates/standing-rules.md)。
