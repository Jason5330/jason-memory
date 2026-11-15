# Jason-memory

一套以 Markdown 檔案為基礎的 AI Agent 記憶。Claude Code 與 Codex 共用同一份記憶，讓新對話記得你的偏好、
專案限制與未完成工作，不需要資料庫、伺服器或 Git。

**預設是背景記憶模式：前台只管做事，記憶交給背景整理。**

- 對話中 AI 不寫記憶、不分心判斷「這句以後有沒有用」，也不會說「已記住」。
- 每輪對話自動記入只追加、防竄改的原始帳本；累積約 20 筆後，背景程序呼叫本機已登入的
  Claude Code 或 Codex CLI，把可重用的偏好、要求與更正整理成附來源的筆記。
- 新對話只帶一份小索引和常駐偏好，其他筆記需要時才讀；要查確切原文、數字或錯誤訊息時回查原始帳本。
- 想要舊的「當輪保存並通知」，把 `.jason-memory.json` 的 `foreground_writes` 改成 `true`。

**兩種版本，都是解壓後雙擊一次 `INSTALL.cmd`，同時設定 Claude Code 與 Codex：**

| 版本 | 適合 | 下載 |
|---|---|---|
| 全局版 | 想在電腦上**任何資料夾**都有記憶；一般偏好全局共用，各專案需求分開存 | [jason-memory-global.zip](https://github.com/Jason5330/jason-memory/releases/latest/download/jason-memory-global.zip)・[說明](global/README.md) |
| 專案版 | 只在**這一個資料夾**工作，記憶跟著資料夾走 | [jason-memory.zip](https://github.com/Jason5330/jason-memory/releases/latest/download/jason-memory.zip)・[開始使用](START_HERE.md) |

兩個版本可以並存：資料夾裡有專案版設定（`.jason-memory.json`）時，以專案版為準。兩者不會自動搬移彼此的記憶。
需要 Python 3.9+；背景整理需要本機已安裝並登入 Claude Code 或 Codex CLI。不需要 Git、clone、commit 或 PR。

**一鍵全部刪除：** 關閉 AI 工作階段後，雙擊套件裡的 `UNINSTALL.bat`，永久刪除框架、記憶（含原始帳本）
以及 Claude hooks、Codex 規則與 `notify` 設定；保留其他設定與工作檔案。可先用 `UNINSTALL.bat --dry-run`
預覽，刪除不可復原，詳見 [清除說明](docs/uninstall.md)。

**換電腦／更換 AI 接手開發：** 先讀 [開發交接入口](https://github.com/Jason5330/jason-memory/blob/master/HANDOFF.md)，
再依需要查閱 [踩坑經驗](https://github.com/Jason5330/jason-memory/blob/master/docs/踩坑經驗總結.md)。
各版本的變更見 [Releases](https://github.com/Jason5330/jason-memory/releases)。

## 目錄

- [運作方式](#運作方式)
- [安裝](#安裝)
- [日常使用](#日常使用)
- [使用方法與技巧](#使用方法與技巧)
- [背景整理怎麼判斷](#背景整理怎麼判斷)
- [前台模式的通報與 Git 界線](#前台模式的通報與-git-界線)
- [健檢](#健檢)
- [索引整理與可選門檻](#索引整理與可選門檻)
- [從舊版升級](#從舊版升級)
- [備份與換機](#備份與換機)
- [安全與能力邊界](#安全與能力邊界)
- [開發與測試](#開發與測試)

## 運作方式

```text
你的專案/
├── AGENTS.md              # 唯一的常駐規則（Codex 與 Claude Code 共用）
├── .jason-memory.json     # 設定：記憶位置、背景模式、整理門檻
└── .jason-memory/         # 你的本機記憶庫
    ├── MEMORY.md          # 索引：一行連結＋一句摘要
    ├── feedback/ user/ project/ reference/   # 筆記
    ├── archive/           # 被更正或封存的舊筆記（可追溯）
    └── .raw/              # 原始帳本、文件鏡像與背景整理狀態
```

全局版的記憶在 `~/.jason-memory-global/memory/`：`shared/` 是所有資料夾共用的偏好，
`projects/<雜湊>/` 是各資料夾自己的需求與帳本。

一輪對話的流程：

1. **召回**：Claude 由 hooks 在啟動與每輪注入索引和常駐偏好；Codex 依 `AGENTS.md` 讀索引，
   需要時再讀單篇筆記。正常召回保持安靜，不說「我來讀取記憶」。
2. **做事**：AI 只處理你的任務；你在這一輪說的話本來就優先，不需要先存成記憶。
3. **記錄**：你的訊息、AI 的回覆與 Shell 工具結果自動追加到原始帳本
   （Claude 靠 hooks，Codex 靠 `notify`）。憑證值會遮蔽。
4. **背景整理**：未整理的紀錄達到門檻（預設 20 筆）時，分離程序把這批紀錄交給 Agent CLI，
   整理成筆記並標出「來源：ledger seq N」。成功才前進；失敗下次重試；沒有新紀錄就不跑。
5. **下一輪生效**：筆記更新後，下一則訊息起的召回就會帶入。

| 類型 | 記錄內容 |
|---|---|
| `user` | 使用者的身份、職責，以及已確認的持久個人偏好 |
| `feedback` | 持久工作需求與偏好、叮嚀、踩坑教訓、驗證有效的做法 |
| `project` | 專案目標、決策、限制、卡點及未完成工作的下一步 |
| `reference` | 未來會用到的 URL、票單、儀表板或檔案位置 |

筆記範例：

```markdown
---
name: preference-d92a3014d20a96bf
description: "回報時一律先列結論，再說明原因"
type: feedback
created: 2026-11-12
updated: 2026-11-12
---

回報時一律先列結論，再說明原因

Why: 使用者明確要求以後回報都這樣做；來源：ledger seq 1

How to apply: 全局共用；回報時一律先列結論，再說明原因
```

正文、摘要預設繁體中文；欄位名、類型代號及 Why／How to apply 標籤維持英文。

## 安裝

健檢工具需要 **Python 3.9+**，只使用標準函式庫。Codex 與 Claude Code 都讀專案根目錄的 `AGENTS.md`，
不需要 `CLAUDE.md`。[Codex 官方規則載入說明](https://learn.chatgpt.com/docs/agent-configuration/agents-md)。

### 全局版（任何資料夾都能用）

1. 下載 [jason-memory-global.zip](https://github.com/Jason5330/jason-memory/releases/latest/download/jason-memory-global.zip) 並解壓。
2. 雙擊 `INSTALL.cmd`。Claude Code 會合併全局 hooks；Codex 會寫入 `~/.codex/AGENTS.md` 規則，並在
   `config.toml` 加入記錄對話用的 `notify`。看結果裡的 `codex_notify` 與 `warnings`：你原本就有別的
   `notify` 程式時會保留它並提出警告，此時 Codex 對話不會被記錄。
3. 之後在任何資料夾用 Claude Code 或 Codex 開新對話即可。詳見 [全局版說明](global/README.md)。

### 專案版：直接使用下載版

1. 下載專案版 ZIP，解壓到獨立的一般資料夾，例如桌面。不要放進其他 Git 儲存庫。
2. 雙擊 `INSTALL.cmd`：合併本資料夾的 Claude hooks，並在 Codex 設定加入 `notify`
   （已有其他 Jason-memory 安裝的 `notify` 時共用它）。可先用 `INSTALL.cmd --dry-run` 預覽。
3. 用 Claude Code 或 Codex 開啟這個資料夾，建立**新對話**，直接提出工作需求。
   專案版只對這個資料夾有效；桌面另開的資料夾不會有記憶，需要時請用全局版。

也可以在 GitHub 選 **Code → Download ZIP** 下載原始碼；原始碼 ZIP 不含個人記憶，
第一次使用由 AI 從範本建立索引。兩種方式都不需 Git，也不要執行 `git init`。
若看到 `master`／`Create PR`，那是宿主 App 的 Git 介面，不是記憶功能；請改開
新解壓的獨立資料夾，避免置於其他儲存庫內。詳見 [開始使用](START_HERE.md)。

### 加入已存在的其他工作專案

1. 下載本框架。它可以放在專案的 `jason-memory/` 子目錄或另外的固定位置。
2. 在專案根目錄建立 `.jason-memory.json`（可照抄框架中的那份）與記憶目錄 `.jason-memory/`；索引不存在時，把 [索引範本](templates/MEMORY.md) 複製為 `MEMORY.md`。重跑安裝時保留既有索引與所有筆記。
3. 把 [常駐規則範本](templates/standing-rules.md) 的「記憶（Jason-memory）」一節合併到專案根目錄的 `AGENTS.md`。已有該節時更新該節，保留其他規則；不要重複附加。替換兩個佔位符：
   - `<MEMORY_ROOT>`：實際記憶路徑，例如 `.jason-memory`。
   - `<FRAMEWORK_ROOT>`：框架原始碼路徑，例如 `jason-memory` 或絕對路徑。
4. 執行 `python 框架路徑/tools/install_hooks.py --project 專案路徑`，一次設定 Claude hooks 與 Codex `notify`。
5. 一般資料夾直接略過 Git 設定。只有目標已知使用 Git 時，才將記憶目錄加入 `.gitignore`；不為記憶啟用而探測、初始化 Git，也不建立監控或提交提醒。
6. 確認套用後的規則沒有佔位符殘留，然後執行健檢。

舊專案若有本框架以前寫過的 `CLAUDE.md` 匯入行，可以刪除；規則只保留在 `AGENTS.md`。
只複製框架但沒有建立設定、記憶目錄與規則，記憶流程不會啟用。

若直接在本框架專案工作，根目錄的 [AGENTS.md](AGENTS.md) 已指向本機 `.jason-memory/` 與本專案的工具；它們不是供其他路徑直接複製的安裝範本。更新規則後開啟新對話，確認 host 有載入常駐規則。

也可以把這段提示詞交給 Agent：

```text
請在目前專案初始化 Jason-memory：
框架原始碼位於 <框架絕對路徑>。
在專案根目錄建立 .jason-memory.json（照抄框架的那份）與 .jason-memory；只有
MEMORY.md 不存在時才使用 templates/MEMORY.md 初始化，保留既有索引與筆記。
將框架的 templates/standing-rules.md 合併進專案根目錄的 AGENTS.md
（Codex 與 Claude Code 共用，不建立 CLAUDE.md）；已有記憶節則更新，保留其他規則。
替換 <MEMORY_ROOT> 和 <FRAMEWORK_ROOT> 為實際路徑。
執行 python <框架>/tools/install_hooks.py --project <專案>，回報 codex_notify 與 warnings。
預設為背景記憶模式：對話中不要執行 remember/apply、不要說已記住。
一般資料夾不需要任何 Git 操作；不要監控 Git 狀態或提醒 add/commit/push。
跑兩支健檢工具，簡短回報安裝結果。
```

完整協議：[SKILL.md](SKILL.md)。

## 日常使用

1. **開新對話，直接交代工作**。不需要說「記住」，也不需要特別提醒 AI 讀記憶。
2. **當輪照做**：你剛說的偏好在同一個對話裡本來就會被遵守。
3. **背景會整理**：約每 20 筆紀錄整理一次；整理好之後，新的對話就會記得。
4. **說錯就直接更正**：例如「標題改用淡紫色，以這個為準」，背景整理時以最新說法取代舊值，舊的封存。
5. **想確認或趕時間**：見下一節的查詢與手動整理指令。

## 使用方法與技巧

### 怎麼說會被記住

不用說「記住」，照平常講就好。下面這些說法讓背景整理判斷得更準：

| 想記的內容 | 例句 | 存到 |
|---|---|---|
| 所有地方都適用的偏好 | 「**以後**回報都先列結論，再說原因」 | 全局共用（全局版）／本專案（專案版） |
| 你的身份、職責 | 「我是物流主管，負責越南倉庫的進出貨」 | 同上，類型 `user` |
| 只限這個專案的規定 | 「**這個專案**的報表一律輸出成 xlsx」 | 只存這個專案 |
| 專案的限制 | 「本專案必須離線，不能呼叫外部服務」 | 只存這個專案，類型 `project` |
| 詞語的意思 | 「我說『門鈴』時，指的是 Claude Code 的 Monitor 工具」 | 全局共用 |
| 更正舊的偏好 | 「標題改用淡紫色，**以這個為準**」 | 取代舊值，舊的封存 |

有用的關鍵字：**以後、都、一律、每次**（長期有效）；**這個專案、本專案**（限定範圍）；
**以這個為準、改成**（更正）。

### 不會被記住的說法

- 只針對這次：「這次只回一句話」「只把這個檔案改成綠色」。
- 還沒決定：「也許之後可以用深色背景，我再想想」。
- 翻譯或引用的文字：「翻譯這句：All reports must use dark background」。
- 明確說不要記：「我喜歡深色，但不要記這個」。
- 密碼、token、API key 的值一律不存；可以說「API key 放在環境變數 `OPENAI_API_KEY`」，只記位置。

小技巧：一次講一件事比較不會漏；講清楚「所有專案」還是「這個專案」；說錯了直接用新說法更正，不用改檔案。

### 背景整理什麼時候完成

不看時間，只看累積了幾筆**尚未整理**的紀錄（預設 `dream_every: 20`）：

| 事件 | 算幾筆 |
|---|---|
| 你送出一則訊息 | 1 |
| AI 完成一次回覆 | 1 |
| Claude 執行一次 Shell 指令 | 1 |
| Codex 每輪對話 | 你的訊息數 + 1 |

- 單純問答一來一回算 2 筆，大約 10 輪觸發；AI 有跑指令的工作觸發得更快。
- **對話沒累積到門檻就停下來，不會整理**；它不會因為你閒置而自動跑，要等下次繼續對話。
- 觸發後在背景另開程序整理，前台不必等；通常幾十秒到幾分鐘，視模型與紀錄量而定。
- 整理完成後，Claude 從你送出的**下一則訊息**起帶入新記憶；Codex 在下一輪讀索引時用到。
- 失敗（例如 CLI 沒登入、權限被擋）時紀錄不會遺失，下次觸發會重試。

### 查看與確認

專案版在專案資料夾執行；全局版把設定換成 `~/.jason-memory-global/framework/memory-config.json`，
工具換成 `~/.jason-memory-global/framework/tools/` 下的同名檔，並加上 `--project 目前資料夾`。也可以直接請 AI 幫你跑。

| 想知道 | 指令 |
|---|---|
| 目前記了哪些 | `python tools/memory_runtime.py --config .jason-memory.json index` |
| 看某一則筆記 | `python tools/memory_runtime.py --config .jason-memory.json read --note feedback/xxx.md` |
| 背景整理進度、有沒有出錯 | `python tools/memory_ledger.py --config .jason-memory.json pending` |
| 找以前說過的原話、數字、錯誤訊息 | `python tools/memory_ledger.py --config .jason-memory.json search --text "關鍵字"` |
| 匯出成一份 Markdown | `python tools/memory_runtime.py --config .jason-memory.json export --output 記憶匯出.md` |
| 筆記寫的來源是哪句話 | `python tools/memory_ledger.py --config .jason-memory.json source --ref 來源編號` |

`pending` 會顯示 `cursor`（整理到哪）、`pending`（還有幾筆沒整理）、`running`（是否正在整理）與
`last_error`（上次錯誤）。筆記的 `Why` 會寫「來源：ledger 識別碼:seq:雜湊」，把這串交給 `source --ref`
就能查回並驗證原始紀錄；全局版從任何資料夾都能查到別的專案的來源。筆記是一般 Markdown 檔，也可以直接打開來看。

### 想更快生效

- **調低門檻**：把設定的 `dream_every` 改小（例如 `5`），兩三輪就整理一次；代價是較常呼叫模型。
- **手動立刻整理**：先用 `pending` 看 `head`，再執行（會等整理跑完才結束）：

  ```text
  python tools/memory_ledger.py --config .jason-memory.json dream-run --upto <head 數字>
  ```

### 其他常用操作

- **記憶整理得亂了，或換了更強的模型**：
  `python tools/memory_dream.py --config .jason-memory.json rebuild --yes`
  從原始帳本重新整理。先在暫存庫重建，成功才替換；舊筆記封存，失敗時現行筆記不動。
- **回到「當輪保存並通知」**：設定的 `foreground_writes` 改成 `true`（全局版用 `install.py --mode foreground` 重裝）。
- **更新版本**：先備份記憶，下載新版重新執行 `INSTALL.cmd`；專案版把自己的 `.jason-memory/` 複製到新版資料夾，不要用新版空白索引覆蓋。
- **全部移除**：雙擊 `UNINSTALL.bat`（可先 `--dry-run`）。

### 要注意的

- 背景整理需要本機已安裝並登入 Claude Code 或 Codex CLI；找不到時對話照樣記入帳本，但不會整理成筆記，`pending` 會顯示錯誤。
- 筆記和帳本都是明碼檔案；帳本開啟後保存對話全文（憑證值遮蔽）。分享資料夾前先移除記憶資料夾。
- Windows PowerShell 5.1 不要用管線把含中文的 Python 程式碼送給 `python -`，中文會變成 `?`；匯出請用 `export`。

## 背景整理怎麼判斷

判斷依據是「使用者是否清楚表達或已有證據，以及未來是否有用」，不是有沒有出現「記住」關鍵字。
每一段紀錄都重新判斷：上一輪是一次性任務，不代表下一輪提出的格式規範也是一次性。

| 情境 | 背景整理的處理 |
|---|---|
| 「以後的報表都保留原始資料」 | 保存為 `feedback` |
| 隨機產生資料後補充：「輸出的表格，欄位標題都必須填滿淺藍色」 | 記住本專案的表格格式要求；隨機姓名電話不記 |
| 「只把這個檔案的標題列改成淺藍色」 | 當次操作，不推成通用偏好 |
| 「這次測試資料必須正好三筆」 | 即使有「必須」，仍是一次性數量要求 |
| 「本專案必須能離線使用」 | 保存為 `project` 限制 |
| 使用者更正之前的要求 | 更新原筆記，替換或縮小適用範圍，舊版封存 |
| AI 用工具查出的事實 | 只有工具結果實際證實的內容才寫成已驗證規則 |
| 尚未驗證的解法或猜測的偏好 | 不當成持久規則 |
| 重複、機密值或使用者說不要記 | 略過 |

「明天下午三點提醒我」需要外部排程與通知管道；本框架可以記錄待辦，但記錄成功不代表已設定定時提醒。

## 前台模式的通報與 Git 界線

本節只適用 `foreground_writes: true`（當輪保存並通知）。這個模式下 AI 在回覆前完成保存、健檢，
並在回覆末尾通報，例如：

```markdown
已將此要求記憶在 [報表要求](.jason-memory/feedback/report-source-data.md)；適用本專案，健檢通過。
```

通知中的連結與結果必須來自本輪工具收據；寫入或健檢失敗要明確說明，不能只口頭說「記住了」。
背景模式下 AI 不會當輪通報，說了「已記住」反而會被 Stop hook 要求更正。

**記憶流程不包含 Git 狀態監控。** 普通資料夾不做 Git 檢查；只有已知使用 Git 的安裝目標才處理 `.gitignore`。一般記憶、安裝及工作完成回覆，都不附「尚未 git add/commit，需要的話再說」。只有使用者要求版本控制工作，或當前開發／審查任務確實需要時，才使用 Git。

## 健檢

以下指令在框架根目錄執行；從其他目錄使用時，請填入工具與記憶庫的實際路徑。路徑有空格時加引號。

```bash
python tools/jason_check.py <MEMORY_ROOT>/MEMORY.md
python tools/jason_doctor.py <MEMORY_ROOT>
```

### 大小統計：jason_check

預設只回報索引的行數和實際位元組數；超過 200 行不會因此報錯。以分塊讀取統計，避免把大型檔案一次載入記憶體。

| 結束碼 | 意義 |
|---|---|
| `0` | 統計完成，沒有超過任何已設定門檻 |
| `1` | 超過使用者自行設定的警告門檻 |
| `2` | 超過使用者自行設定的預算 |
| `64` | 指令用法錯誤 |
| `66` | 無法讀取檔案 |

### 一致性檢查：jason_doctor

檢查斷鏈、逃出記憶庫的路徑、從索引無法到達的筆記、重複檔名、必要欄位、日期與非空 Why／How to apply 說明。兩則互相連結卻沒有索引入口的筆記，也會被判定為孤兒。

結束碼 `0` 表示未發現問題，`1` 表示發現問題或未完成檢查。只檢查結構時：

```bash
python tools/jason_doctor.py <MEMORY_ROOT> --no-schema
```

語法規則：

- 索引用平面清單 `- [標題](slug.md) — 摘要`，也接受 `+`、`*`。副檔名可用 `.MD`；完整檔名大小寫仍依作業系統。
- 支援錨點、尖括號和連結標題；檔名使用不含空格的 slug。參照式連結、巢狀清單等 Markdown 擴充不在協議範圍。
- 索引入口必須是完整的一般連結；未閉合的括號／標題、圖片及跳脫文字不算有效入口。
- HTML 註解、程式碼範例中的連結不算有效入口。正文的 `[[slug]]` 可連到其他筆記；未建立的前向連結會提示，但不直接判錯。
- 索引與筆記需為 UTF-8（可含 BOM）。無法解碼的檔案或無法巡覽的目錄會回報未完成檢查，避免誤判正常。
- frontmatter 是受限的 `key: value` 格式，不是完整 YAML；可讀取 `metadata:` 下的必要欄位。
- Why／How to apply 的說明可以寫在標籤後或下一行。格式檢查不能證明內容真實或方法有效。

doctor 保留 **1 MiB 索引／4 MiB 單篇筆記的解析保護**，超過時明確回報未完成檢查。這是診斷程式的資源限制，不是 200 行限制、host 載入上限或寫入攔截。

## 索引整理與可選門檻

索引保持「一行連結＋一句摘要」，內容放進筆記。出現重複或過時內容、讀取成本上升、工具回報截斷時，再整理摘要、合併重複筆記或封存舊主題。不要只為壓低行數而丟棄有用記憶。

若要設定自己的大小預算，以下環境變數仍可使用；**全部預設停用**。值必須是正整數，未設定、無效值、零或負數都停用該維度。

| 變數 | 作用 |
|---|---|
| `JASON_WARN` | jason_check 的行數警告 |
| `JASON_WARN_BYTES` | jason_check 的位元組警告 |
| `JASON_HARD` | 兩支健檢工具的行數預算 |
| `JASON_HARD_BYTES` | 兩支健檢工具的位元組預算 |

獨立健檢只回報；新版 writer 會在健檢不通過時拒絕套用。

**與官方原生記憶的差別：**截至 2026-09-09 核對的 Claude Code 官方文件，原生 auto-memory 索引仍有前 200 行／25KB 的啟動載入限制，接近或超過限制會提示處理。本框架使用獨立專案目錄並要求明確讀取，因此不把原生自動載入限制設成 Jason 的預設硬門檻；實際工具若截斷輸出仍需續讀。[官方說明](https://code.claude.com/docs/en/memory#how-it-works)

## 從舊版升級

升級方式及限制見 [runtime 協議](docs/memory-runtime.md)。

- 下載新版後重新執行 `INSTALL.cmd`（專案版另把自己的 `.jason-memory/` 與 `.jason-memory.json` 的 `memory_root` 帶到新資料夾）；開新對話後生效。既有筆記、索引與其他規則保留，不需重建記憶庫。
- 新版預設背景記憶模式，對話中不會再出現「已記住」通知；想維持舊流程，把 `foreground_writes` 設為 `true`（全局版用 `--mode foreground`）。
- 規則只放在 `AGENTS.md`；舊版寫在 `CLAUDE.md` 的 Jason-memory 匯入行或管理區塊可以刪除（全局版安裝時會自動移除）。
- 曾安裝舊版者，從專案或使用者的 Claude Code 設定中移除指向 `jason_index_guard.py` 的那一筆註冊，保留其他事件處理與設定。本 repo 的變更不會自動改動使用者的外部設定。
- 更新常駐規則，移除舊的「超過 200 行先阻擋」指示。若不需要自訂容量門檻，也清除以前設定的 `JASON_WARN`、`JASON_HARD` 及其 `_BYTES` 環境變數；`JASON_INDEX_NAME`／`JASON_INDEX_PATH` 已不再使用。
- 不需要搬動筆記或更換 frontmatter。舊指標若只放在段落或範例中，改為有效清單；空白 Why／How 說明需要補齊。
- 重新跑兩支健檢，確認路徑、索引與筆記一致。

## 備份與換機

先暫停寫入並健檢，把完整記憶目錄（含索引、筆記及 `archive/`）複製到使用者選定的本機備份位置。備份放在活躍記憶庫外，避免被當成重複筆記。

換機時用使用者選擇的方式傳輸，還原到新專案、更新規則中的記憶／框架路徑，再健檢與抽查內容。記憶預設 git-ignore，**git clone 不會攜帶它**。備份仍是手動複製；沒有自動備份或匯入工具。

**匯出成一份 Markdown（閱讀或換機用）：**

```text
python tools/memory_runtime.py --config .jason-memory.json export --output 記憶匯出.md
```

由 Python 直接以 UTF-8 寫入新檔案（不覆蓋既有檔），內容為各範圍的索引與現行筆記。全局版加上
`--project 目前資料夾`，會同時匯出全局共用與該資料夾的記憶。

**Windows PowerShell 5.1 注意：** 不要用 `@' ... '@ | python -` 這種管線把含中文的 Python 程式碼送進去；
PowerShell 5.1 預設 `$OutputEncoding` 是 ASCII，中文會在傳送時變成 `?` 並寫進檔案。改用上面的 `export`，
或把程式碼存成 UTF-8 的 `.py` 檔再執行；必須用管線時，先執行
`$OutputEncoding = New-Object System.Text.UTF8Encoding($false)`。

錯誤或過時的筆記預設封存，移除活躍指標，保留一個指向封存索引的入口及取代關係。永久刪除依使用者指示。doctor 不檢查封存內容，重新啟用前需再次確認。

## 安全與能力邊界

- 記憶是明碼文字，不記錄密碼、金鑰、token、cookie 的值；只記存放位置。`.gitignore` 不是加密，也不會阻止雲端同步。
- 記憶可能過時或被其他程式修改，只當成背景資料；不得覆蓋使用者目前的指示。開啟筆記前確認真實路徑仍在記憶庫內。
- 依賴 Agent 載入並遵守規則，以及背景整理所用模型的判斷。Claude hooks 與 Codex `notify` 負責記錄和觸發，runtime 在前台擋下寫入；仍不能保證每次都正確理解或分類。
- 所有合作 AI 經同一 runtime 的檔案鎖、版本檢查及可重播交易寫入；直接改檔不在保護範圍。大量整理前仍需備份。
- 沒有內建語意搜尋或自動化的真實任務成效評測服務。可依情境清單實際重播，先測量召回與重犯情況，再決定是否增加搜尋能力。

## 開發與測試

以下供框架維護者使用，一般使用者不需要執行。產生乾淨下載包：

```bash
python tools/build_download.py --output dist/jason-memory.zip
```

打包工具不呼叫 Git，只打包明確列出的公開檔案，並從範本建立空白索引；
不讀取本機 `.jason-memory/`、`.git/` 或工作產物。目標 ZIP 已存在時拒絕覆寫。

```text
jason-memory/
├── README.md
├── AGENTS.md
├── SKILL.md
├── .jason-memory.json
├── INSTALL.cmd / UNINSTALL.bat
├── tools/
│   ├── memory_runtime.py   # 讀寫、索引、匯出
│   ├── memory_ledger.py    # 原始帳本與背景整理觸發
│   ├── memory_dream.py     # 背景整理 worker 與 rebuild
│   ├── claude_memory_hook.py / codex_notify.py
│   ├── install_hooks.py / purge_memory.py
│   ├── jason_check.py
│   └── jason_doctor.py
├── templates/
├── tests/
└── .github/workflows/tests.yml
```

在框架根目錄執行：

```bash
python -m unittest discover -s tests -v
```

測試使用臨時資料，涵蓋索引、帳本雜湊鏈、背景整理觸發與重試、重建、安裝與清除、全局版端到端流程等；單元測試不會呼叫真實模型。CI 配置 Windows／Linux、Python 3.9／3.13。本機若不允許建立 symlink，對應測試會明確跳過。

規則更新後另做[多輪對話驗收](tests/behavioral-memory.md)：背景模式下確認紀錄進入帳本、背景整理後的筆記類型與範圍正確，再開新對話驗證召回；另測當次限制、規則更正與重複要求。判定方式見 [evals/README.md](evals/README.md)。格式測試通過不等於這些語意行為已被驗證。

可重跑的提示與預期結果在 [evals/evals.json](evals/evals.json)。驗收時逐輪送出訊息，不向受測 Agent 提供預期結果；分別保留原版及新版的回覆、記憶快照與產物。

[2026-09-16 實測紀錄](evals/results/2026-09-16.md) 包含 16 種情境的新舊版對照、跨輪與新對話召回，以及測試的限制。

MIT License，見 [LICENSE](LICENSE)。
