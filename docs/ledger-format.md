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

`document` 紀錄的 `data` 為 `{"path", "sha256", "size"}`；`blobs/<sha256>` 的內容重算
SHA-256 必須等於檔名。

## 各 kind 的 data

| kind | data |
|---|---|
| `prompt` | `{"text": 使用者訊息, "host"?: "codex"}` |
| `assistant` | `{"text": 最終回覆, "stop_hook_active"?: bool, "host"?: "codex"}` |
| `tool` | `{"tool": 名稱, "input": 參數, "result": 輸出}`（Claude hook 目前只記錄 Shell 類工具） |
| `document` | `{"path": 原始路徑, "sha256": 指紋, "size": 位元組數}` |

## 匯出與匯入

帳本本身就是交換格式：轉給其他工具時直接複製 `ledger.jsonl` 與 `blobs/`。
匯入其他宿主的歷史時，依時間順序以 `memory_ledger.py append` 追加，雜湊鏈會自動延續。
專案內的獨立讀取器測試見 `tests/test_ledger.py` 的 `test_independent_reader_verifies_ledger_format`。
