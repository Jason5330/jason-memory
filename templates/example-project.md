---
name: api-gateway-v2-status
description: API gateway v2 遷移已上線 —— 2.0 版
type: project
created: 2026-01-15
updated: 2026-01-15
---

API gateway v2 遷移已完成最終上線，於 2026-01-15 以 2.0 版釋出。原本 2.0
路線圖裡的項目（request 層級的 rate limiting）延後到 2.1 版才做。

**Why:** 延後是刻意的範圍決策，但 code 跟 git 紀錄都沒有記下「為什麼 2.0
沒有 rate limiting」——沒有這則筆記的話，之後會有人重新爭論這件事，或把它
當成 bug 回報。

**How to apply:** 之後有人提到 rate limiting，要知道這是刻意延後的，不是被
忘記。要知道現在該對準哪個版本，去問專案自己的版本工具——這則筆記刻意不記
版本號，因為那是會過時的「目前狀態」，寫在這裡會腐爛（見 SKILL.md §2）。

相關：[[deploy-runbook]] · [[release-versioning]]
