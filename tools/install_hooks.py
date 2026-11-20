#!/usr/bin/env python3
"""One install for both hosts: Claude Code hooks and, in background memory mode, the Codex
`notify` program. Never replaces other settings, rules or memory."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import uuid
import hook_settings
import memory_runtime

def codex_config(args):
    if args.codex_home:
        home = Path(args.codex_home)
    elif not args.user_home and os.environ.get('CODEX_HOME'):
        home = Path(os.environ['CODEX_HOME'])
    else:
        home = Path(args.user_home or Path.home()) / '.codex'
    return home.absolute() / 'config.toml'


def write_with_backup(target, original, updated):
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target.read_bytes() if target.exists() else b'') != original:
        raise ValueError('File changed during install; retry: ' + str(target))
    if target.exists():
        backup = target.with_name(target.name + '.' + uuid.uuid4().hex + '.bak')
        with backup.open('xb') as stream:
            stream.write(original)
    memory_runtime.storelib.atomic_write(target, updated.decode('utf-8'))  # A BOM stays as U+FEFF.


def hook_command(root, config):
    """Script and config are named relative to the project through Claude Code's $CLAUDE_PROJECT_DIR,
    so moving or renaming the project folder keeps the hook working (#87). A framework outside the
    project keeps its absolute path. The interpreter stays absolute: a missing interpreter is a
    non-blocking hook error, while a bare `python` may be absent from PATH or be another install."""
    script = (Path(__file__).resolve().parent / 'claude_memory_hook.py')
    python = hook_settings.shell_quote(Path(sys.executable).as_posix())
    try:
        relative = script.relative_to(root).as_posix()
    except ValueError:
        return hook_settings.shell_join((Path(sys.executable).as_posix(), script.as_posix(),
                                         '--config', config.as_posix(), '--jason-hook'))
    return (python + ' "$CLAUDE_PROJECT_DIR/' + relative + '" --config "$CLAUDE_PROJECT_DIR/'
            + config.relative_to(root).as_posix() + '" --jason-hook')


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
        framework = Path(__file__).resolve().parents[1]
        problem = hook_settings.path_too_long(root, [p.relative_to(root).as_posix() for p in
                                                     framework.rglob('*.py') if root in p.parents] or ['tools'] )
        store = memory_runtime.configuration(config)[3]['project']
        problem = problem or hook_settings.path_too_long(store, hook_settings.STORE_DEPTHS)
        if problem and not args.uninstall:
            # A hook whose script Windows cannot open exits 2 and blocks every prompt: refuse up front.
            raise ValueError(problem)
        result = {'dry_run': args.dry_run, 'memories_preserved': True}
        if args.agent in ('both', 'claude'):
            target = memory_runtime.storelib.safe(root / '.claude/settings.json', root)
            original = target.read_bytes() if target.exists() else b''
            command = hook_command(root, config)
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
                updated, status = hook_settings.merged_notify(
                    original, sys.executable, Path(__file__).resolve().with_name('codex_notify.py'), args.uninstall)
                if not args.dry_run and updated != original:
                    write_with_backup(toml, original, updated)
            result.update(codex_config=str(toml), codex_notify=status)
            if background and not args.uninstall and status.startswith('skipped: another'):
                result['warnings'] = ['Codex is NOT connected to background memory: ' + status]
        if not args.uninstall and (cfg.get('ledger') or {}).get('enabled') is True:
            import memory_dream
            if memory_dream.agent_command({'dream_agent': (cfg.get('ledger') or {}).get('dream_agent', 'auto')}) is None:
                # Say it now, not only later in `pending` (#71).
                result.setdefault('warnings', []).append(memory_dream.NO_AGENT)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
