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
            if name in ('PreToolUse', 'PostToolUse'):
                group['matcher'] = 'Read|Write|Edit|MultiEdit|Bash|PowerShell|powershell'
            retained.append(group)
        if retained:
            hooks[name] = retained
        else:
            hooks.pop(name, None)
    if not hooks:
        data.pop('hooks', None)
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
