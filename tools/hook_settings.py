"""Merge only owned Claude hook commands, preserving unrelated configuration."""
import copy
import json
import os as _os
from pathlib import Path as _Path
import re as _re
import shlex

TOOL_EVENTS = ('PostToolUse', 'PostToolUseFailure')
EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', *TOOL_EVENTS, 'Stop')
MARKER = '--jason-hook'


def shell_quote(text):
    """Quote one argument for the shell that runs hooks and the agent's Bash tool (#98): double quotes,
    which Git Bash (Claude Code's hook shell on Windows), sh and cmd.exe all accept, unlike POSIX single
    quotes. Inside them only the characters bash still expands are escaped."""
    text = str(text)
    if text and all(c.isalnum() or c in '-_./:=@%+,' for c in text):
        return text
    if '"' in text:
        raise ValueError('A path containing a double quote cannot be used in a hook command: ' + text)
    return '"' + text.replace('\\', '\\\\').replace('$', '\\$').replace('`', '\\`') + '"'


def shell_join(parts):
    return ' '.join(shell_quote(p) for p in parts)


MAX_PATH = 259  # Characters Windows allows in a full path unless long paths are enabled.
LONG_PATH_HINT = ('Windows limits paths to 260 characters unless long paths are enabled: move the project to a '
                  'shorter folder, or enable "LongPathsEnabled" (Windows long path support) and restart')


def long_paths_enabled():
    if _os.name != 'nt':
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\FileSystem') as key:
            return winreg.QueryValueEx(key, 'LongPathsEnabled')[0] == 1
    except OSError:
        return False


def path_too_long(base, relatives):
    """The longest full path the framework will need under `base`, if Windows cannot open it (#99)."""
    if long_paths_enabled():
        return None
    longest = max(relatives, key=len)
    full = len(str(_Path(base))) + 1 + len(longest)
    if full > MAX_PATH:
        return ('Path too long for Windows: ' + str(base) + ' needs paths of ' + str(full) + ' characters (for example '
                + longest + '), over the ' + str(MAX_PATH + 1) + '-character limit. ' + LONG_PATH_HINT + '.')
    return None


# Deepest paths every conversation needs inside a store: hook state (sha256 name) and a set-aside
# progress file (#95). Rare deeper paths (a quarantined note) report WinError 206 with a hint instead.
STORE_DEPTHS = ('.hook-state/' + 'f' * 64 + '.json', '.raw/dream.json.corrupt-20261120-170049-3ef571')


def python_command():
    """`python` (or `python3`) when that name on PATH is this same interpreter: a short command needs no
    per-path approval in sandboxed agents (#87). Otherwise the absolute interpreter path."""
    import shutil
    import sys
    for name in ('python', 'python3'):
        found = shutil.which(name)
        try:
            if found and _os.path.samefile(found, sys.executable):
                return name
        except OSError:
            continue
    return _Path(sys.executable).as_posix()


def managed_home(block):
    # Both generated Markdown variants contain one JSON block with a memory_home field.
    _, found, remainder = block.replace(b'\r\n', b'\n').partition(b'```json\n')
    if not found:
        return None  # Legacy blocks without a recorded home.
    encoded, closing, _ = remainder.partition(b'\n```')
    values = json.loads(encoded) if closing else {}
    if not isinstance(values, dict) or not isinstance(values.get('memory_home'), str):
        raise ValueError('Invalid Jason-memory managed home; cannot determine ownership')
    path = _Path(values['memory_home'])
    if not path.is_absolute():
        raise ValueError('Invalid Jason-memory managed home; expected an absolute path')
    return path


def _is_managed_hook(hook, owned_script):
    command = hook.get('command') if isinstance(hook, dict) else None
    if not isinstance(command, str) or MARKER not in command or 'claude_memory_hook.py' not in command:
        return False
    if owned_script is None:
        return True
    try:
        args = shlex.split(command)
        config_argument = args.index('--config')
        return config_argument > 0 and _same(args[config_argument - 1], owned_script)
    except ValueError:
        return False


def merged(original, command=None, *, owned_script=None):
    previous = json.loads(original.decode('utf-8-sig')) if original.strip() else {}
    if not isinstance(previous, dict) or not isinstance(previous.get('hooks', {}), dict):
        raise ValueError('Claude settings/hooks must be JSON objects')
    data = copy.deepcopy(previous)
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
            group['hooks'] = [h for h in row['hooks'] if not _is_managed_hook(h, owned_script)]
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
    if owned_script is not None and data == previous:
        return original
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


# --- Codex notify (shared by the project/global installers and the purge tool) --

# Single-line `notify = [...]` arrays that mention codex_notify.py (the only form we ever write).
NOTIFY_KEY = rb"""^[ \t]*(?:notify|"notify"|'notify')[ \t]*="""
NOTIFY_LINE = _re.compile(NOTIFY_KEY + rb'[ \t]*(\[[^\r\n]*codex_notify\.py[^\r\n]*\])[ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)')
ANY_NOTIFY = _re.compile(NOTIFY_KEY)
TOML_TEXT = _re.compile(
    rb'#[^\r\n]*'
    rb'|"""(?:\\[\s\S]|""?(?!")|[^"\\])*"{3,5}'
    rb"|'''(?:''?(?!')|[^'])*'{3,5}"
    rb'|"(?:\\[^\r\n]|[^"\\\r\n])*"'
    rb"|'[^'\r\n]*'"
)


def _notify_lines(body):
    """Find top-level notify assignments while leaving strings, tables and array contents alone."""
    syntax = TOML_TEXT.sub(lambda found: _re.sub(rb'[^\r\n]', b' ', found.group()), body)
    position, nesting = 0, 0
    for row in syntax.splitlines(keepends=True):
        end = position + len(row)
        if nesting == 0 and row.lstrip().startswith(b'['):
            break  # A TOML table ends the top-level assignments.
        original = body[position:end]
        if nesting == 0 and b'=' in row and ANY_NOTIFY.match(original):
            yield position, original
        nesting += row.count(b'[') + row.count(b'{') - row.count(b']') - row.count(b'}')
        position = end


def _without_lines(body, lines):
    for position, row in reversed(lines):
        body = body[:position] + body[position + len(row):]
    return body


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
    removed, lines = [], []
    for position, row in _notify_lines(body):
        match = NOTIFY_LINE.match(row)
        if match is None:
            continue
        script = notify_script(match.group(1))
        if script is not None and any(_same(script, path) for path in owned):
            removed.append(script)
            lines.append((position, row))
    return _without_lines(body, lines), removed


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
                          if next(_notify_lines(body), None) is not None else 'absent')
    stale = []
    for position, row in _notify_lines(body):
        match = NOTIFY_LINE.match(row)
        if match is None:
            continue
        other = notify_script(match.group(1))
        if other is None:
            continue
        if not other.is_file() or (takeover and is_jason_dispatcher(other)):
            stale.append((position, row))  # Stale, or handed over to the global dispatcher.
        elif is_jason_dispatcher(other):
            return original, ('shared: Codex turns are recorded by another Jason-memory installation ('
                              + str(other) + ')')
    body = _without_lines(body, stale)
    if next(_notify_lines(body), None) is not None:
        return original, ('skipped: another Codex notify program is configured, so Codex turns are NOT '
                          'recorded; call ' + _Path(script).as_posix() + ' from it with the same JSON argument')
    line = ('notify = ' + json.dumps([_Path(python).as_posix(), _Path(script).as_posix()],
                                     ensure_ascii=False) + '\n').encode('utf-8')
    updated = bom + line + body
    return updated, 'installed' if updated != original else 'unchanged'
