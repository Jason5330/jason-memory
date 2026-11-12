#!/usr/bin/env python3
"""Background consolidation ("dreaming"): turn new raw ledger records into Markdown notes.

The foreground agent never writes memory in background mode. This worker runs detached after
enough new ledger records accumulate, hands the unconsolidated records plus the current index to
an agent CLI (Claude Code or Codex, auto-detected, or a configured command) and lets that agent
save reusable requirements through the runtime with JASON_DREAM=1. Each saved fact cites the
ledger seq numbers it came from, so notes stay traceable and can be rebuilt from the raw layer.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import memory_ledger as ledger
import memory_runtime as runtime

RECORD_CAP = 4000      # Characters of one record shown to the consolidating agent.
CHUNK_CAP = 120000     # Characters of records per agent call; larger ranges are split.
AGENTS = {
    'claude': ['claude', '-p', '--allowedTools', 'Bash(python:*)', 'Bash(py:*)', 'Bash(python3:*)', 'Read'],
    'codex': ['codex', 'exec', '--full-auto', '-'],
}

INSTRUCTIONS = """你是 Jason-memory 的背景整理程序（做夢）。前台 Agent 不寫記憶；你只根據下面的原始紀錄整理。

任務：從這段原始紀錄中找出「之後的對話仍應遵守或會用到」的內容並保存：
- 使用者明確的偏好、做法要求、更正（以最新一次為準）→ --type feedback
- 使用者身份、職務、職責 → --type user
- 本專案的限制、現況、決策 → --type project
- 外部資源位置 → --type reference
不保存：一次性要求（例如「這次只回一句話」「只改這個檔案」）、翻譯或引用的文字、
未決定的猜測、使用者說不要記的內容、任何憑證值（token、密碼、API key；只能記環境變數名稱或保管位置）。

做法：
1. 先看「現有索引」，需要時用 read 讀相關筆記；同一事實沿用既有 subject/key。
2. 新事實用一次 remember；--why 必須包含來源，例如「來源：ledger seq 12、15」。
3. 若與既有值不同（使用者更正），用 context 取得 revision，再用 apply --plan - 一次更正所有相關筆記，
   保留其他事實，筆記正文寫明來源 seq。
