---
name: verify-before-done
description: 回報完成前要先 grep 確認改動真的生效
type: feedback
created: 2026-06-13
updated: 2026-06-13
---

在跟使用者說「這個改動完成了」之前，先做一次快速搜尋，確認改動真的落在預期的地方。

**Why:** 使用者之前被「回報完成，但改動其實默默失敗沒套用」坑過，沒驗證過就說
「完成了」會讓使用者失去信任。

**How to apply:** 每次編輯之後，grep 改動的符號/字串，把對到的那幾行貼在回報裡；
或是跑相關測試，把結果貼出來。

相關：[[code-change-hygiene]]
