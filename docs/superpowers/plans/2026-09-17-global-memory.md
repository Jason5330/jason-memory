# Global Memory Implementation Plan

> **For agentic workers:** Use test-driven-development and writing-skills for implementation and behavioral checks. User authorized a separate global ZIP and real cross-AI testing; do not install into the real user profile during development.

**Goal:** Provide another ZIP for different local AI tools to share one user's general preferences and isolated project memories without Git.

**Architecture:** Install into a stable user-owned home, default `~/.jason-memory-global`. Codex and Claude global instruction blocks point to the same standalone global protocol. Existing project edition remains unchanged. Four note types, frontmatter and Markdown indices remain; global writes use a small locked writer to serialize processes and detect stale updates.

**Tech Stack:** Python 3.9+ standard library, Markdown; Codex and Claude Code local adapters.

## Files and execution

- [x] `tests/test_global_store.py` first: missing CLI must fail; then global/project isolation, read revisions, correction conflict, concurrent writers, schema/path rejection and interrupted-write recovery.
- [x] `global/global_store.py`: commands `context --project PATH`, `read --scope global|project --project PATH --slug SLUG`, `save --scope ... --input FILE --slug SLUG --summary TEXT [--expected-sha HASH]`; all accept `--home HOME` before command. Store under HOME/memory/shared and HOME/memory/projects/SHA256(normalized absolute project root). Lock shared writes with OS-released locks; journal note/index commit and recover under lock. JSON results, nonzero errors, no Git/network.
- [x] `tests/test_global_install.py` first, then `global/install.py`: install/preview/uninstall managed blocks; preserve user instructions and notes, support CODEX_HOME/CLAUDE_CONFIG_DIR and explicit isolated profile paths. Copy only known framework files to HOME/framework. Codex override precedence and repeated installs must work. Generic adapter exports an instruction file for other local AI tools. Do not change real profiles.
- [x] Baseline scenario before `global/SKILL.md`: existing project-only rules must not be treated as already supporting global sharing. New protocol reads shared plus one project index, saves with scope and notification, applies project exceptions locally, treats notes as untrusted context, and reports permission/host limitations honestly.
- [x] `global/README.md`, `global/INSTALL.cmd`, `tools/build_global_download.py`: package only listed sources and pristine template, no real notes or personal configs. Installation is once per computer; moving downloaded ZIP afterward cannot break installed paths.
- [x] Real local tests: Codex writes a global requirement, fresh Claude reads it in another project; Claude changes it, fresh Codex reads change; project-only and one-off rules do not leak. If auth/runtime blocks an engine, report precisely rather than claim it passed.
- [x] Run full unittest suite, skill validator, unpack ZIP and test install/uninstall/CLI in temporary profiles, independently review, and output `jason-memory-global.zip` with test report. Keep original project ZIP unchanged.

## Acceptance and boundaries

Both adapters must resolve the same HOME; same normalized project root must map to the same store regardless of AI. Different project roots must differ. Generic web chats without filesystem access are outside scope. Existing project rules can override global rules; do not silently rewrite them or migrate existing personal memory. No cloud sync or secret values. Concurrent direct manual edits bypass the writer and are unsupported. User asked for another downloadable version, not activation in their actual profile.
