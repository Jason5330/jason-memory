"""Merge only owned Claude hook commands, preserving unrelated configuration."""
import copy
import json

TOOL_EVENTS = ('PostToolUse', 'PostToolUseFailure')
EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', *TOOL_EVENTS, 'Stop')
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
            elif name in TOOL_EVENTS:
                group['matcher'] = 'Bash|PowerShell|powershell'
            retained.append(group)
        if retained:
            hooks[name] = retained
        else:
            hooks.pop(name, None)
    if not hooks:
        data.pop('hooks', None)
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


# --- Codex notify (shared by the project/global installers and the purge tool) --
import os as _os
import re as _re
from pathlib import Path as _Path

# Single-line `notify = [...]` arrays that mention codex_notify.py (the only form we ever write).
NOTIFY_LINE = _re.compile(rb'^[ \t]*notify[ \t]*=[ \t]*(\[[^\r\n]*codex_notify\.py[^\r\n]*\])[ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)', _re.M)
ANY_NOTIFY = _re.compile(rb'^[ \t]*notify[ \t]*=', _re.M)


def notify_script(array):
    """The codex_notify.py path named in a single-line TOML array, or None if it cannot be parsed."""
    try:
        items = json.loads(array.decode('utf-8'))
    except ValueError:
        items = [item.decode('utf-8', 'replace') for item in _re.findall(rb"'([^']*)'", array)]
    if not isinstance(items, list):
        return None
    paths = [item for item in items
             if isinstance(item, str) and item.replace('\\', '/').endswith('/codex_notify.py')]
    return _Path(paths[-1]) if paths else None


def _same(a, b):
    return _os.path.normcase(_os.path.abspath(str(a))) == _os.path.normcase(_os.path.abspath(str(b)))


def is_jason_dispatcher(path):
    """A Jason-memory codex_notify.py sits next to the ledger module of the same installation."""
    return bool(path) and _Path(path).is_file() and (_Path(path).parent / 'memory_ledger.py').is_file()


def strip_notify(body, owned):
    """Remove notify lines whose script is exactly one of `owned`. Returns (body, removed paths)."""
    removed = []

    def drop(match):
        script = notify_script(match.group(1))
        if script is not None and any(_same(script, path) for path in owned):
            removed.append(script)
            return b''
        return match.group(0)
    return NOTIFY_LINE.sub(drop, body), removed


def merged_notify(original, python, script, uninstall=False, takeover=False):
    """Return (updated bytes, status). Ownership is the exact script path (#19):
    - uninstall removes only this installation's line;
    - install never replaces a foreign notifier, reuses another live Jason-memory dispatcher
      (it resolves each folder's own configuration) and replaces a dispatcher whose file is gone.
    `takeover` (global installer) replaces another Jason dispatcher, because the global one also
    serves folders that have their own project configuration."""
    bom = b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b''
    body, removed = strip_notify(original[len(bom):], [script])
    if uninstall:
        if removed:
            return bom + body, 'removed'
        return original, ('kept: notify belongs to another program or installation'
                          if ANY_NOTIFY.search(body) else 'absent')
    for match in list(NOTIFY_LINE.finditer(body)):
        other = notify_script(match.group(1))
        if other is None:
            continue
        if not other.is_file() or (takeover and is_jason_dispatcher(other)):
            body = body.replace(match.group(0), b'', 1)  # Stale, or handed over to the global dispatcher.
        elif is_jason_dispatcher(other):
            return original, ('shared: Codex turns are recorded by another Jason-memory installation ('
                              + str(other) + ')')
    if ANY_NOTIFY.search(body):
        return original, ('skipped: another Codex notify program is configured, so Codex turns are NOT '
                          'recorded; call ' + _Path(script).as_posix() + ' from it with the same JSON argument')
    line = ('notify = ' + json.dumps([_Path(python).as_posix(), _Path(script).as_posix()],
                                     ensure_ascii=False) + '\n').encode('utf-8')
    updated = bom + line + body
    return updated, 'installed' if updated != original else 'unchanged'
