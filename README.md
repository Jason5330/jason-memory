# Jason-memory

一套**零基礎設施的 AI Agent 記憶協議**——不是資料庫、不是框架、不需要伺服器。
記憶就是一個資料夾裡放很多小的、人類可讀的 Markdown 筆記檔，加上一份「每次都會被載入」的索引檔 `MEMORY.md`。

Jason-memory 改寫自開源專案 [tinqiao-oss/engramory](https://github.com/tinqiao-oss/engramory)（MIT 授權，版權見 [LICENSE](LICENSE)），核心差異是把 `feedback`（經驗回饋）這個類型的用途收得更窄、更明確：

> **`feedback` 只記兩件事：踩過的坑、驗證有效的做法。** 而且 Agent 要**自己判斷**該不該記，不是只有使用者開口糾正才記。

目標：讓 Agent 跟同一個使用者、同一個專案協作得越久，就越懂這個人的做事習慣，**不再犯同一個錯**。

---

## 目錄

- [核心概念](#核心概念)
- [安裝](#安裝)
- [Hook：容量上限強制](#hook容量上限強制)
- [健檢（Health Check）](#健檢health-check)
- [檔案結構](#檔案結構)
- [設定選項](#設定選項)
- [安全與隱私](#安全與隱私)
- [已知限制](#已知限制)
- [授權與致謝](#授權與致謝)

---

## 核心概念

### 一個索引 + 多個筆記檔，全部平放

```
<MEMORY_ROOT>/
├── MEMORY.md              ← 唯一索引檔，每次任務開始都會被讀取
├── verify-before-done.md  ← 一則筆記 = 一件事，全部平放在同一層，不分子資料夾
├── api-retry-bug.md
└── ...
```

`MEMORY.md` 裡**只放指標**（一行一則連結 + 一句話摘要），實際內容寫在各自的筆記檔裡：

```markdown
## feedback
- [Verify before done](verify-before-done.md) — 改完程式碼要主動驗證是否真的生效
```

每個筆記檔用 YAML frontmatter 標記類型：

```markdown
---
name: verify-before-done
description: 改完程式碼要主動驗證是否真的生效
metadata:
  type: feedback
  created: 2026-07-31
  updated: 2026-07-31
---

**Why:** 使用者曾遇過「看起來改成功但其實沒改到」的情況。
**How to apply:** 每次修改後，grep 改動的元素並附上結果，或跑測試並附上輸出。
```

### 四種類型

| 類型 | 放什麼 |
|---|---|
| `user` | 使用者是誰：身份、角色、專長、對「他本人」的長期偏好 |
| **`feedback`** | **只放踩坑教訓 + 驗證有效的做法**（見下方，這是 Jason-memory 的核心設計） |
| `project` | 專案現況：目標、決策、卡點、下一步——一般性事實、任務狀態都放這裡，不要塞進 `feedback` |
| `reference` | 外部資源指標：URL、儀表板、票單、log 路徑 |

### `feedback`：本框架的核心，而且要「自己判斷」

跟原版 Engramory 不同，Jason-memory 把 `feedback` 定義得很窄，**只記兩種情境**，而且 Agent 必須主動判斷，不能只等使用者說「記住這個」：

1. **踩坑（使用者糾正 或 自己發現）**
   - 使用者糾正你、指出你錯了——這種好判斷
   - **更重要的是使用者完全沒說話的情況**：指令跑錯後自己找到對的做法、重看自己輸出時發現假設錯了、測試失敗後自己修好、走到一半發現此路不通而放棄的做法
   - 判斷標準：「如果換一個從零開始的我，會不會踩到同一個坑？」會的話就寫
2. **成功經驗（使用者確認 或 自己驗證）**
   - 使用者說「對，就這樣做」
   - **或是你自己拿到的證據**：測試通過、一次就修好的 workaround、某個不直覺但確認必要的處理順序——不需要使用者評論，你自己的驗證證據就足夠

兩種情況都要寫成同樣格式，內文必須含：
- `Why:` 錯在哪裡/驗證了什麼，附證據（錯誤訊息、失敗的測試、被打回的 diff）
- `How to apply:` 下次該怎麼做、什麼情境該想起這則筆記

拿不準的時候**傾向寫**：漏記一個教訓的代價（重蹈覆轍）比多記一則筆記的代價（之後花點時間整理）更高。

完整規則文字見 [`rules-snippet.md`](rules-snippet.md)，詳細協議規格見 [`SKILL.md`](SKILL.md)。

---

## 安裝

需求：**Python 3.9+**（`hooks/` 和 `tools/` 都是純 Python，跨平台）。

以 **Claude Code** 為例：

1. **下載/clone 這個 repo** 到你想要的位置（例如專案內的 `jason-memory/`，或任何你喜歡的固定路徑）
2. **選一個 `<MEMORY_ROOT>`**：實際存放記憶筆記的資料夾，例如你的專案下 `.jason-memory/`。把 [`templates/MEMORY.md`](templates/MEMORY.md) 複製進去當作起始索引檔
3. **載入規則**：打開 [`rules-snippet.md`](rules-snippet.md)，把裡面所有 `<MEMORY_ROOT>` 佔位符換成你在步驟 2 選的實際路徑，整份貼進：
   - `~/.claude/CLAUDE.md`（全部專案都生效）
   - 或專案內的 `CLAUDE.md`（只該專案生效，建議這樣做，避免影響其他無關專案）
4. **（強烈建議）註冊 hook**：見下一節
5. 若這個專案本身是 git 專案，記得把 `<MEMORY_ROOT>` 加進 `.gitignore`——記憶內容是明碼純文字，不該進版本控制

若使用其他 host（Cursor、Cline、Windsurf、Codex…），原理相同：把 `rules-snippet.md` 貼進該 host 的「永遠載入」規則檔即可；各 host 的 hook 機制不同，是否能做到強制擋寫入視 host 而定（詳見下一節）。

---

## Hook：容量上限強制

### 這個 hook 解決什麼問題

`MEMORY.md` 索引檔每次任務都會被完整載入，但 host（例如 Claude Code）只會讀**前 200 行 / 25 KB**，超過的部分會被**默默截斷**——不是報錯，是悄悄不見。也就是說索引一旦寫到超過上限，後面那些記憶就再也不會被讀到，而且你不會收到任何警告。

`hooks/jason_index_guard.py` 是一支 **Claude Code 的 `PreToolUse` hook**，作用是：在 Agent 真的把 `MEMORY.md` 寫超過上限**之前**，直接擋下這次寫入。

### 什麼時機會觸發

- **監聽對象**：檔名為 `MEMORY.md` 的檔案（可用環境變數改成其他名字或指定絕對路徑，見下方設定）
- **監聽動作**：`Edit`、`Write`、`MultiEdit` 這三種工具呼叫（PreToolUse，也就是**動作執行前**攔截）
- **不會攔到的情況**：透過 Bash/PowerShell 之類的 shell 指令去寫檔、或透過 MCP 檔案工具、外部編輯器、雲端同步軟體去改這個檔案——這些都繞過了 hook，這是 Claude Code hook 機制的天生限制，不是這支腳本的 bug

### 觸發後的三種結果

hook 會**模擬**這次編輯套用後的結果，同時檢查「行數」跟「位元組數」兩個維度（超過任一個就算超標），然後：

| 情況 | 結果 |
|---|---|
| 套用後**超過硬上限**，而且這次編輯讓它**變得更超標**（成長） | **`deny`**：直接擋下這次寫入，不會真的寫進去 |
| 套用後還是超過硬上限，但這次編輯是在**縮小**（正在整理中） | 允許通過，但附上提醒：繼續壓縮 |
| 套用後超過**軟性警告線**（150 行 / 20 KB），但還沒到硬上限 | 允許通過，附上提醒：該考慮整理了 |
| 都沒超標 | 靜默放行，不打擾 |

重點：**縮小/整理性質的編輯永遠會被放行**，就算當下還是超標——這樣才能一步一步把索引壓下來（例如 210 行 → 205 行 → 198 行），而不是被卡死改不動。

### 如何註冊

把 [`hooks/settings.snippet.json`](hooks/settings.snippet.json) 的內容合併進你的 Claude Code 設定檔：

- 使用者層級：`%USERPROFILE%/.claude/settings.json`（Windows）或 `~/.claude/settings.json`（macOS/Linux），對所有專案生效
- 或專案層級：`<你的專案>/.claude/settings.json`，只對該專案生效（建議）

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python",
            "args": ["/絕對路徑/到/jason-memory/hooks/jason_index_guard.py"]
          }
        ]
      }
    ]
  }
}
```

**三個要注意的地方：**

1. **`args` 裡的路徑必須是絕對路徑**——這是 Claude Code hook 的 EXEC 執行形式（不經過 shell），路徑會被原樣傳遞，不用處理引號跳脫，但也代表不能用 `~` 或相對路徑
2. **`command` 依系統而定**：Windows 通常是 `"python"`；macOS/Linux 通常只有 `python3`（沒有裸的 `python`）；最保險的做法是用 `python -c "import sys; print(sys.executable)"` 印出直譯器的絕對路徑，直接填那個
3. **裝完一定要驗證**——如果 `command` 指到一個實際上叫不動的直譯器（例如 Windows 上裝了 `python3` 這個名字），Claude Code 會靜默地 fail-open（hook 出錯 = 直接放行），你會完全不知道 hook 根本沒在運作。驗證方法見下方

### 如何驗證 hook 真的有效

裝完之後，故意讓 Agent（或你自己手動）對一份測試用的 `MEMORY.md` 寫入超過 200 行的內容，確認會被 `deny`。最快的方式是直接用 Python 模擬一次 hook 呼叫：

```bash
python -c "
import json
content = chr(10).join(f'line {i}' for i in range(250))
payload = {'tool_name': 'Write', 'tool_input': {'file_path': '/絕對路徑/到/你的/MEMORY.md', 'content': content}}
print(json.dumps(payload))
" | python /絕對路徑/到/jason-memory/hooks/jason_index_guard.py
```

預期輸出應該包含 `"permissionDecision": "deny"`。如果沒有任何輸出（代表靜默放行），代表 hook 沒被正確觸發，回頭檢查 `command` 那個直譯器是否真的能被叫動。

---

## 健檢（Health Check）

有兩層工具，用途不同：

### 1. `jason_check.py` —— 快速檢查單一索引檔的大小

給人或給 CI/腳本用的輕量檢查，**不檢查內容格式**，只看大小有沒有超標：

```bash
python tools/jason_check.py <MEMORY_ROOT>/MEMORY.md
```

**什麼時候該跑：**
- 沒裝上面那個 hook 的 host（Claude Code 以外的環境）上，每次寫完索引後手動跑一次
- 就算裝了 hook，透過 Bash/MCP 等 hook 看不到的管道寫完索引後，也該跑一次補檢查
- 準備做「compact 前的整理」時，先跑一次確認目前狀態

**結果怎麼看（exit code）：**

| Exit code | 意義 |
|---|---|
| `0` | OK，在容量範圍內 |
| `1` | WARN，超過軟性警告線（150 行 / 20 KB），該規劃整理了 |
| `2` | OVER，超過硬上限（200 行 / 25 KB），**必須立刻整理**，不然後面的記憶已經在被截斷 |
| `64` | 用法錯誤（沒給路徑） |
| `66` | 讀不到那個檔案 |

### 2. `jason_doctor.py` —— 完整健檢，檢查整個記憶庫的一致性

不只看大小，還會檢查：索引裡的連結是否指向真實存在的筆記檔（有沒有「斷掉的指標」）、有沒有筆記檔存在但索引沒連結到它（孤兒檔案）、每個筆記檔的 YAML frontmatter 格式是否正確（`name`/`description`/`type`/`created`/`updated` 齊不齊全、`type` 是不是四種合法值之一）、wikilink 格式是否正確。

```bash
# 完整檢查（含格式規範）
python tools/jason_doctor.py <MEMORY_ROOT>

# 只檢查結構（連結完整性、孤兒檔案），跳過格式規範
python tools/jason_doctor.py <MEMORY_ROOT> --no-schema
```

**什麼時候該跑：**
- **定期健檢**：建議每隔一段時間（例如每週，或每次大整理前）跑一次，當作記憶庫的例行體檢
- **接手一個既有的記憶庫時**：第一次連上一個不是用 Jason-memory 規範建的舊資料夾，先用 `--no-schema` 只看結構問題，不要一次被一堆格式警告淹沒；等結構乾淨了再跑完整版慢慢補 frontmatter
- **手動改過筆記檔之後**：人工編輯容易漏改索引連結或打錯 frontmatter 欄位，改完跑一次確認沒弄壞

輸出範例：

```
jason-doctor: clean — index 29 lines / 1.3 KB, 0 note(s), no broken pointers, orphans, or schema errors.
```

若有問題會列出每一項（斷掉的連結指到哪個不存在的檔案、哪個筆記缺了哪個欄位），照著訊息修就好。

---

## 檔案結構

```
jason-memory/                  ← 這個框架本身（放進版控、可以整包複製到別的專案）
├── README.md                  ← 這份文件
├── LICENSE                    ← MIT
├── rules-snippet.md           ← 貼進 CLAUDE.md 的規則片段
├── SKILL.md                   ← 完整協議規格（給 Agent 當詳細參考）
├── hooks/
│   ├── jason_index_guard.py   ← PreToolUse 容量上限 hook
│   └── settings.snippet.json  ← hook 註冊範例
├── tools/
│   ├── jason_check.py         ← 快速大小檢查
│   └── jason_doctor.py        ← 完整健檢
└── templates/
    ├── MEMORY.md              ← 起始索引檔範本
    ├── example-feedback.md    ← feedback 筆記範例
    └── example-project.md     ← project 筆記範例

<MEMORY_ROOT>/                 ← 實際的記憶庫（跟框架本身分開，通常不進版控）
├── MEMORY.md
└── *.md                       ← 各則筆記，平放
```

---

## 設定選項

透過環境變數調整（hook 跟 `jason_check.py` 共用同一組，保持兩層一致）：

| 變數 | 預設值 | 意義 |
|---|---|---|
| `JASON_WARN` | 150 | 軟性警告的行數上限 |
| `JASON_HARD` | 200 | 硬性上限的行數 |
| `JASON_WARN_BYTES` | 20480（20 KB） | 軟性警告的位元組上限 |
| `JASON_HARD_BYTES` | 25600（25 KB） | 硬性上限的位元組數 |
| `JASON_INDEX_NAME` | `MEMORY.md` | hook 監聽的檔名 |
| `JASON_INDEX_PATH` | （無） | 指定要監聽的索引絕對路徑，蓋過檔名比對，適合同一台機器上有多個 `MEMORY.md` 時用 |

行數與位元組數兩個上限**任一個先超標就算超標**，因為有些筆記行很長，可能行數還沒破百但位元組數已經爆了。

---

## 安全與隱私

記憶庫是**明碼、未加密的純文字**，任何本機程式都讀得到。`.gitignore` 只是不進版控，**不是加密**，對 Dropbox/iCloud/OneDrive 之類的雲端同步、系統備份、桌面搜尋索引都沒有防護力。

- **絕對不要把密碼、金鑰、token、cookie、恢復碼的「值」寫進記憶**，只記「這個密碼放在哪」（例如「在密碼管理器 / 環境變數 `FOO` 裡」）
- 個資（電話、email、地址）盡量存指標，不存明碼本身
- 這條紀律**沒有 hook 強制**（沒有掃描筆記內容的機制），純粹靠規則自律，請自行留意

---

## 已知限制

Jason-memory 是**單一專案、單一寫入者、個人規模**的工具，目前沒有：

- **版本控制/遷移機制**：frontmatter 格式若改版，沒有自動升級路徑
- **來源可信度標記**：沒有 `source`/`confidence`/`last_verified` 欄位，記憶內容是「建議參考」而非「已驗證事實」
- **多專案/多範圍支援**：沒有 `scope`/`project_id`，同一個記憶庫混用在不同專案會有 slug 撞名風險
- **並發控制**：假設單一序列化寫入者，沒有檔案鎖
- **大規模擴充性**：索引上限決定了「常駐可讀」的記憶量（約 200 行指標），適合個人策展規模；量大的知識庫請用向量檢索類工具（如 basic-memory、mem0）

---

## 授權與致謝

MIT License，見 [LICENSE](LICENSE)。

Jason-memory 改寫自 [tinqiao-oss/engramory](https://github.com/tinqiao-oss/engramory)（同為 MIT 授權），沿用其「Markdown 檔案 + 常駐索引 + 策展紀律」的核心設計；Jason-memory 的差異主要在於把 `feedback` 類型收窄為「踩坑教訓 + 驗證有效的做法」，並明確要求 Agent 自我判斷、不依賴使用者主動要求記憶。
