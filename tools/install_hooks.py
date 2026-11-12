#!/usr/bin/env python3
"""One install for both hosts: Claude Code hooks and, in background memory mode, the Codex
`notify` program. Never replaces other settings, rules or memory."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys
import uuid
import hook_settings
import memory_runtime

# Same single-line form that tools/purge_memory.py removes.
NOTIFY_LINE = re.compile(rb'^[ \t]*notify[ \t]*=[ \t]*\[[^\r\n]*codex_notify\.py[^\r\n]*\][ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)', re.M)
ANY_NOTIFY = re.compile(rb'^[ \t]*notify[ \t]*=', re.M)


def codex_config(args):
    if args.codex_home:
        home = Path(args.codex_home)
    elif not args.user_home and os.environ.get('CODEX_HOME'):
        home = Path(os.environ['CODEX_HOME'])
    else:
        home = Path(args.user_home or Path.home()) / '.codex'
    return home.absolute() / 'config.toml'


def merged_notify(original, script, uninstall):
    """Return (updated bytes, status). Adds a top-level `notify` before any TOML table."""
    bom = b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b''
    body = NOTIFY_LINE.sub(b'', original[len(bom):])
    stripped = bom + body
    if uninstall:
        return stripped, 'removed' if stripped != original else 'absent'
    if ANY_NOTIFY.search(body):
        return original, 'skipped: another Codex notify program is configured; add codex_notify.py manually'
    line = ('notify = ' + json.dumps([Path(sys.executable).as_posix(), script.as_posix()],
                                     ensure_ascii=False) + '\n').encode('utf-8')
    updated = bom + line + body
    return updated, 'installed' if updated != original else 'unchanged'


def write_with_backup(target, original, updated):
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target.read_bytes() if target.exists() else b'') != original:
        raise ValueError('File changed during install; retry: ' + str(target))
    if target.exists():
        backup = target.with_name(target.name + '.' + uuid.uuid4().hex + '.bak')
        with backup.open('xb') as stream:
            stream.write(original)
    memory_runtime.storelib.atomic_write(target, updated.decode('utf-8'))  # A BOM stays as U+FEFF.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--codex-home', help='Codex configuration directory (default: CODEX_HOME or ~/.codex)')
    parser.add_argument('--user-home', help='Isolated profile for tests; ignores CODEX_HOME')
    parser.add_argument('--agent', choices=('both', 'claude', 'codex'), default='both')
    args = parser.parse_args(argv)
    try:
        root = args.project.resolve(strict=True)
        config = root / '.jason-memory.json'
        if not config.is_file():
            raise ValueError('Missing .jason-memory.json; configure the canonical project root first')
        cfg, _, _, _ = memory_runtime.configuration(config)
        result = {'dry_run': args.dry_run, 'memories_preserved': True}
        if args.agent in ('both', 'claude'):
            target = memory_runtime.storelib.safe(root / '.claude/settings.json', root)
            original = target.read_bytes() if target.exists() else b''
            command = ' '.join(shlex.quote(p) for p in (Path(sys.executable).as_posix(),
                             (Path(__file__).parent / 'claude_memory_hook.py').as_posix(),
                             '--config', config.as_posix(), '--jason-hook'))
            updated = hook_settings.merged(original, None if args.uninstall else command)
            if not args.dry_run and updated != original:
                write_with_backup(target, original, updated)
            result.update(settings=str(target), changed=updated != original)
        if args.agent in ('both', 'codex'):
            toml = codex_config(args)
            background = (cfg.get('ledger') or {}).get('enabled') is True
            if not args.uninstall and not background:
                status = 'not needed: ledger disabled (Codex reads AGENTS.md directly)'
            elif not toml.parent.is_dir():
                status = 'skipped: Codex configuration folder not found (' + str(toml.parent) + ')'
            else:
                if toml.is_symlink():
                    raise ValueError('Redirected Codex config is not supported: ' + str(toml))
                original = toml.read_bytes() if toml.exists() else b''
                updated, status = merged_notify(original, Path(__file__).resolve().with_name('codex_notify.py'),
                                                args.uninstall)
                if not args.dry_run and updated != original:
                    write_with_backup(toml, original, updated)
            result.update(codex_config=str(toml), codex_notify=status)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