4. 沒有值得保存的內容就什麼都不寫。不要修改原始帳本，不要寫入記憶庫以外的檔案。
完成後輸出一行摘要（保存／更正／略過了什麼），最後一行必須單獨輸出：
JASON_DREAM_STATUS: ok      （所有該保存的都已成功保存，或確實沒有需要保存的內容）
JASON_DREAM_STATUS: failed  （任何指令被拒絕、需要核准、出錯或未執行）
"""
STATUS_OK = 'JASON_DREAM_STATUS: ok'


def runtime_prefix(config, project, cfg):
    parts = [sys.executable, str(Path(runtime.__file__).resolve()), '--config', str(config)]
    if cfg.get('mode') == 'global':
        parts += ['--project', str(project)]
    return ' '.join(shlex.quote(Path(p).as_posix() if i < 2 else p) for i, p in enumerate(parts))


def render_record(record):
    data = record.get('data')
    if record.get('kind') == 'prompt':
        text = '使用者：' + str((data or {}).get('text', ''))
    elif record.get('kind') == 'assistant':
        text = '助理：' + str((data or {}).get('text', ''))
    elif record.get('kind') == 'tool':
        text = '工具 ' + str((data or {}).get('tool', '')) + '：' + json.dumps((data or {}).get('input'), ensure_ascii=False)
    else:
        text = record.get('kind', '') + '：' + json.dumps(data, ensure_ascii=False)
    if len(text) > RECORD_CAP:
        text = text[:RECORD_CAP] + '…（截斷，完整內容見 ledger seq ' + str(record['seq']) + '）'
    return '[seq ' + str(record['seq']) + ' session ' + str(record.get('session', ''))[:8] + '] ' + text


def chunks(records):
    current, size = [], 0
    for record in records:
        line = render_record(record)
        if current and size + len(line) > CHUNK_CAP:
            yield current
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        yield current


GLOBAL_SCOPE_HINT = ('\n全局版範圍：使用者對 AI 的一般偏好與身份（回報格式、語言、時間格式、稱呼等，'
                     '沒有限定某個專案或檔案）用 --scope global，所有資料夾都會套用；只針對目前這個專案的'
                     '需求、限制與現況用 --scope project。--type project 只能搭配 --scope project。'
                     'read 時也要指定 --scope。\n')


def build_prompt(lines, index_text, prefix, global_mode=False):
    return (INSTRUCTIONS + (GLOBAL_SCOPE_HINT if global_mode else '') + '\n指令前綴（Bash）：' + prefix +
            '\n例：' + prefix + ' remember --scope project --type feedback --subject user --key KEY '
            '--value "完整要求" --why "使用者要求；來源：ledger seq N"\n'
            + prefix + ' read --note 相對路徑.md\n\n## 現有索引\n\n' + index_text +
            '\n\n## 原始紀錄（未整理）\n\n' + '\n'.join(lines) + '\n')


def _claude_command(base):
    # Allow exactly this interpreter so runtime calls are not stopped by a permission prompt.
    exe = Path(sys.executable)
    extra = ['Bash(' + p + ':*)' for p in sorted({exe.as_posix(), str(exe)})]
    return base[:3] + extra + base[3:]


def agent_command(opts):
    agent = opts.get('dream_agent', 'auto')
    if isinstance(agent, list):
        return agent
    names = ['claude', 'codex'] if agent == 'auto' else [agent]
    for name in names:
        if name in AGENTS and shutil.which(AGENTS[name][0]):
            command = list(AGENTS[name])
            command[0] = shutil.which(command[0])
            return _claude_command(command) if name == 'claude' else command
    return None


def consolidate(config, project, start, upto, opts, runner=None, target=None):
    """Feed records start..upto to the agent. Non-zero means retry later; the cursor stays.
    The ledger is verified first; a broken or truncated source is never handed to a model.
    `target` is the memory config the agent writes to (defaults to `config`)."""
    with runtime.opened(config, project) as (cfg, project, roots):
        root = roots['project']
        try:
            existing = ledger.verified_records(root)
        except ValueError as exc:
            print('Jason-memory dream: ledger verification failed: ' + str(exc), file=sys.stderr)
            return 65
        records = [r for r in existing if start <= r['seq'] <= upto
                   and r.get('kind') in ('prompt', 'assistant', 'tool', 'document', 'correction')]
    target = Path(target or config)
    with runtime.opened(target, project) as (tcfg, _, troots):
        index_text = '\n\n'.join(scope + ':\n' + runtime.read_index(r) for scope, r in troots.items())
        prefix = runtime_prefix(target, project, tcfg)
    if not records:
        return 0
    command = agent_command(opts) if runner is None else ['injected-runner']
    if command is None:
        print('Jason-memory dream: no agent CLI found (claude/codex); set ledger.dream_agent', file=sys.stderr)
        return 127
    env = dict(os.environ, JASON_DREAM='1')
    run = runner or (lambda cmd, text: _run_agent(cmd, text, env, project))
    for lines in chunks(records):
        try:
            code = run(command, build_prompt(lines, index_text, prefix, tcfg.get('mode') == 'global'))
        except (OSError, subprocess.SubprocessError) as exc:
            print('Jason-memory dream: agent failed to run: ' + type(exc).__name__, file=sys.stderr)
            return 127 if isinstance(exc, OSError) else 124
        if code:
            return code
    return 0


def _run_agent(command, text, env, project):
    """Run the agent; success needs exit 0 AND its explicit ok status line (else retry later)."""
    result = subprocess.run(command, input=text.encode('utf-8'), env=env, cwd=str(project),
                            stdout=subprocess.PIPE, timeout=1800)
    output = result.stdout.decode('utf-8', 'replace')
    sys.stdout.write(output)
    if result.returncode:
        return result.returncode
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    return 0 if lines and lines[-1] == STATUS_OK else 75


def rebuild(config, project, opts, runner=None):
    """Re-derive all notes from the whole ledger in a staging store, then swap them in.
    The current notes stay untouched unless the rebuild succeeds; old notes are archived on swap.
    Holds the store's single consolidation slot, so it never overlaps a dream worker."""
    result = {'rebuilt': False, 'archived': [], 'records': 0}
    if runner is None and agent_command(opts) is None:
        return dict(result, exit_code=127, error='no agent CLI found (claude/codex); current notes kept')
    with runtime.opened(config, project) as (_, project, roots):
        root = roots['project']
        try:
            head = len(ledger.verified_records(root))
        except ValueError as exc:
            return dict(result, exit_code=65, error='ledger verification failed: ' + str(exc))
        owner = ledger.claim_job(root, 'rebuild', head)
        if owner is None:
            return dict(result, exit_code=75, error='another consolidation job is running; retry later')
        started_revision = runtime.revision(root)
        staging = runtime.storelib.safe(ledger.raw_root(root) / ('rebuild-' + owner), root)
        staging.mkdir()
        staging_config = staging / '.jason-memory.json'
        staging_config.write_text('{"version": 1, "mode": "project", "memory_root": "store"}', encoding='utf-8')
    result['records'] = head
    previous = os.environ.get('JASON_DREAM')
    os.environ['JASON_DREAM'] = '1'
    error = None
    try:
        code = consolidate(config, project, 1, head, opts, runner, target=staging_config)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        code, error = 1, str(exc)
    finally:
        if previous is None:
            os.environ.pop('JASON_DREAM', None)
        else:
            os.environ['JASON_DREAM'] = previous
    try:
        with runtime.opened(config, project) as (_, _, roots):
            if code == 0 and (ledger.dream_state(root).get('running') or {}).get('owner') != owner:
                # Our reservation expired and another job took over: swapping now could clobber it.
                code, error = 75, 'rebuild reservation lost to another job; current notes kept'
            if code == 0 and runtime.revision(root) != started_revision:
                # Notes changed while rebuilding (e.g. foreground_writes=true): swapping would undo them.
                code, error = 75, 'memory store changed during rebuild; retry'
            if code == 0:
                try:
                    rebuilt = runtime.notes(staging / 'store')
                    current = runtime.notes(root)
                    retire = sorted(n for n in current if n not in rebuilt)
                    updates = {n: c for n, c in rebuilt.items() if current.get(n) != c}
                    if updates or retire:
                        runtime.apply(root, {'expected_revision': runtime.revision(root),
                                             'updates': updates, 'retire': retire})
                    result.update(archived=sorted(set(retire) | {n for n in updates if n in current}))
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    code, error = 1, 'swap failed: ' + str(exc)
            ledger.release_job(root, owner, head, None if code == 0 else
                               {'code': code, 'error': 'rebuild failed; current notes kept' +
                                (': ' + error if error else '')})
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    result.update(exit_code=code, rebuilt=code == 0)
    if code:
        result['error'] = (error or 'agent did not finish') + '; current notes kept unchanged'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--project', type=Path)
    parser.add_argument('command', choices=['consolidate', 'prompt', 'rebuild'])
    parser.add_argument('--yes', action='store_true', help='confirm rebuild (archives current notes)')
    args = parser.parse_args()
    config = args.config.absolute()
    project = Path(os.environ.get('JASON_MEMORY_PROJECT') or args.project or Path.cwd())
    try:
        opts = ledger.settings(config)
        if args.command == 'rebuild':
            if not args.yes:
                raise ValueError('rebuild archives all current notes; rerun with --yes')
            result = rebuild(config, project, opts)
            runtime.emit_json(result)
            return result['exit_code']
        start = int(os.environ.get('JASON_DREAM_FROM', '1'))
        upto = int(os.environ.get('JASON_DREAM_UPTO', '0'))
        if args.command == 'prompt':
            captured = []
            consolidate(config, project, start, upto or 10 ** 9, dict(opts, dream_agent=['print']),
                        lambda cmd, text: captured.append(text) or 0)
            sys.stdout.write('\n'.join(captured))
            return 0
        return consolidate(config, project, start, upto, opts)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        runtime.emit_json({'error': str(exc)}, sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
