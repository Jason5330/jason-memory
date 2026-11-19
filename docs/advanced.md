# 進階與維護

[README](../README.md) 涵蓋日常使用。本頁收錄較少用到的內容：手動接入既有專案、前台模式、健檢工具細節、
從舊版升級、備份與換機，以及框架開發與測試。

## 目錄

- [手動接入既有專案](#手動接入既有專案)
- [前台模式的通報與 Git 界線](#前台模式的通報與-git-界線)
- [健檢](#健檢)
- [索引整理與可選門檻](#索引整理與可選門檻)
- [從舊版升級](#從舊版升級)
- [備份與換機](#備份與換機)
- [開發與測試](#開發與測試)

## 手動接入既有專案

想讓一個已存在的工作專案使用專案版記憶，而不是另開資料夾時：

1. 下載本框架，放在專案的 `jason-memory/` 子目錄或另外的固定位置。
2. 在專案根目錄建立 `.jason-memory.json`（可照抄框架中的那份）與記憶目錄 `.jason-memory/`；
   索引不存在時，把 [索引範本](../templates/MEMORY.md) 複製為 `MEMORY.md`。重跑時保留既有索引與所有筆記。
3. 把 [常駐規則範本](../templates/standing-rules.md) 的「記憶（Jason-memory）」一節合併到專案根目錄的 `AGENTS.md`。
   已有該節時更新該節，保留其他規則，不要重複附加。替換兩個佔位符：
   - `<MEMORY_ROOT>`：實際記憶路徑，例如 `.jason-memory`。
   - `<FRAMEWORK_ROOT>`：框架原始碼路徑，例如 `jason-memory` 或絕對路徑。
4. 執行 `python 框架路徑/tools/install_hooks.py --project 專案路徑`，一次設定 Claude hooks 與 Codex `notify`。
5. 一般資料夾略過 Git 設定。只有目標已知使用 Git 時，才把記憶目錄加入 `.gitignore`。
6. 確認規則沒有佔位符殘留，然後執行[健檢](#健檢)。

只複製框架、沒有建立設定、記憶目錄與規則，記憶流程不會啟用。舊專案若有本框架以前寫過的 `CLAUDE.md`
匯入行，可以刪除；規則只保留在 `AGENTS.md`。

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

完整協議見 [SKILL.md](../SKILL.md)。

## 前台模式的通報與 Git 界線

本節只適用 `foreground_writes: true`（當輪保存並通知）。這個模式下 AI 在回覆前完成保存與健檢，
並在回覆末尾通報，例如：

```markdown
已將此要求記憶在 [報表要求](.jason-memory/feedback/report-source-data.md)；適用本專案，健檢通過。
```

通知中的連結與結果必須來自本輪工具收據；寫入或健檢失敗要明確說明，不能只口頭說「記住了」。
背景模式下 AI 不會當輪通報，說了「已記住」反而會被 Stop hook 要求更正。

**記憶流程不包含 Git 狀態監控。** 普通資料夾不做 Git 檢查；只有已知使用 Git 的安裝目標才處理 `.gitignore`。
記憶、安裝及工作完成回覆都不附「尚未 git add/commit」之類提醒。只有使用者要求版本控制工作，
或當前開發／審查任務確實需要時，才使用 Git。

## 健檢

在框架根目錄執行；從其他目錄使用時，填入工具與記憶庫的實際路徑，路徑有空格時加引號。

```bash
python tools/jason_check.py <MEMORY_ROOT>/MEMORY.md
python tools/jason_doctor.py <MEMORY_ROOT>
```

### 大小統計：jason_check

預設只回報索引的行數和位元組數；超過 200 行不會因此報錯。以分塊讀取統計，不會把大型檔案一次載入記憶體。

| 結束碼 | 意義 |
|---|---|
| `0` | 統計完成，沒有超過任何已設定門檻 |
| `1` | 超過使用者自行設定的警告門檻 |
| `2` | 超過使用者自行設定的預算 |
| `64` | 指令用法錯誤 |
| `66` | 無法讀取檔案 |

### 一致性檢查：jason_doctor

檢查斷鏈、逃出記憶庫的路徑、從索引無法到達的筆記、重複檔名、必要欄位、日期與非空 Why／How to apply。
兩則互相連結卻沒有索引入口的筆記，也會被判定為孤兒。結束碼 `0` 表示未發現問題，`1` 表示發現問題或未完成檢查。
只檢查結構時加 `--no-schema`。

語法規則：

- 索引用平面清單 `- [標題](slug.md) — 摘要`，也接受 `+`、`*`。副檔名可用 `.MD`；完整檔名大小寫依作業系統。
- 支援錨點、尖括號和連結標題；檔名使用不含空格的 slug。參照式連結、巢狀清單等擴充不在協議範圍。
- 索引入口必須是完整的一般連結；未閉合的括號／標題、圖片及跳脫文字不算有效入口。
- HTML 註解、程式碼範例中的連結不算有效入口。正文的 `[[slug]]` 可連到其他筆記；未建立的前向連結只提示。
- 索引與筆記需為 UTF-8（可含 BOM）。無法解碼的檔案或無法巡覽的目錄會回報未完成檢查。
- frontmatter 是受限的 `key: value` 格式，不是完整 YAML；可讀取 `metadata:` 下的必要欄位。
- Why／How to apply 可以寫在標籤後或下一行。格式檢查不能證明內容真實或方法有效。

doctor 保留 **1 MiB 索引／4 MiB 單篇筆記的解析保護**，超過時回報未完成檢查。這是診斷程式的資源限制，
不是行數限制或寫入攔截。

## 索引整理與可選門檻

索引保持「一行連結＋一句摘要」，內容放進筆記。出現重複或過時內容、讀取成本上升、工具回報截斷時，
再整理摘要、合併重複筆記或封存舊主題。不要只為壓低行數而丟棄有用記憶。

需要自己的大小預算時可設定下列環境變數，**全部預設停用**。值必須是正整數，其他值都視為停用。

| 變數 | 作用 |
|---|---|
| `JASON_WARN` | jason_check 的行數警告 |
| `JASON_WARN_BYTES` | jason_check 的位元組警告 |
| `JASON_HARD` | 兩支健檢工具的行數預算 |
| `JASON_HARD_BYTES` | 兩支健檢工具的位元組預算 |

獨立健檢只回報；writer 會在健檢不通過時拒絕套用。

**與 Claude Code 原生記憶的差別：** 截至 2026-09-09 核對的[官方說明](https://code.claude.com/docs/en/memory#how-it-works)，
原生 auto-memory 索引有前 200 行／25KB 的啟動載入限制。本框架使用獨立目錄並明確讀取，
因此不把該限制設成預設硬門檻；工具若截斷輸出仍需續讀。

## 從舊版升級

下載新版後重新執行 `INSTALL.cmd`，開新對話後生效。既有筆記、索引與其他規則保留，不需要重建記憶庫。
專案版另把自己的 `.jason-memory/` 與 `.jason-memory.json` 的 `memory_root` 帶到新資料夾，不要用新版的空白索引覆蓋。

從很舊的版本升級時另外注意：

- 新版預設背景記憶模式，對話中不再出現「已記住」通知；想維持舊流程，把 `foreground_writes` 設為 `true`
  （全局版用 `install.py --mode foreground`）。
- 規則只放在 `AGENTS.md`；舊版寫在 `CLAUDE.md` 的匯入行或管理區塊可以刪除（全局版安裝時自動移除）。
- 從 Claude Code 設定中移除指向 `jason_index_guard.py` 的舊註冊，保留其他設定。
- 移除舊的「超過 200 行先阻擋」規則；不需要自訂門檻時，也清除 `JASON_WARN`、`JASON_HARD` 及其 `_BYTES`
  環境變數。`JASON_INDEX_NAME`／`JASON_INDEX_PATH` 已不再使用。
- 不需要搬動筆記或更換 frontmatter；空白 Why／How 說明需要補齊。完成後重跑兩支健檢。

協議層面的變更與限制見 [runtime 協議](memory-runtime.md)。

## 備份與換機

先暫停對話並健檢，再把完整記憶目錄（含索引、筆記、`archive/` 與 `.raw/`）複製到記憶庫外的備份位置，
避免備份被當成重複筆記。換機時還原到新位置、更新規則中的記憶與框架路徑，再健檢與抽查內容。
記憶預設不進 Git，`git clone` 不會攜帶它。

只想閱讀或留存時，用 `export` 匯出成一份 Markdown（見 README）。它由 Python 直接以 UTF-8 寫入新檔，
不覆蓋既有檔。

**Windows PowerShell 5.1：** 不要用 `@' ... '@ | python -` 這種管線把含中文的 Python 程式碼送進去，
中文會變成 `?` 並寫進檔案。改用 `export`，或把程式碼存成 UTF-8 的 `.py` 檔再執行；必須用管線時先執行
`$OutputEncoding = New-Object System.Text.UTF8Encoding($false)`。

錯誤或過時的筆記預設封存，移除活躍指標並保留取代關係；永久刪除依使用者指示。doctor 不檢查封存內容。

## 開發與測試

以下供框架維護者使用。換電腦或換 AI 接手開發時，先讀 [開發交接入口](../HANDOFF.md) 與
[踩坑經驗](踩坑經驗總結.md)。

產生乾淨下載包（不呼叫 Git，只打包明確列出的公開檔案，目標 ZIP 已存在時拒絕覆寫）：

```bash
python tools/build_download.py --output dist/jason-memory.zip
python tools/build_global_download.py --output dist/jason-memory-global.zip
```

```text
jason-memory/
├── README.md / START_HERE.md / AGENTS.md / SKILL.md
├── .jason-memory.json
├── INSTALL.cmd / UNINSTALL.bat
├── global/                 # 全局版安裝器與說明
├── tools/
│   ├── memory_runtime.py   # 讀寫、索引、匯出
│   ├── memory_ledger.py    # 原始帳本與背景整理觸發
│   ├── memory_dream.py     # 背景整理 worker 與 rebuild
│   ├── memory_facts.py     # 憑證判斷與遮蔽
│   ├── claude_memory_hook.py / codex_notify.py
│   ├── install_hooks.py / purge_memory.py
│   └── jason_check.py / jason_doctor.py
├── docs/
├── templates/
├── tests/
└── .github/workflows/
```

在框架根目錄執行：

```bash
python -m unittest discover -s tests -v
```

測試使用臨時資料，涵蓋索引、帳本雜湊鏈、背景整理觸發與重試、重建、安裝與清除、全局版端到端流程，
以及 `tests/test_qa_matrix.py` 對打包後全局版 ZIP 的驗收矩陣；單元測試不呼叫真實模型。
CI 跑 Windows／Linux、Python 3.9／3.13。本機不允許建立 symlink 時，對應測試會明確跳過。

規則更新後另做[多輪對話驗收](../tests/behavioral-memory.md)，判定方式見 [evals/README.md](../evals/README.md)，
可重跑的提示在 [evals/evals.json](../evals/evals.json)。格式測試通過不等於語意行為已被驗證。
[2026-09-16 實測紀錄](../evals/results/2026-09-16.md) 有 16 種情境的新舊版對照與限制。
