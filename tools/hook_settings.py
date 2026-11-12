"""Merge only owned Claude hook commands, preserving unrelated configuration."""
import copy
import json

EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop')
MARKER = '--jason-hook'


def merged(original, command=None):
    data = json.loads(original.decode('utf-8-sig')) if original.strip() else {}
    if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
        raise ValueError('Claude settings/hooks must be JSON objects')
    data = copy.deepcopy(data)
    hooks = data.setdefault('hooks', {})
    for name in EVENTS:
        rows = hooks.get(name, [])
        if not isinstance(rows, list):
            raise ValueError('Claude event hooks must be arrays')
        retained = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('hooks'), list):
                raise ValueError('Invalid Claude hook group')
            group = dict(row)
            group['hooks'] = [h for h in row['hooks'] if not (isinstance(h, dict) and
                              MARKER in h.get('command', '') and 'claude_memory_hook.py' in h.get('command', ''))]
            if group['hooks']:
                retained.append(group)
        if command:
            group = {'hooks': [{'type': 'command', 'command': command, 'timeout': 15}]}
            if name == 'PreToolUse':
                group['matcher'] = 'Read|Write|Edit|MultiEdit'
            elif name == 'PostToolUse':
                group['matcher'] = 'Bash|PowerShell|powershell'
            retained.append(group)
        if retained:
            hooks[name] = retained
        else:
            hooks.pop(name, None)
    if not hooks:
        data.pop('hooks', None)
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


# --- Codex notify (shared by the project and global installers) -------------
import re as _re
from pathlib import Path as _Path

# Same single-line form that tools/purge_memory.py removes.
NOTIFY_LINE = _re.compile(rb'^[ \t]*notify[ \t]*=[ \t]*\[[^\r\n]*codex_notify\.py[^\r\n]*\][ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)', _re.M)
ANY_NOTIFY = _re.compile(rb'^[ \t]*notify[ \t]*=', _re.M)


def merged_notify(original, python, script, uninstall=False):
    """Return (updated bytes, status). Adds a top-level `notify` before any TOML table and never
    replaces another notify program."""
    bom = b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b''
    body = NOTIFY_LINE.sub(b'', original[len(bom):])
    stripped = bom + body
    if uninstall:
        return stripped, 'removed' if stripped != original else 'absent'
    if ANY_NOTIFY.search(body):
        return original, 'skipped: another Codex notify program is configured; add codex_notify.py manually'
    line = ('notify = ' + json.dumps([_Path(python).as_posix(), _Path(script).as_posix()],
                                     ensure_ascii=False) + '\n').encode('utf-8')
    updated = bom + line + body
    return updated, 'installed' if updated != original else 'unchanged'
