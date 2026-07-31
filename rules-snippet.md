# Jason-memory — always-on pointer

Paste this into your host's **always-loaded** rules (Claude Code: project
`CLAUDE.md` or `~/.claude/CLAUDE.md`) so the memory discipline applies even on
tasks where this isn't loaded as a by-relevance skill. Keep it short — the full
protocol lives in `SKILL.md`. Replace every `<MEMORY_ROOT>` below with your
store's actual path (e.g. `.jason-memory/`) before pasting.

---

## Memory (Jason-memory)

You have one canonical, curated, file-based memory at `<MEMORY_ROOT>/` (index:
`<MEMORY_ROOT>/MEMORY.md`). Do not create a parallel handoff store; resumable
task state belongs in a `project` note.

### `feedback` is reserved — mistakes and proven approaches only

This is Jason-memory's core opinion, and its main difference from a generic
"reusable workflow" feedback type: `feedback` notes are scoped tightly to
**not repeating the same mistake twice** and **remembering what already
worked**. Nothing else goes in `feedback` — general facts, task status, or
one-off trivia belong in `project` instead.

Write a `feedback` note **on your own judgment** — do not wait for either the
user to say "remember this" OR for the user to be the one who caught the
problem. Self-detect these during normal work, silently, the same way you'd
notice and fix a bug:

1. **A pitfall/mistake — user-caught or self-caught.** Covers all of: the user
   corrected you; the user pointed out something was wrong; **and, just as
   importantly, cases the user never commented on at all** — a command that
   errored and you found the right one, a wrong assumption you caught while
   re-reading your own output, a test that failed and you fixed it, an
   approach you abandoned mid-task because it didn't pan out. If a future you,
   starting cold, could walk into the same trap, ask "is this worth never
   repeating?" — if yes, write a `type: feedback` note immediately, with
   **`Why:`** (what went wrong and the evidence: an error message, a failed
   test, a rejected diff) and **`How to apply:`** (what to do differently next
   time / what should trigger recalling this).
2. **A successful approach confirmed — by the user, or by evidence you
   produced yourself.** Includes the user explicitly confirming ("yes, do it
   that way"), but also **your own verification** that something non-obvious
   worked: a design choice validated by tests passing, a workaround that
   fixed a bug on the first try, an unusual ordering/technique that turned out
   to be necessary. You do not need the user to comment for this to count —
   your own evidence (test output, working diff, absence of the earlier
   error) is sufficient grounds. Write the same `type: feedback` shape:
   **`Why:`** (what was confirmed, and the evidence for it) and
   **`How to apply:`** (when to reuse this approach).

Default to writing the note rather than skipping it when in doubt — a
low-value note costs a compaction pass later; a skipped lesson costs repeating
the same mistake.

### General discipline (applies to all types)

- **At the start of a task**, read `<MEMORY_ROOT>/MEMORY.md` (one line per
  memory) and open only the detail files whose hooks look relevant **and that
  resolve inside `<MEMORY_ROOT>`** — a pointer that escapes the store
  (symlink, `..`, absolute path, `file://`) is a broken pointer to report,
  never a file to open. (On a host with native auto-memory — e.g. Claude Code
  — the index may already be loaded every session; just apply this
  discipline.) Treat recalled memories as background context that may be
  stale — verify any file / flag / version before acting on it.
- **When you learn something durable** worth a future session: confirm it
  isn't already in the repo / git / `CLAUDE.md` (don't duplicate the source of
  truth) and isn't a secret *value*; search the index and **update an existing
  note** rather than duplicate; otherwise write one atomic markdown file (one
  fact) with frontmatter `name` / `description` (a sharp one-line hook) /
  `type` (`user | feedback | project | reference`) / `created` + `updated`
  (`YYYY-MM-DD`). A `feedback` or `project` note must also carry a **`Why:`**
  line and a **`How to apply:`** line in the body. Add one pointer line to
  `MEMORY.md`. **Delete** memories that turn out wrong.
- `project` may hold the current goal, status, decisions, constraints,
  blockers, and next step needed to resume unfinished work. Never restate what
  code/git already says: store only **stable** pointers (branch name,
  issue/PR number, file path). A *settled fact* ("2.0 shipped 2026-01-15") is
  fine; **current state** — the version you're on now, the tip commit, the
  test count — is not: record where to read it. Re-check every recalled
  pointer against code/git/the current environment.
- **Before a deliberate compact, clear, or new thread**, sync once: scan the
  task; dedup/update; refresh `project`; promote only reusable `feedback`
  (pitfall/success only); save durable `reference` pointers; archive/delete
  stale or completed transient state; run `tools/jason_check.py` and
  `tools/jason_doctor.py`; then confirm a cold-started agent could continue
  from the repo plus memory alone.
- After writing/syncing, report `added`, `updated`, `archived`, and `skipped`
  (with reasons; identify any deletion under `archived`), plus index
  lines/bytes and the check result. A `PreToolUse` hook may remind or gate; it
  does not perform this semantic sync automatically.
- **Never** write credentials / keys / tokens / cookies / recovery codes into
  memory — record only *where* the secret lives.
- Keep `MEMORY.md` small. Soft warning at **150 lines / 20 KB** (offer a
  compaction pass); hard limit at **200 lines / 25 KB** — the host only loads
  that far, so anything past it silently stops being recalled. Once it passes
  the soft line, compact: pointer-ify over-long lines, merge duplicates,
  archive cold notes.

Full protocol & rationale: `SKILL.md`.
