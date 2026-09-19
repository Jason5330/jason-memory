#!/usr/bin/env python3
"""Canonical Markdown memory with versioned, recoverable multi-note transactions."""
import argparse
import contextlib
from datetime import date
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
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


def read_index(root):
    index = storelib.safe(root / 'MEMORY.md', root)
    with index.open('rb') as stream:
        raw = stream.read(storelib.doctor.INDEX_READ_CAP + 1)
    if len(raw) > storelib.doctor.INDEX_READ_CAP:
        raise ValueError('Index exceeds reading limit')
    return raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')


def revision(root):
    return revision_for(notes(root), read_index(root))


def revision_for(data, index):
    return storelib.digest(json.dumps({'notes': data, 'index': index}, sort_keys=True, ensure_ascii=False))


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


def recover(root, _prepared=None):
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
    if tx != _prepared:
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
        if not target.exists() or target.read_text(encoding='utf-8-sig') != content:
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
        index = read_index(root)
        result[scope] = {'root': str(root), 'revision': revision_for(data, index),
                         'index': index,
                         'notes': data, 'audit': report}
    return result


def receipt_path(root, relative):
    part = Path(relative)
    if (part.is_absolute() or '..' in part.parts or not part.parts or '\\' in relative
            or any(p.startswith('.') for p in part.parts) or part.suffix.casefold() != '.md'):
        raise ValueError('Invalid receipt note path')
    return storelib.safe(root / part, root)


def issue_receipt(root, paths, operation, _validated=None):
    """Issue evidence only after reading the committed files back under the store lock."""
    data = notes(root)
    index = read_index(root)
    if _validated != (data, index):
        validate(data, index)
    files = {}
    for path in paths:
        relative = Path(path).relative_to(root).as_posix()
        target = receipt_path(root, relative)
        if target.stat().st_size > storelib.doctor.NOTE_READ_CAP:
            raise ValueError('Receipt note exceeds reading limit')
        files[relative] = storelib.digest(target.read_text(encoding='utf-8-sig'))
    receipt = {'version': 1, 'id': uuid.uuid4().hex, 'root': str(root),
               'created_ns': time.time_ns(), 'operation': operation,
               'index_sha': storelib.digest(index), 'files': files}
    folder = storelib.safe(root / '.receipts', root)
    folder.mkdir(exist_ok=True)
    storelib.atomic_write(folder / (receipt['id'] + '.json'), json.dumps(receipt, ensure_ascii=True))
    return {'receipt': receipt['id'], 'receipt_root': str(root)}


def check_receipt(root, receipt_id, since=0):
    """Validate stored evidence against current disk contents; never trust a claimed filename alone."""
    if not isinstance(receipt_id, str) or len(receipt_id) != 32 or any(c not in '0123456789abcdef' for c in receipt_id):
        raise ValueError('Invalid receipt id')
    receipt = read_json(storelib.safe(root / '.receipts' / (receipt_id + '.json'), root))
    if (not isinstance(receipt, dict) or receipt.get('version') != 1 or receipt.get('id') != receipt_id
            or receipt.get('root') != str(root) or not isinstance(receipt.get('created_ns'), int)
            or receipt['created_ns'] < since or receipt.get('operation') not in ('apply', 'verify')
            or not isinstance(receipt.get('files'), dict) or not receipt['files']):
        raise ValueError('Missing, stale or invalid memory evidence')
    current_index = read_index(root)
    if storelib.digest(current_index) != receipt['index_sha']:
        # Other verified writes in this turn may add unrelated index entries.
        # Revalidate current reachability instead of invalidating unchanged notes.
        validate(notes(root), current_index)
    verified = []
    for relative, expected in receipt['files'].items():
        target = receipt_path(root, relative)
        if target.stat().st_size > storelib.doctor.NOTE_READ_CAP:
            raise ValueError('Receipt note exceeds reading limit')
        if storelib.digest(target.read_text(encoding='utf-8-sig')) != expected:
            raise ValueError('Memory content changed after verification')
        verified.append(str(target))
    return verified


def verify(root, names):
    if not names:
        raise ValueError('verify requires at least one --note relative/path.md')
    paths = [note_path(root, name) for name in names]
    evidence = issue_receipt(root, paths, 'verify')
    return {'changed': False, 'verified_paths': list(map(str, paths)),
            'contents': {name: path.read_text(encoding='utf-8-sig') for name, path in zip(names, paths)}, **evidence}


