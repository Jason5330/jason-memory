#!/usr/bin/env python3
"""Permanently remove an identified Jason-memory installation; never scan the PC."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import tempfile

sys.dont_write_bytecode = True  # --dry-run must not create even a helper cache.
import hook_settings

START = b'<!-- jason-memory-global:start -->'
END = b'<!-- jason-memory-global:end -->'
RECALL = b'<!-- jason-memory-global:recall-v2 -->'
KEEP = {'UNINSTALL.bat', 'tools/purge_memory.py', 'tools/hook_settings.py',
        'uninstall-manifest.json', 'docs/uninstall.md'}
# Exact destinations owned by global/install.py, including its generated config.
GLOBAL_FILES = (
    'docs/memory-runtime.md', 'tools/memory_facts.py', 'tools/memory_runtime.py',
    'tools/claude_memory_hook.py', 'tools/hook_settings.py', 'SKILL.md',
    'global_store.py', 'templates/MEMORY.md', 'tools/jason_check.py',
    'tools/jason_doctor.py', 'memory-config.json',
)


def digest(data):
    # Git checkouts may convert text line endings on Windows.
    return hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest()


def safe(path):
    path = Path(os.path.abspath(Path(path).expanduser()))
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and
                getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Refusing symlink/junction/reparse path: ' + str(part))
    return path.resolve()


def child(root, name):
    win = PureWindowsPath(name)
    relative = Path(name)
    if (not isinstance(name, str) or not relative.parts or relative.is_absolute()
            or win.drive or win.root or '..' in relative.parts or '..' in win.parts
            or ':' in name or '\\' in name):
        raise ValueError('Unsafe relative path: ' + str(name))
    path = safe(root / relative)
    if path == root or root not in path.parents:
        raise ValueError('Path escapes the selected folder: ' + str(path))
    return path


def strip_block(data):
    if not data.count(START) and not data.count(END):
        return data
    if data.count(START) != 1 or data.count(END) != 1 or data.index(END) < data.index(START):
        raise ValueError('Damaged/duplicate Jason-memory managed block; no files removed')
    start, end = data.index(START), data.index(END) + len(END)
    # v2 owns its trailing separator. Preserve every other user byte, including BOM.
    if RECALL in data[start:end]:
        if data[end:end + 2] == b'\r\n':
            end += 2
        elif data[end:end + 1] == b'\n':
            end += 1
    return data[:start] + data[end:]


class Plan:
    def __init__(self):
        self.files = {}  # expected original bytes, replacement bytes or None
        self.dirs = set()
        self.empty_dirs = set()
        self.remaining = []

    def file(self, path, updated=None):
        path = safe(path)
        if path.exists():
            if not path.is_file():
                raise ValueError('Expected a regular file: ' + str(path))
            original = path.read_bytes()
            if updated != original:
                self.files[path] = (original, updated)

    def tree(self, path):
        path = safe(path)
        if not path.exists():
            return
        if not path.is_dir():
            raise ValueError('Expected a directory: ' + str(path))
        # Enumerate and validate everything BEFORE any mutation; never follow links.
        for current, dirs, files in os.walk(path, followlinks=False):
            base = safe(current)
            self.dirs.add(base)
            for name in dirs + files:
                target = safe(base / name)
                if target != path and path not in target.parents:
                    raise ValueError('Tree escaped deletion boundary: ' + str(target))
            for name in files:
                self.file(base / name)

    def execute(self):
        # Recheck all originals before applying the plan. Close AI sessions first.
        for path, (original, _) in self.files.items():
            if safe(path).read_bytes() != original:
                raise ValueError('File changed during preflight; retry: ' + str(path))
        # Detach entrances/hooks before deleting framework executables or memories.
        entrances = {'AGENTS.md', 'AGENTS.override.md', 'CLAUDE.md', 'settings.json',
                     'settings.local.json', '.jason-memory.json'}
        ordered = sorted(self.files.items(), key=lambda item: (
            item[0].name not in entrances, item[1][1] is None))
        for path, (original, updated) in ordered:
            if safe(path).read_bytes() != original:
                raise ValueError('File changed during cleanup: ' + str(path))
            if updated is None:
                path.unlink()
            else:
                fd, temp = tempfile.mkstemp(prefix='.jason-purge-', dir=str(path.parent))
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(updated)
                    os.replace(temp, path)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
        for path in sorted(self.dirs, key=lambda p: len(p.parts), reverse=True):
            safe(path).rmdir()  # Fails if a concurrent writer created a new file.
        for path in sorted(self.empty_dirs, key=lambda p: len(p.parts), reverse=True):
            path = safe(path)
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()


def clean_settings(plan, target):
    target = safe(target)
    if not target.exists():
        return
    original = target.read_bytes()
    updated = hook_settings.merged(original)
    # Avoid touching/reformatting settings which have no owned hooks.
    if json.loads(original.decode('utf-8-sig').strip() or '{}') != json.loads(updated):
        plan.file(target, updated if json.loads(updated) else None)


def clean_rules(plan, target):
    target = safe(target)
    if target.is_file():
        data = target.read_bytes()
        updated = strip_block(data)
        if updated != data:
            plan.file(target, updated if updated.strip(b'\xef\xbb\xbf\r\n\t ') else None)
        # Discover custom global homes only from a validated managed block.
        if START in data:
            block = data[data.index(START):data.index(END)]
            return [Path(json.loads(match)) for match in re.findall(
                rb'"memory_home"\s*:\s*("(?:[^"\\]|\\.)*")', block)]
    return []


def clean_backups(plan, directory, names, project=False):
    directory = safe(directory)
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        for name in names:
            if re.fullmatch(re.escape(name) + r'\.jason-memory-[0-9a-f]{32}\.bak', path.name):
                plan.file(path)
            elif project and re.fullmatch(re.escape(name) + r'\.[0-9a-f]{32}\.bak', path.name):
                data = safe(path).read_bytes()
                if b'--jason-hook' in data and b'claude_memory_hook.py' in data:
                    # Keep unrelated settings from historical mixed JSON backups too.
                    clean_settings(plan, path)
                else:
                    plan.remaining.append(str(path) + ' (backup ownership uncertain; preserved)')


def clean_global(plan, home):
    # A custom installation home may predate this framework and contain user files.
    # Delete the owned memory subtree and exact installed files, never the whole home.
    memory = safe(home / 'memory')
    if memory.exists() and not safe(memory / 'shared/MEMORY.md').is_file():
        raise ValueError('Cannot identify global memory subtree: ' + str(memory))
    plan.tree(memory)
    framework = safe(home / 'framework')
    for name in GLOBAL_FILES:
        path = child(framework, name)
        plan.file(path)
        for parent in path.parents:
            if parent == home:
                break
            plan.empty_dirs.add(parent)
        if name.endswith('.py'):
            cache = safe(path.parent / '__pycache__')
            if cache.is_dir():
                for compiled in cache.glob(path.stem + '.*.pyc'):
                    plan.file(compiled)
                plan.empty_dirs.add(cache)
    plan.file(home / '.writer.lock')
    clean_rules(plan, home / 'GENERIC_INSTRUCTIONS.md')
    plan.empty_dirs.add(home)


def make_plan(args):
    root = safe(args.project or Path(__file__).resolve().parents[1])
    if not root.is_dir() or not any(safe(root / name).is_file() for name in (
            '.jason-memory.json', 'uninstall-manifest.json')):
        raise ValueError('Cannot identify selected project/package: ' + str(root))
    profile = safe(args.user_home or Path.home())
    codex = safe(args.codex_home or (None if args.user_home else os.environ.get('CODEX_HOME'))
                 or profile / '.codex')
    claude = safe(args.claude_home or (None if args.user_home else os.environ.get('CLAUDE_CONFIG_DIR'))
                  or profile / '.claude')
    plan = Plan()
    homes = {safe(args.home or profile / '.jason-memory-global')}
    for directory, names in ((codex, ('AGENTS.md', 'AGENTS.override.md')),
                             (claude, ('CLAUDE.md',))):
        for name in names:
            homes.update(safe(p) for p in clean_rules(plan, directory / name))
        clean_backups(plan, directory, names + ('settings.json',))
    clean_settings(plan, claude / 'settings.json')
    clean_settings(plan, claude / 'settings.local.json')
    protected = {root, profile, codex, claude, Path(root.anchor)}
    for home in homes:
        if any(home == p or home in p.parents or p in home.parents and p in (codex, claude)
               for p in protected):
            raise ValueError('Unsafe global deletion root: ' + str(home))
        if home.exists():
            marker = safe(home / 'framework/memory-config.json')
            if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8-sig')) != {
                    'version': 1, 'mode': 'global', 'home': '..'}:
                raise ValueError('Cannot verify Jason-memory global ownership: ' + str(home))
            clean_global(plan, home)

    config = safe(root / '.jason-memory.json')
    if config.exists():
        cfg = json.loads(config.read_text(encoding='utf-8-sig'))
        if cfg.get('version') != 1 or cfg.get('mode') != 'project':
            raise ValueError('Expected a version 1 project configuration')
        memory = child(root, cfg['memory_root'])
        # A local config alone must never authorize deleting an AI config or source directory.
        if memory.parts[len(root.parts)] in ('.git', '.claude', '.codex', 'tools', 'global',
                                             'docs', 'tests', 'templates', 'evals'):
            raise ValueError('Unsafe project memory root: ' + str(memory))
        if memory.exists() and not safe(memory / 'MEMORY.md').is_file():
            raise ValueError('Missing memory index; cannot verify store: ' + str(memory))
        plan.tree(memory)
        plan.file(config)
    clean_settings(plan, root / '.claude/settings.json')
    clean_settings(plan, root / '.claude/settings.local.json')
    clean_backups(plan, root / '.claude', ('settings.json', 'settings.local.json'), project=True)

    manifest = safe(root / 'uninstall-manifest.json')
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding='utf-8-sig'))
        if data.get('format') != 'jason-memory-package-v1' or not isinstance(data.get('files'), dict):
            raise ValueError('Invalid package manifest')
        for name, expected in data['files'].items():
            path = child(root, name)
            if name in KEEP or path in plan.files or name.startswith('.jason-memory/'):
                continue
            # Never permit a manifest to include host data, nested Git metadata or memory.
            if (any(part in ('.git', '.codex', '.claude', '.jason-memory') for part in path.relative_to(root).parts)
                    or name.endswith('.json') and name == '.jason-memory.json'):
                continue
            if path.is_file():
                if digest(path.read_bytes()) == expected:
                    plan.file(path)
                else:
                    plan.remaining.append(str(path) + ' (modified framework file; preserved)')
            if name.endswith('.py'):
                cache = safe(path.parent / '__pycache__')
                if cache.is_dir():
                    for compiled in cache.glob(path.stem + '.*.pyc'):
                        plan.file(compiled)
    else:
        plan.remaining.append(str(manifest) + ' (missing; framework source files preserved)')
    # Managed blocks may have been merged into local custom rules. Do not delete user prose.
    for name in ('AGENTS.md', 'AGENTS.override.md', 'CLAUDE.md'):
        path = safe(root / name)
        if path not in plan.files:
            clean_rules(plan, path)
            if path.is_file() and b'jason-memory' in strip_block(path.read_bytes()).lower():
                message = str(path) + ' (unmarked custom rules; manual review required)'
                if not any(str(path) in item for item in plan.remaining):
                    plan.remaining.append(message)
    return root, homes, plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('project', 'user-home', 'home', 'codex-home', 'claude-home'):
        parser.add_argument('--' + name)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    result = {'dry_run': args.dry_run, 'complete': False}
    try:
        root, homes, plan = make_plan(args)
        result.update(project=str(root), global_homes=sorted(map(str, homes)),
                      delete=[str(p) for p, (_, new) in plan.files.items() if new is None],
                      edit=[str(p) for p, (_, new) in plan.files.items() if new is not None],
                      remaining=plan.remaining,
                      kept='Cleanup utility, unrelated files/settings, Git history and other project copies')
        if not args.dry_run:
            plan.execute()
        result['complete'] = not plan.remaining
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 2 if plan.remaining else 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result['error'] = str(exc)
        result['message'] = 'Cleanup failed; do not assume full removal. Inspect paths and retry.'
        print(json.dumps(result, ensure_ascii=True, indent=2), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
