#!/usr/bin/env python3
"""Enable/remove project Claude hooks without replacing other settings or memory."""
import argparse
import json
from pathlib import Path
import shlex
import sys
import uuid
import hook_settings
import memory_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        root = args.project.resolve(strict=True)
        config = root / '.jason-memory.json'
        if not config.is_file():
            raise ValueError('Missing .jason-memory.json; configure the canonical project root first')
        memory_runtime.configuration(config)
        target = memory_runtime.storelib.safe(root / '.claude/settings.json', root)
        original = target.read_bytes() if target.exists() else b''
        command = ' '.join(shlex.quote(p) for p in (Path(sys.executable).as_posix(),
                         (Path(__file__).parent / 'claude_memory_hook.py').as_posix(),
                         '--config', config.as_posix(), '--jason-hook'))
        updated = hook_settings.merged(original, None if args.uninstall else command)
        if not args.dry_run and updated != original:
            target.parent.mkdir(parents=True, exist_ok=True)
            if (target.read_bytes() if target.exists() else b'') != original:
                raise ValueError('Settings changed during install; retry')
            if target.exists():
                backup = target.with_name(target.name + '.' + uuid.uuid4().hex + '.bak')
                with backup.open('xb') as stream:
                    stream.write(original)
            memory_runtime.storelib.atomic_write(target, updated.decode('utf-8'))
        print(json.dumps({'settings': str(target), 'changed': updated != original,
                          'dry_run': args.dry_run, 'memories_preserved': True}))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