def apply(root, plan, scope='project'):
    if not isinstance(plan, dict) or set(plan) != {'expected_revision', 'updates', 'retire'}:
        raise ValueError('Plan needs expected_revision, updates (path: content), retire (paths)')
    old = notes(root)
    old_index = read_index(root)
    if plan['expected_revision'] != revision_for(old, old_index):
        raise ValueError('Revision conflict: refresh context and rebase all related corrections')
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
    index_changed = old_index != index
    if changed or index_changed:
        storelib.atomic_write(root / '.transaction.json', payload)
        recover(root, _prepared=tx)
    if notes(root) != final or read_index(root) != index:
        raise ValueError('Committed memory does not match the plan; do not claim success')
    result = {'changed': bool(changed or index_changed),
            'paths': [str(root / n) if n in final else str(root / 'archive' / tx['id'] / n) for n in changed],
            'index': str(root / 'MEMORY.md'), 'revision': revision_for(final, index), 'checks': checks,
            'semantic_review_needed': facts['semantic_review_needed'],
            'notice_required': bool(changed or index_changed)}
    proof_paths = result['paths'] + [str(root / n) for n in plan['updates'] if n not in changed]
    if not proof_paths:
        proof_paths = [str(root / 'MEMORY.md')]
    result.update(issue_receipt(root, proof_paths, 'apply', _validated=(final, index)))
    return result


def remember(root, subject, key, value, why, scope='project'):
    """Single-call add/verify. Corrections retain the explicit batch review path."""
    if not all(isinstance(v, str) and v.strip() for v in (subject, key, value, why)):
        raise ValueError('remember requires nonempty --subject, --key, --value and --why')
    if any('\n' in v or '\r' in v or '```' in v for v in (subject, key, value, why)):
        raise ValueError('remember accepts single-line facts; use apply for complex notes')
    subject, key = memory_facts.canonical(subject), memory_facts.canonical(key)
    data = notes(root)
    matches = []
    wanted = memory_facts.canonical(value) if key == 'occupation' else value
    for name, content in data.items():
        facts, _ = memory_facts.extract(content)
        for s, p, v in facts:
            if (s, p) == (subject, key):
                if v != wanted:
                    raise ValueError('Existing fact differs: use context and apply to batch-correct related notes, preserving other facts: ' + name)
                matches.append(name)
    if matches:
        return verify(root, sorted(set(matches)))
    slug = 'preference-' + hashlib.sha256((subject + '\0' + key).encode('utf-8')).hexdigest()[:16]
    name = 'feedback/' + slug + '.md'
    if name in data:
        raise ValueError('Existing note path needs manual batch review; do not overwrite: ' + name)
    today = date.today().isoformat()
    # JSON strings are valid quoted YAML scalar values; no hand-written metadata required.
    description = json.dumps(value, ensure_ascii=False)
    content = ('---\nname: ' + slug + '\ndescription: ' + description + '\ntype: feedback\n'
               'created: ' + today + '\nupdated: ' + today + '\n---\n\n' + value + '\n\nWhy: ' + why +
               '\n\nHow to apply: ' + ('全局共用' if scope == 'global' else '本專案') + '；' + value +
               '\n\n```jason-facts\n' + json.dumps([{'subject': subject, 'predicate': key, 'value': value}], ensure_ascii=False) + '\n```\n')
    index = read_index(root)
    result = apply(root, {'expected_revision': revision_for(data, index), 'updates': {name: content}, 'retire': []}, scope)
    result['saved_fact'] = {'subject': subject, 'predicate': key, 'value': value}
    return result


def emit_json(result, stream=None):
    stream = stream or sys.stdout
    raw = json.dumps(result, ensure_ascii=False) + '\n'
    if hasattr(stream, 'buffer'):
        stream.buffer.write(raw.encode('utf-8'))
    else:
        stream.write(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--project', type=Path)
    parser.add_argument('command', choices=['context', 'audit', 'apply', 'verify', 'remember'])
    parser.add_argument('--scope', choices=['project', 'global'], default='project')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--note', action='append', default=[])
    parser.add_argument('--subject')
    parser.add_argument('--key')
    parser.add_argument('--value')
    parser.add_argument('--why')
    args = parser.parse_args()
    try:
        with opened(args.config, args.project) as (_, project, roots):
            if args.command == 'apply':
                if args.scope not in roots or args.plan is None:
                    raise ValueError('Missing plan or unavailable scope')
                if str(args.plan) == '-':
                    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise ValueError('Plan exceeds reading limit')
                    plan = json.loads(raw.decode('utf-8-sig'))
                else:
                    plan = read_json(args.plan)
                result = apply(roots[args.scope], plan, args.scope)
            elif args.command == 'remember':
                if args.scope not in roots:
                    raise ValueError('Unavailable scope')
                result = remember(roots[args.scope], args.subject, args.key, args.value, args.why, args.scope)
            elif args.command == 'verify':
                if args.scope not in roots:
                    raise ValueError('Unavailable scope')
                result = verify(roots[args.scope], args.note)
            else:
                result = {'project': str(project), 'stores': context(roots)}
        emit_json(result)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        emit_json({'error': str(exc)}, sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
