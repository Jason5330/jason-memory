#!/usr/bin/env python3
"""Canonical Markdown memory with versioned, recoverable multi-note transactions."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

import memory_facts

BASE = Path(__file__).resolve().parents[1]
source = BASE / 'global/global_store.py'
if not source.exists():
    source = BASE / 'global_store.py'
spec = importlib.util.spec_from_file_location('memory_store', source)
storelib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storelib)
MAX_BYTES = 8 * 1024 * 1024


def read_json(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('JSON exceeds reading limit')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def configuration(path, project=None):
    path = Path(path).absolute()
    # Windows TEMP commonly uses 8.3 aliases (JASON_~1). Canonicalize those without
    # accepting symlink/junction redirections in any original path component.
    for component in (path, *path.parents):
        if component.is_symlink() or (component.exists() and
                getattr(component.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Redirected memory configuration path is not supported: ' + str(component))
    path = path.resolve(strict=True)
    cfg = read_json(path)
    if cfg.get('version') != 1 or cfg.get('mode') not in ('project', 'global'):
        raise ValueError('Unsupported memory configuration')
    if cfg['mode'] == 'project':
        root = path.parent
        relative = Path(cfg['memory_root'])
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise ValueError('Project memory_root must be a child path')
        memory = storelib.safe(root / relative, root)
        if memory == root:
            raise ValueError('Memory root cannot equal project root')
        return cfg, root, memory, {'project': memory}
    if project is None:
        raise ValueError('Global mode requires --project ACTIVE_PROJECT_ROOT')
    root = Path(project).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Project root must be a directory')
    home = (path.parent / cfg['home']).absolute()
    # Resolve explicit .. in the installed relative home, then reject redirected parents.
    home = Path(os.path.abspath(home))
    storelib.safe(home, home.parent)
    key = hashlib.sha256(os.path.normcase(str(root)).encode('utf-8')).hexdigest()
    return cfg, root, home, {'global': home / 'memory/shared', 'project': home / 'memory/projects' / key}


def note_path(root, name):
    if not isinstance(name, str) or '\\' in name:
        raise ValueError('Use forward-slash relative note paths')
    part = Path(name)
    if (part.is_absolute() or '..' in part.parts or not part.parts or
            any(p.startswith('.') for p in part.parts) or part.parts[0] == 'archive' or
            part.name.casefold() == 'memory.md' or part.suffix.casefold() != '.md'):
        raise ValueError('Invalid active note path: ' + name)
    storelib.slug_check(part.stem)
    return storelib.safe(root / part, root)


def notes(root):
    result = {}
    def walk_error(error):
        raise error
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = sorted(d for d in dirs if d != 'archive' and not d.startswith('.'))
        for directory in dirs:
            storelib.safe(Path(folder) / directory, root)
        for filename in sorted(files):
            path = Path(folder) / filename
            if path.suffix.casefold() != '.md' or path.name.startswith('.') or path.name.casefold() == 'memory.md':
                continue
            rel = path.relative_to(root).as_posix()
            note_path(root, rel)
            if path.stat().st_size > storelib.doctor.NOTE_READ_CAP:
                raise ValueError('Note exceeds reading limit: ' + str(path))
            result[rel] = path.read_text(encoding='utf-8-sig')
    if sum(len(t.encode('utf-8')) for t in result.values()) > MAX_BYTES:
        raise ValueError('Active notes exceed transaction limit; curate before writing')
    return result


def revision(root):
    index = storelib.safe(root / 'MEMORY.md', root)
    if index.stat().st_size > storelib.doctor.INDEX_READ_CAP:
        raise ValueError('Index exceeds reading limit')
    data = {'notes': notes(root), 'index': index.read_text(encoding='utf-8-sig')}
    return storelib.digest(json.dumps(data, sort_keys=True, ensure_ascii=False))


def index_for(data, archived=False):
    sections = {kind: [] for kind in ('user', 'feedback', 'project', 'reference')}
    for name, content in sorted(data.items()):
        fm, problems, _ = storelib.doctor._frontmatter(content)
        if not fm or problems or fm.get('name') != Path(name).stem or fm.get('type') not in sections:
            raise ValueError('Invalid frontmatter/name/type: ' + name)
        description = fm.get('description', '')
        if not description or '\n' in description or '\r' in description:
            raise ValueError('Use a one-line description: ' + name)
        sections[fm['type']].append('- [' + fm['name'] + '](' + name + ') — ' + description)
    text = '# 記憶索引 (Jason-memory)\n\n> 索引只定位，相關筆記正文才是完整要求。\n'
    for kind, rows in sections.items():
        text += '\n## ' + kind + '\n\n' + '\n'.join(rows) + '\n'
    if archived:
        text += '\n- [歷史記憶](archive/MEMORY.md) — 僅供追溯，不作現行要求\n'
    return text


def validate(data, index):
    facts = memory_facts.audit(data)
    if facts['conflicts'] or facts['invalid_facts']:
        raise ValueError('Fact conflict/invalid metadata: ' + json.dumps(facts, ensure_ascii=False))
    with tempfile.TemporaryDirectory(prefix='jason-batch-') as tmp:
        stage = Path(tmp).resolve()
        for name, content in data.items():
            dest = note_path(stage, name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding='utf-8')
        (stage / 'MEMORY.md').write_text(index, encoding='utf-8')
        if 'archive/MEMORY.md' in index:
            (stage / 'archive').mkdir()
            (stage / 'archive/MEMORY.md').write_text('# History\n', encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = storelib.doctor.main(['doctor', str(stage)])
            size = storelib.check.main(['check', str(stage / 'MEMORY.md')])
        if code or size:
            raise ValueError('Memory validation failed: ' + output.getvalue())
    return output.getvalue().strip(), facts


def recover(root):
    journal = storelib.safe(root / '.transaction.json', root)
    if not journal.exists():
        return
    tx = read_json(journal)
    if (not isinstance(tx, dict) or set(tx) != {'id', 'notes', 'before', 'remove', 'index'} or
            not isinstance(tx['id'], str) or len(tx['id']) != 32 or
            any(c not in '0123456789abcdef' for c in tx['id'])):
        raise ValueError('Invalid transaction journal; manual review required')
    if not isinstance(tx['notes'], dict) or not isinstance(tx['before'], dict) or not isinstance(tx['remove'], list):
        raise ValueError('Invalid transaction payload')
    for name, content in list(tx['notes'].items()) + list(tx['before'].items()):
        note_path(root, name)
        if not isinstance(content, str):
            raise ValueError('Invalid note content')
    for name in tx['remove']:
        note_path(root, name)
        if name in tx['notes']:
            raise ValueError('Cannot remove a retained note')
    expected_index = index_for(tx['notes'], bool(tx['before']) or (root / 'archive/MEMORY.md').exists())
    if tx['index'] != expected_index:
        raise ValueError('Journal index does not match final notes')
    validate(tx['notes'], tx['index'])
    # Readers using this runtime hold the same lock and finish this replay before reading.
    for name, content in tx['before'].items():
        target = storelib.safe(root / 'archive' / tx['id'] / name, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        storelib.atomic_write(target, content)
    if tx['before']:
        manifest = storelib.safe(root / 'archive/MEMORY.md', root)
        links = sorted(p.relative_to(manifest.parent).as_posix() for p in manifest.parent.rglob('*.md') if p != manifest)
        storelib.atomic_write(manifest, '# 歷史資料，不作現行要求\n\n' + ''.join('- [' + p + '](' + p + ')\n' for p in links))
    for name, content in tx['notes'].items():
        target = note_path(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        storelib.atomic_write(target, content)
    for name in tx['remove']:
        target = note_path(root, name)
        if target.exists():
            target.unlink()
    storelib.atomic_write(root / 'MEMORY.md', tx['index'])
    journal.unlink()


@contextlib.contextmanager
def opened(config, project=None):
    cfg, project, lock_root, roots = configuration(config, project)
    with storelib.locked(lock_root):
        for root in roots.values():
            storelib.safe(root, lock_root)
            root.mkdir(parents=True, exist_ok=True)
            index = storelib.safe(root / 'MEMORY.md', lock_root)
            if not index.exists():
                storelib.atomic_write(index, (BASE / 'templates/MEMORY.md').read_text(encoding='utf-8-sig'))
            storelib.recover(root)
            recover(root)
        yield cfg, project, roots


def context(roots):
    result = {}
    for scope, root in roots.items():
        data = notes(root)
        report = memory_facts.audit(data)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = storelib.doctor.main(['doctor', str(root)])
            size = storelib.check.main(['check', str(root / 'MEMORY.md')])
        report['structural_errors'] = output.getvalue().strip() if code or size else ''
        result[scope] = {'root': str(root), 'revision': revision(root),
                         'index': (root / 'MEMORY.md').read_text(encoding='utf-8-sig'),
                         'notes': data, 'audit': report}
    return result


def apply(root, plan, scope='project'):
    if not isinstance(plan, dict) or set(plan) != {'expected_revision', 'updates', 'retire'}:
        raise ValueError('Plan needs expected_revision, updates (path: content), retire (paths)')
    if plan['expected_revision'] != revision(root):
        raise ValueError('Revision conflict: refresh context and rebase all related corrections')
    old = notes(root)
    final = dict(old)
    if not isinstance(plan['updates'], dict) or not isinstance(plan['retire'], list):
        raise ValueError('Invalid updates/retire plan')
    for name in plan['retire']:
        note_path(root, name)
        if name not in old or name in plan['updates']:
            raise ValueError('Retire must name an existing note without an update')
        del final[name]
    for name, content in plan['updates'].items():
        note_path(root, name)
        if not isinstance(content, str):
            raise ValueError('Note content must be a string')
        fm, _, _ = storelib.doctor._frontmatter(content)
        extracted, structured = memory_facts.extract(content)
        if old.get(name) != content and fm and fm.get('type') in ('user', 'feedback') and not structured:
            raise ValueError('New/corrected user or feedback notes require a jason-facts JSON block: ' + name)
        final[name] = content
    # Updating a mixed note must not silently drop an unrelated identified fact.
    retained_keys = {(s, p) for content in final.values() for s, p, _ in memory_facts.extract(content)[0]}
    old_keys = {(s, p) for name in plan['updates'] if name in old
                for s, p, _ in memory_facts.extract(old[name])[0]}
    if old_keys - retained_keys:
        raise ValueError('Update drops unrelated fact keys; preserve them or explicitly retire the note: '
                         + repr(sorted(old_keys - retained_keys)))
    if scope == 'global':
        for text in final.values():
            fm, _, _ = storelib.doctor._frontmatter(text)
            if fm and fm.get('type') == 'project':
                raise ValueError('Project notes require project scope')
    changed = sorted(n for n in set(old) | set(final) if old.get(n) != final.get(n))
    before = {n: old[n] for n in changed if n in old}
    index = index_for(final, bool(before) or (root / 'archive/MEMORY.md').exists())
    checks, facts = validate(final, index)
    # Metadata-free legacy notes remain supported, but cannot be certified semantically clean.
    tx = {'id': uuid.uuid4().hex, 'notes': final, 'before': before,
          'remove': plan['retire'], 'index': index}
    payload = json.dumps(tx, ensure_ascii=False)
    if len(payload.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Transaction exceeds size limit')
    index_changed = (root / 'MEMORY.md').read_text(encoding='utf-8-sig') != index
    if changed or index_changed:
        storelib.atomic_write(root / '.transaction.json', payload)
        recover(root)
    return {'changed': bool(changed or index_changed),
            'paths': [str(root / n) if n in final else str(root / 'archive' / tx['id'] / n) for n in changed],
            'index': str(root / 'MEMORY.md'), 'revision': revision(root), 'checks': checks,
            'semantic_review_needed': facts['semantic_review_needed'],
            'notice_required': bool(changed or index_changed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--project', type=Path)
    parser.add_argument('command', choices=['context', 'audit', 'apply'])
    parser.add_argument('--scope', choices=['project', 'global'], default='project')
    parser.add_argument('--plan', type=Path)
    args = parser.parse_args()
    try:
        with opened(args.config, args.project) as (_, project, roots):
            if args.command == 'apply':
                if args.scope not in roots or args.plan is None:
                    raise ValueError('Missing plan or unavailable scope')
                result = apply(roots[args.scope], read_json(args.plan), args.scope)
            else:
                result = {'project': str(project), 'stores': context(roots)}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
