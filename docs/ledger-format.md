# 原始帳本格式（jason-ledger v1）

帳本是記憶的唯一事實來源，與宿主（Claude Code、Codex 或其他）無關。任何程式只要會讀
JSON 與算 SHA-256，就能重播並驗證它；不需要本專案的程式碼。

## 位置

```text
<memory_root>/.raw/ledger.jsonl      每行一筆紀錄，UTF-8，只追加
<memory_root>/.raw/blobs/<sha256>    文件逐位元組鏡像，檔名即內容的 SHA-256
<memory_root>/.raw/dream.json        背景整理游標（衍生狀態，可刪除後從 0 重建）
```

## 紀錄欄位

| 欄位 | 型別 | 說明 |
|---|---|---|
| `seq` | 整數 | 從 1 起連續遞增 |
| `ts_ns` | 整數 | 寫入時的 Unix 奈秒時間 |
| `session` | 字串 | 宿主的會話識別（Claude session_id、Codex thread-id），可為空 |
| `kind` | 字串 | `prompt`、`assistant`、`tool`、`document`、`correction` 或其他自訂類型 |
| `data` | 任意 JSON | 事件內容；文字欄位超過 64 KB 會截斷並註明原長度，憑證值以 `[REDACTED]` 遮蔽 |
| `prev` | 字串 | 上一筆的 `hash`；第一筆為 64 個 `0` |
| `hash` | 字串 | 本筆除 `hash` 外所有欄位的 SHA-256 |

`hash` 的計算：取紀錄去掉 `hash` 欄位，以 `json.dumps(body, sort_keys=True, ensure_ascii=False)`
序列化（鍵排序、`, ` 與 `: ` 分隔、非 ASCII 原樣輸出），UTF-8 編碼後取 SHA-256 十六進位小寫。

## 驗證規則

依序讀每一行：`seq` 等於行號、`prev` 等於前一筆 `hash`、重算的 `hash` 與欄位相符。
任何修改、刪除、插入或重排都會在第一筆不符處失敗。
如果最後一筆 JSON 完整、只缺行尾換行，下次追加會補上分隔字元，保留原紀錄與雜湊。

`document` 紀錄的 `data` 為 `{"path", "sha256", "size"}`；`blobs/<sha256>` 的內容重算
SHA-256 必須等於檔名，大小必須等於 `size`。`verify` 與 `source --ref` 都會檢查，鏡像遺失或損壞即失敗（#39）。

寫入前會遮蔽字串裡看起來像憑證的值（#38）。`password`、`token`、`api_key`、`secret`、`cookie`、
`authorization` 等憑證欄位中的非空字串、數字與布林值一律改為 `[REDACTED]`，包含巢狀物件或陣列中的值。
`token_count`、`tokenizer` 等一般計量欄位保留（#43）。
純文字中也會遮蔽：`Authorization`／`Proxy-Authorization` 標頭與 `Bearer` 值、`api_key=`、`GITHUB_TOKEN=`、
`password:` 等「憑證名稱＋`=`或`:`」後的值、`--password` 類參數、`Cookie`／`Set-Cookie` 的值、網址中的密碼
（`https://user:密碼@host`）與整段私鑰（#50）。值若用引號包住，整段引號內容都遮蔽（含空格與跳脫引號，#54）。
環境變數引用（`${TOKEN}`）保留。

雜湊鏈或已整理游標的 checkpoint 驗證不通過時（含截短、重新計算雜湊的改寫，#53），`append`、`mirror` 一律拒絕寫入，帳本保持原狀供診斷；Claude hook 只顯示警告、不阻擋對話（#51）。

## 各 kind 的 data

| kind | data |
|---|---|
| `prompt` | `{"text": 使用者訊息, "host"?: "codex"}` |
| `assistant` | `{"text": 最終回覆, "stop_hook_active"?: bool, "host"?: "codex"}` |
| `tool` | `{"tool": 名稱, "input": 參數, "result": 輸出}`（Claude hook 記錄 Shell 成功及失敗結果，失敗輸出含 `error` 與 `is_interrupt`） |
| `document` | `{"path": 原始路徑, "sha256": 指紋, "size": 位元組數}` |

## 來源編號

筆記引用原始紀錄時寫成 `識別碼:seq:雜湊前12碼`，例如 `3d9d6dc09bcfaf35:7:ab12cd34ef56`。
識別碼在專案版為 `local`，在全局版為 `memory/projects/` 下該專案資料夾名稱的前 16 碼。
雜湊前綴讓引用在帳本被改寫時失效，不會悄悄指向不同的內容。

## 匯出與匯入

帳本本身就是交換格式：轉給其他工具時直接複製 `ledger.jsonl` 與 `blobs/`。
匯入其他宿主的歷史時，依時間順序以 `memory_ledger.py append` 追加，雜湊鏈會自動延續。
專案內的獨立讀取器測試見 `tests/test_ledger.py` 的 `test_independent_reader_verifies_ledger_format`。
