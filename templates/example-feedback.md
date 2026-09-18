---
name: verify-before-done
description: 回報完成前確認改動已生效並驗證相關行為
type: feedback
created: 2026-06-13
updated: 2026-06-13
---

在跟使用者說「這個改動完成了」之前，先做一次快速搜尋，確認改動真的落在預期的地方。

**Why:** 使用者之前被「回報完成，但改動其實默默失敗沒套用」坑過，沒驗證過就說
「完成了」會讓使用者失去信任。

**How to apply:** 修改檔案後確認預期內容已生效；行為有改動時執行相關測試。
對使用者簡短說明實際結果即可，不必貼出每次搜尋輸出，也不延伸成 Git 提交提醒。

相關：[[code-change-hygiene]]

```jason-facts
[{"subject":"agent:workflow","predicate":"completion.verification","value":"回報完成前驗證改動及相關行為"}]
```
