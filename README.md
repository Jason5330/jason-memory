# Jason-memory

讓 Claude Code 與 Codex 記得你的偏好、專案限制與未完成的工作。記憶是一般的 Markdown 檔案，
不需要資料庫、伺服器或 Git。

- **前台只管做事**：對話中 AI 不寫記憶、不說「已記住」，也不必分心判斷哪句話以後有用。
- **背景整理**：每輪對話自動記進防竄改的原始帳本，累積約 20 筆後，由本機已登入的 Claude Code 或
  Codex CLI 在背景整理成附來源的筆記。
- **新對話自動召回**：只帶一份小索引和常駐偏好，其他筆記需要時才讀；要查原話、數字或錯誤訊息時回查帳本。

## 下載與安裝

兩個版本都是**解壓後雙擊一次 `INSTALL.cmd`**，同時設定 Claude Code 與 Codex。

| 版本 | 適合 | 下載 |
|---|---|---|
| **全局版** | 在電腦上**任何資料夾**都有記憶；一般偏好全局共用，各專案需求分開存 | [jason-memory-global.zip](https://github.com/Jason5330/jason-memory/releases/latest/download/jason-memory-global.zip)・[全局版說明](global/README.md) |
| **專案版** | 只在**這一個資料夾**工作，記憶跟著資料夾走 | [jason-memory.zip](https://github.com/Jason5330/jason-memory/releases/latest/download/jason-memory.zip)・[開始使用](START_HERE.md) |

- 需要 **Python 3.9+**；背景整理需要本機已安裝並登入 **Claude Code 或 Codex CLI**。
- 專案版請解壓到獨立的一般資料夾（例如桌面），不要放進其他 Git 儲存庫，也不需要 `git init`。
- 安裝結果若有 `warnings`（例如你原本就有別的 Codex `notify` 程式），代表 Codex 對話不會被記錄，請先處理。
- 兩個版本可以並存：資料夾裡有專案版設定（`.jason-memory.json`）時以專案版為準，兩者不會互相搬移記憶。
- 想手動接入一個已存在的專案，見 [進階與維護](docs/advanced.md#手動接入既有專案)。

裝好後，用 Claude Code 或 Codex **開新對話**，直接交代工作即可。

## 運作方式

```text
你說的話 ─▶ 原始帳本（.raw/ledger.jsonl） ─▶ 背景整理 ─▶ 筆記與索引 ─▶ 下一則訊息起召回
            每輪自動追加、憑證遮蔽          約每 20 筆一次     附來源編號
```

1. **召回**：Claude 由 hooks 在啟動與每輪注入索引和常駐偏好；Codex 依 `AGENTS.md` 讀索引，需要時再讀單篇筆記。
2. **做事**：AI 只處理你的任務；你這一輪說的話本來就優先，不需要先存成記憶。
3. **記錄**：你的訊息、AI 的回覆與 Shell 工具結果自動追加到原始帳本（Claude 靠 hooks，Codex 靠 `notify`），
   密碼、token 等憑證值會先遮蔽。
4. **背景整理**：未整理的紀錄達到門檻時，背景程序把這批紀錄交給 Agent CLI 整理成筆記。成功才前進，
   失敗下次重試，紀錄不會遺失。
5. **生效**：整理完成後，從下一則訊息起召回就會帶入。

記憶放在哪裡：

```text
專案版：你的資料夾/.jason-memory/          全局版：~/.jason-memory-global/memory/
  ├── MEMORY.md         索引                  ├── shared/            所有資料夾共用的偏好
  ├── feedback/ user/   筆記                  └── projects/<雜湊>/    各資料夾自己的需求與帳本
  │   project/ reference/
  ├── archive/          被更正或封存的舊筆記
  └── .raw/             原始帳本與整理狀態
```

| 筆記類型 | 內容 |
|---|---|
| `user` | 你的身份、職責與已確認的個人偏好 |
| `feedback` | 長期的工作要求、叮嚀、踩坑教訓與驗證有效的做法 |
| `project` | 專案目標、決策、限制、卡點與下一步 |
| `reference` | 之後會用到的網址、票單、儀表板或檔案位置 |

筆記範例（正文預設繁體中文）：

```markdown
---
name: preference-d92a3014d20a96bf
description: "回報時一律先列結論，再說明原因"
type: feedback
created: 2026-11-12
updated: 2026-11-12
---

回報時一律先列結論，再說明原因

Why: 使用者要求以後回報都這樣做；來源：ledger local:1:ab12cd34ef56

How to apply: 全局共用；回報時一律先列結論，再說明原因
```

`來源：ledger …` 是原始紀錄的編號，可以用 `source --ref` 查回原話並驗證沒被改過（見[查看與手動操作](#查看與手動操作)）。

## 怎麼說會被記住

不用說「記住」，照平常講就好。下面的說法讓背景整理判斷得更準：

| 想記的內容 | 例句 | 存到 |
|---|---|---|
| 所有地方都適用的偏好 | 「**以後**回報都先列結論，再說原因」 | 全局共用（全局版）／本專案（專案版） |
| 你的身份、職責 | 「我是物流主管，負責越南倉庫的進出貨」 | 同上，類型 `user` |
| 只限這個專案的規定 | 「**這個專案**的報表一律輸出成 xlsx」 | 只存這個專案 |
| 專案的限制 | 「本專案必須離線，不能呼叫外部服務」 | 只存這個專案，類型 `project` |
| 更正舊的說法 | 「標題改用淡紫色，**以這個為準**」 | 取代舊值，舊的封存 |

有用的關鍵字：**以後、都、一律、每次**（長期有效）；**這個專案、本專案**（限定範圍）；**以這個為準、改成**（更正）。

**不會被記住的：**

- 只針對這次：「這次只回一句話」「這次測試資料必須正好三筆」。
- 還沒決定：「也許之後可以用深色背景，我再想想」。
- 翻譯或引用的文字、AI 猜測但沒被證實的做法。
- 你明確說不要記的。
- 密碼、token、API key 的**值**一律不存；可以說「API key 放在環境變數 `OPENAI_API_KEY`」，只記位置。

小技巧：一次講一件事比較不會漏；講清楚是「所有專案」還是「這個專案」；說錯了直接用新說法更正，不用改檔案。

## 背景整理什麼時候完成

不看時間，只看累積了幾筆**還沒整理**的紀錄（預設 `dream_every: 20`）：

| 事件 | 算幾筆 |
|---|---|
| 你送出一則訊息 | 1 |
| AI 完成一次回覆 | 1 |
| Claude 執行一次 Shell 指令 | 1 |
| Codex 每輪對話 | 你的訊息數 + 1 |

- 單純問答一來一回算 2 筆，大約 10 輪觸發；AI 有跑指令的工作會更快。
- 沒累積到門檻就停下來時**不會整理**，要等下次繼續對話才會觸發。
- 整理在背景進行，前台不必等，通常幾十秒到幾分鐘。
- 失敗時（例如 CLI 沒登入）紀錄不會遺失，下次觸發會重試；`pending` 會顯示錯誤。
- 想更快：把設定裡的 `dream_every` 改小（例如 `5`），或用下一節的 `dream-run` 立刻整理。

## 查看與手動操作

也可以直接請 AI 幫你跑。下表是**專案版**在專案資料夾的寫法；**全局版**把 `tools/` 換成
`~/.jason-memory-global/framework/tools/`，`--config` 換成 `~/.jason-memory-global/framework/memory-config.json`，
並在 `--config …` 後面加上 `--project 目前資料夾`。

| 想做什麼 | 指令 |
|---|---|
| 目前記了哪些 | `python tools/memory_runtime.py --config .jason-memory.json index` |
| 看某一則筆記 | `python tools/memory_runtime.py --config .jason-memory.json read --note feedback/xxx.md` |
| 背景整理進度、有沒有出錯 | `python tools/memory_ledger.py --config .jason-memory.json pending` |
| 立刻整理（`N` 用 `pending` 的 `head`） | `python tools/memory_ledger.py --config .jason-memory.json dream-run --upto N` |
| 找以前說過的原話、數字、錯誤訊息 | `python tools/memory_ledger.py --config .jason-memory.json search --text "關鍵字"` |
| 查筆記來源是哪句話 | `python tools/memory_ledger.py --config .jason-memory.json source --ref 來源編號` |
| 匯出成一份 Markdown | `python tools/memory_runtime.py --config .jason-memory.json export --output 記憶匯出.md` |
| 從帳本重新整理全部記憶 | `python tools/memory_dream.py --config .jason-memory.json rebuild --yes` |

- `pending` 顯示 `cursor`（整理到哪）、`pending`（還有幾筆）、`running`（是否正在整理）與 `last_error`（上次錯誤）。
- `rebuild` 適合記憶整理得亂了或換了更強的模型：先在暫存區重建，成功才替換，舊筆記封存；失敗時現行筆記不動。
- 筆記是一般 Markdown 檔，也可以直接打開來看。
- 想回到舊的「當輪保存並通知」：把設定的 `foreground_writes` 改成 `true`（全局版用 `install.py --mode foreground` 重裝）。

## 更新與移除

- **更新**：下載新版重新執行 `INSTALL.cmd`，開新對話後生效。筆記、索引與背景整理設定都會保留。
  專案版要把自己的 `.jason-memory/` 複製到新版資料夾，不要用新版的空白索引覆蓋。
  從很舊的版本升級見 [進階與維護](docs/advanced.md#從舊版升級)。
- **備份**：把整個記憶資料夾複製到別處即可，細節見 [備份與換機](docs/advanced.md#備份與換機)。
- **全部移除**：關閉 AI 工作階段後雙擊 `UNINSTALL.bat`，永久刪除框架、記憶（含原始帳本）以及 Claude hooks、
  Codex 規則與 `notify` 設定，保留其他設定與工作檔案。可先用 `UNINSTALL.bat --dry-run` 預覽，
  刪除不可復原，詳見 [清除說明](docs/uninstall.md)。

## 安全與限制

- 筆記和帳本都是**明碼檔案**。憑證值不存進筆記，帳本也會遮蔽，但分享資料夾前仍請先移除記憶資料夾。
  `.gitignore` 不是加密，也不會阻止雲端同步。
- 記憶只是背景資料，可能過時；不會蓋過你目前的指示。
- 記得與否仍取決於 AI 是否遵守規則，以及背景整理所用模型的判斷，不能保證每次都正確分類。
- 所有寫入都經過同一套檔案鎖與版本檢查；直接手動改檔不在保護範圍內，大量整理前請先備份。
- 沒有語意搜尋或跨電腦同步；全局版是同一台電腦上的檔案共用。
- Windows PowerShell 5.1 不要用管線把含中文的 Python 程式碼送給 `python -`（中文會變成 `?`），匯出請用 `export`。

## 更多文件

| 文件 | 內容 |
|---|---|
| [全局版說明](global/README.md) | 全局版的安裝選項、自訂位置、其他 AI 接入與驗證方式 |
| [開始使用](START_HERE.md) | 專案版解壓後的第一步 |
| [進階與維護](docs/advanced.md) | 手動接入既有專案、前台模式、健檢工具、升級、備份與開發測試 |
| [runtime 協議](docs/memory-runtime.md) | 讀寫流程、背景整理設定與交易規則 |
| [帳本格式](docs/ledger-format.md) | 原始帳本的欄位、驗證規則與來源編號 |
| [清除說明](docs/uninstall.md) | `UNINSTALL.bat` 的刪除範圍與預覽 |
| [完整協議](SKILL.md) | 給 Agent 的完整記憶協議 |
| [Releases](https://github.com/Jason5330/jason-memory/releases) | 各版本的變更 |

MIT License，見 [LICENSE](LICENSE)。
