# 解壓後，直接開始（專案版）

這是**專案版**：記憶只對這一個資料夾有效。想在電腦上任何資料夾都有記憶，請改用
[全局版](global/README.md)（`jason-memory-global.zip`）。

1. 將 ZIP 解壓到自己的工作資料夾，例如桌面上的 `我的AI工作區`。
2. 雙擊 `INSTALL.cmd`，Claude Code 與 Codex 一起設定好：合併本資料夾的 Claude hooks，
   並在 Codex 的 `~/.codex/config.toml` 加入記錄對話用的 `notify`。結果裡的 `warnings`
   若提到 Codex 未接通，表示你原本就有別的 `notify` 程式，Codex 對話不會被記錄。
   不清空、不重建已有記憶；想先預覽可在命令列執行 `INSTALL.cmd --dry-run`。
3. 用 Claude Code 或 Codex 開啟解壓後的 `jason-memory` 資料夾（Codex 用本機資料夾，
   不選 Git worktree），建立**新對話**。Claude 可用 `/hooks` 確認已載入。
4. 直接說明你的工作，例如：「幫我整理這些資料；以後回報都先列結論。」

## 之後會怎樣

- 預設是**背景記憶模式**：AI 當輪不寫記憶，也不會說「已記住」。同一個對話裡，你剛說的話本來就會照做。
- 對話自動記入原始帳本；累積約 20 筆紀錄（一來一回算 2 筆）後，背景會呼叫本機已登入的
  Claude Code 或 Codex CLI 整理成筆記，之後的新對話就會記得。
- 對話沒累積到門檻就停下來，不會整理；想馬上整理或查看進度，見 README 的
  [使用方法與技巧](README.md#使用方法與技巧)。
- 說錯了直接更正：「標題改用淡紫色，以這個為準」。不想被記住的就講明：「這次只回一句話」「這個不要記」。

Claude Code 與 Codex 共用根目錄的 `AGENTS.md`，不需要 `CLAUDE.md`。
正常召回不輸出記憶流程說明。

不需要安裝 Git、執行 clone、建立分支、提交、推送或建立 PR。
健檢使用 Python 3.9+，不需要額外 Python 套件。一般使用版已附空白 `.jason-memory/MEMORY.md`；
若下載的是原始碼 ZIP，第一次使用由 AI 從範本建立索引。

## 若看到 master 或 Create PR

這是 AI 工具的版本控制介面。請確認開啟的是新解壓的一般資料夾，而不是
原本 clone 的儲存庫，也不要把使用資料夾放進其他儲存庫裡。檔案 `.gitignore`
只是排除清單，不會將資料夾變成儲存庫；`.git` 資料夾或檔案才是相關中繼資料。

框架規則只能約束 AI 的操作與回覆，不能強制隱藏宿主 App 的按鈕。

## 個人記憶與更新

記憶保存在工作資料夾的 `.jason-memory/`（含原始帳本 `.raw/`）。它是明文檔案，分享給別人前須
移除；不要將自己整個使用資料夾重新壓縮當作公開版。

更新時先備份自己的 `.jason-memory/`，將新版解壓到另外的資料夾，再將自己的
記憶庫完整複製過去，取代新版附的空白記憶庫。不要反過來用新版空白索引
覆蓋舊記憶。之後在新版資料夾重新執行 `INSTALL.cmd`，開新對話。

只看到 `.hook-state`、`.receipts` 或 `.writer.lock` 不代表偏好已記錄，須有實際筆記；
用 README 中的 `index`、`pending` 指令可確認記了什麼、整理到哪。

全部移除：雙擊 `UNINSTALL.bat`（可先加 `--dry-run` 預覽），詳見 [清除說明](docs/uninstall.md)。

詳細說明見 [README.md](README.md)；帳本、背景整理與 hook 細節見 [runtime 協議](docs/memory-runtime.md)。
