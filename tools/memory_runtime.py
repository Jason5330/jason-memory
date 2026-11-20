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
import re
from pathlib import Path
import shutil
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
NOTE_TYPES = ('user', 'feedback', 'project', 'reference')
SECRET_ERROR = ('NOT SAVED: credential-like value detected. Memory may store an environment variable '
                'name or where the secret is kept, never the secret value itself: ')
BACKGROUND_ERROR = ('NOT SAVED: background memory mode. The foreground agent does not write memory; '
                    'this turn is already recorded in the raw ledger and will be consolidated in the '
                    'background. Do not tell the user it was remembered.')
INDEX_BUDGET = 6000  # UTF-8 bytes per store for the bounded index command.


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


def foreground_writes_allowed(cfg, roots=None):
    """False in background mode unless this process is the background consolidation worker: it carries
    JASON_DREAM=1 AND the owner token of the job currently reserved for this store (#81). A leftover
    JASON_DREAM=1 in some shell does not unlock writes."""
    ledger = cfg.get('ledger') or {}
    if ledger.get('foreground_writes', True) is not False:
        return True
    owner = os.environ.get('JASON_DREAM_OWNER')
    if os.environ.get('JASON_DREAM') != '1' or not owner or not roots or 'project' not in roots:
        return False
    state = storelib.safe(Path(roots['project']) / '.raw/dream.json', Path(roots['project']))
    try:
        running = (read_json(state).get('running') or {}) if state.exists() else {}
    except (OSError, ValueError, AttributeError):
        return False
    return running.get('owner') == owner


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


def check_write_paths(root, names):
    paths = {}
    for name in names:
        path = note_path(root, name)
        if path.resolve().relative_to(root).as_posix() != name:
            raise ValueError('Use the canonical relative note path: ' + name)
        if paths.setdefault(path, name) != name:
            raise ValueError('Note paths refer to the same file: ' + paths[path] + ', ' + name)


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


def journal_error(path, reason):
    """A journal that cannot be replayed blocks every command; say which file and how to get out (#79)."""
    return ValueError(reason + ': ' + str(path) + '. It records an interrupted save that cannot be replayed. '
                      'Back it up, move it out of the memory folder, then retry; the notes on disk stay as they '
                      'are and the raw ledger is not affected. Manual review required.')


def recover(root, _prepared=None):
    journal = storelib.safe(root / '.transaction.json', root)
    if not journal.exists():
        return
    try:
        tx = read_json(journal)
    except (OSError, ValueError):
        raise journal_error(journal, 'Unreadable transaction journal') from None
    if (not isinstance(tx, dict) or set(tx) != {'id', 'notes', 'before', 'remove', 'index'} or
            not isinstance(tx['id'], str) or len(tx['id']) != 32 or
            any(c not in '0123456789abcdef' for c in tx['id'])):
        raise journal_error(journal, 'Invalid transaction journal')
    if not isinstance(tx['notes'], dict) or not isinstance(tx['before'], dict) or not isinstance(tx['remove'], list):
        raise ValueError('Invalid transaction payload')
    check_write_paths(root, [*tx['notes'], *tx['before'], *tx['remove']])
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


def recover_global_rebuild(home, prepared=None):
    journal = storelib.safe(home / '.rebuild-transaction.json', home)
    if not journal.exists():
        return
    # The batch contains at most one normal-sized transaction for each scope.
    if journal.stat().st_size > 2 * MAX_BYTES + 256:
        raise journal_error(journal, 'Oversized global rebuild journal')
    try:
        batch = json.loads(journal.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise journal_error(journal, 'Unreadable global rebuild journal') from None
    if (not isinstance(batch, dict) or set(batch) != {'project', 'transactions'} or
            not isinstance(batch['project'], str) or len(batch['project']) != 64 or
            any(c not in '0123456789abcdef' for c in batch['project']) or
            not isinstance(batch['transactions'], dict) or
            not batch['transactions'].keys() <= {'global', 'project'}):
        raise journal_error(journal, 'Invalid global rebuild journal')
    roots = {'global': home / 'memory/shared', 'project': home / 'memory/projects' / batch['project']}
    for scope, tx in batch['transactions'].items():
        root = storelib.safe(roots[scope], home)
        pending = storelib.safe(root / '.transaction.json', root)
        if pending.exists() and read_json(pending) != tx:
            raise ValueError('Conflicting transaction during global rebuild recovery')
        storelib.atomic_write(pending, json.dumps(tx, ensure_ascii=False))
        recover(root, _prepared=tx if batch == prepared else None)
    journal.unlink()


def commit_global_rebuild(home, roots, plans):
    """Publish both prepared scopes as one recoverable operation under the shared writer lock."""
    transactions = {}
    for scope, plan in plans.items():
        tx, _, _, changed, index_changed = prepare_apply(roots[scope], plan, scope, roots)
        if changed or index_changed:
            transactions[scope] = tx
    if transactions:
        batch = {'project': roots['project'].name, 'transactions': transactions}
        storelib.atomic_write(home / '.rebuild-transaction.json', json.dumps(batch, ensure_ascii=False))
        recover_global_rebuild(home, prepared=batch)


@contextlib.contextmanager
def opened(config, project=None):
    cfg, project, lock_root, roots = configuration(config, project)
    with storelib.locked(lock_root):
        if cfg['mode'] == 'global':
            recover_global_rebuild(lock_root)
        for root in roots.values():
            storelib.safe(root, lock_root)
            root.mkdir(parents=True, exist_ok=True)
            index = storelib.safe(root / 'MEMORY.md', lock_root)
            missing = not index.exists()
            if not missing and quarantine_index(root):
                missing = True  # Rebuilt from the notes below (#93).
            if missing:
                storelib.atomic_write(index, (BASE / 'templates/MEMORY.md').read_text(encoding='utf-8-sig'))
            storelib.recover(root)
            recover(root)
            repair(root, missing)
        yield cfg, project, roots


def quarantine_oversized(root):
    """A note over the reading cap would fail every command for the whole store (#77). Move it,
    byte for byte, to archive/quarantine/ so the store keeps working and the content stays reviewable."""
    moved = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != 'archive' and not d.startswith('.'))
        for filename in sorted(files):
            path = Path(folder) / filename
            if (path.suffix.casefold() != '.md' or path.name.startswith('.') or path.name.casefold() == 'memory.md'
                    or path.is_symlink() or path.stat().st_size <= storelib.doctor.NOTE_READ_CAP):
                continue
            rel = path.relative_to(root).as_posix()
            target = storelib.safe(root / 'archive/quarantine' / uuid.uuid4().hex / rel, root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            if target.stat().st_size != path.stat().st_size:
                raise ValueError('Could not quarantine oversized note: ' + str(path))
            path.unlink()
            moved.append((rel, target))
    if moved:
        manifest = storelib.safe(root / 'archive/MEMORY.md', root)
        links = sorted(p.relative_to(manifest.parent).as_posix() for p in manifest.parent.rglob('*.md') if p != manifest)
        storelib.atomic_write(manifest, '# 歷史資料，不作現行要求\n\n' + ''.join('- [' + p + '](' + p + ')\n' for p in links))
    return moved


def quarantine_index(root):
    """An index over the reading cap fails every command and the hook (#93). The index is derived from the
    notes, so move it byte for byte to archive/quarantine/ and let repair() rebuild it."""
    index = root / 'MEMORY.md'
    if index.is_symlink() or index.stat().st_size <= storelib.doctor.INDEX_READ_CAP:
        return False
    target = storelib.safe(root / 'archive/quarantine' / uuid.uuid4().hex / 'MEMORY.md', root)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(index, target)
    if target.stat().st_size != index.stat().st_size:
        raise ValueError('Could not quarantine oversized index: ' + str(index))
    index.unlink()
    print('Jason-memory: MEMORY.md was over the ' + str(storelib.doctor.INDEX_READ_CAP // (1024 * 1024)) + ' MiB '
          'reading limit; moved it to ' + str(target) + ' and rebuilt the index from the notes', file=sys.stderr)
    return True


def repair(root, index_missing=False):
    """Self-heal states the runtime can fix from what is on disk, and say so on stderr."""
    moved = quarantine_oversized(root)
    for rel, target in moved:
        print('Jason-memory: note over the ' + str(storelib.doctor.NOTE_READ_CAP // (1024 * 1024)) + ' MiB reading '
              'limit was moved out of the active notes: ' + rel + ' -> ' + str(target), file=sys.stderr)
    if not (moved or index_missing):
        return
    try:
        data = notes(root)
        rebuilt = index_for(data, (root / 'archive/MEMORY.md').exists()) if data or moved else None
    except ValueError as exc:
        print('Jason-memory: index could not be rebuilt automatically: ' + str(exc), file=sys.stderr)
        return
    if rebuilt is not None and read_index(root) != rebuilt:
        storelib.atomic_write(root / 'MEMORY.md', rebuilt)
        print('Jason-memory: MEMORY.md was ' + ('missing' if index_missing else 'out of date') + '; rebuilt it from '
              + str(len(data)) + ' note(s): ' + str(root / 'MEMORY.md'), file=sys.stderr)


def index_drift(data, index, archived):
    """Index rows that no longer match the notes they point to - a note edited outside the runtime
    (#86). Reported, not repaired: the mismatch is the evidence, and the next save rewrites the index."""
    try:
        expected = {line for line in index_for(data, archived).splitlines() if line.startswith('- [')}
    except ValueError:
        return ''  # Invalid notes are reported by doctor/audit already.
    actual = {line for line in index.splitlines() if line.startswith('- [')}
    if expected == actual:
        return ''
    names = sorted({re.search(r'\]\(([^)]+)\)', line).group(1) for line in expected ^ actual
                    if re.search(r'\]\(([^)]+)\)', line)})
    return ('index out of sync with notes (edited outside the runtime?): ' + ', '.join(names[:20])
            + (' (+' + str(len(names) - 20) + ' more)' if len(names) > 20 else '')
            + '; the next save rewrites the index from the notes')


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
        drift = index_drift(data, index, (root / 'archive/MEMORY.md').exists())
        if drift and 'out of sync with notes' not in report['structural_errors']:
            report['structural_errors'] = (report['structural_errors'] + '\n' + drift).strip()
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
    try:
        receipt = read_json(storelib.safe(root / '.receipts' / (receipt_id + '.json'), root))
    except FileNotFoundError:
        receipt = None  # Expired, from another turn or cleaned up: no evidence, not a disk fault (#92).
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


def prepare_apply(root, plan, scope, sources=None):
    if not isinstance(plan, dict) or set(plan) != {'expected_revision', 'updates', 'retire'}:
        raise ValueError('Plan needs expected_revision, updates (path: content), retire (paths)')
    old = notes(root)
    old_index = read_index(root)
    if plan['expected_revision'] != revision_for(old, old_index):
        raise ValueError('Revision conflict: refresh context and rebase all related corrections')
    final = dict(old)
    if not isinstance(plan['updates'], dict) or not isinstance(plan['retire'], list):
        raise ValueError('Invalid updates/retire plan')
    check_write_paths(root, [*old, *plan['updates'], *plan['retire']])
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
        if old.get(name) != content:
            if memory_facts.note_has_credential(content):
                raise ValueError(SECRET_ERROR + name)
            # Every newly cited source must resolve to a verified raw record (#33, #36).
            added = note_source_refs(content) - note_source_refs(old.get(name, ''))
            if added:
                check_sources(sorted(added), sources or default_sources(root, scope))
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
    if len(json.dumps(tx, ensure_ascii=False).encode('utf-8')) > MAX_BYTES:
        raise ValueError('Transaction exceeds size limit')
    index_changed = old_index != index
    return tx, checks, facts, changed, index_changed


def apply(root, plan, scope='project', sources=None):
    tx, checks, facts, changed, index_changed = prepare_apply(root, plan, scope, sources)
    final, index = tx['notes'], tx['index']
    if changed or index_changed:
        storelib.atomic_write(root / '.transaction.json', json.dumps(tx, ensure_ascii=False))
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


def remember(root, subject, key, value, why, scope='project', kind=None, roots=None):
    """Single-call add/verify. Corrections retain the explicit batch review path."""
    if not all(isinstance(v, str) and v.strip() for v in (subject, key, value, why)):
        raise ValueError('remember requires nonempty --subject, --key, --value and --why')
    if any('\n' in v or '\r' in v or '```' in v for v in (subject, key, value, why)):
        raise ValueError('remember accepts single-line facts; use apply for complex notes')
    explicit = kind is not None
    kind = kind or 'feedback'
    if kind not in NOTE_TYPES:
        raise ValueError('--type must be one of: ' + ', '.join(NOTE_TYPES))
    if scope == 'global' and kind == 'project':
        raise ValueError('Project notes require project scope')
    if memory_facts.note_has_credential(' '.join((subject, key, value, why))):
        raise ValueError(SECRET_ERROR + 'remember --value/--why')
    subject, key = memory_facts.canonical(subject), memory_facts.canonical(key)
    data = notes(root)
    matches, retype = [], []
    wanted = memory_facts.canonical(value) if key == 'occupation' else value
    holders = sorted(name for name, content in data.items()
                     if any((s, p) == (subject, key) for s, p, _ in memory_facts.extract(content)[0]))
    for name, content in data.items():
        facts, _ = memory_facts.extract(content)
        for s, p, v in facts:
            if (s, p) == (subject, key):
                if v != wanted:
                    # Name every note holding this fact: correcting only one is rejected as a conflict (#76).
                    raise ValueError('Existing fact differs: use context and apply to batch-correct related notes in '
                                     'ONE plan, preserving other facts. Notes holding ' + subject + '/' + key + ': '
                                     + ', '.join(holders))
                fm, _, _ = storelib.doctor._frontmatter(content)
                # A single-fact note filed under the wrong type is moved, not duplicated.
                if explicit and fm and fm.get('type') != kind and len(facts) == 1:
                    retype.append(name)
                else:
                    matches.append(name)
    if matches and not retype:
        upgraded = upgrade_sources(root, data, sorted(set(matches)), why, scope, roots)
        if upgraded is not None:
            return upgraded
        return verify(root, sorted(set(matches)))
    prefix = 'preference' if kind == 'feedback' else kind
    slug = prefix + '-' + hashlib.sha256((subject + '\0' + key).encode('utf-8')).hexdigest()[:16]
    name = kind + '/' + slug + '.md'
    if name in data and name not in retype:
        raise ValueError('Existing note path needs manual batch review; do not overwrite: ' + name)
    today = date.today().isoformat()
    created = today
    for old_name in retype:
        fm, _, _ = storelib.doctor._frontmatter(data[old_name])
        created = min(created, str((fm or {}).get('created') or today))
    # JSON strings are valid quoted YAML scalar values; no hand-written metadata required.
    # The index row is a pointer; the full value stays in the note body, so a long value cannot grow the
    # index without bound (#93).
    description = json.dumps(value if len(value) <= DESCRIPTION_CAP else value[:DESCRIPTION_CAP - 1] + '…',
                             ensure_ascii=False)
    content = ('---\nname: ' + slug + '\ndescription: ' + description + '\ntype: ' + kind + '\n'
               'created: ' + created + '\nupdated: ' + today + '\n---\n\n' + value + '\n\nWhy: ' + why +
               '\n\nHow to apply: ' + ('全局共用' if scope == 'global' else '本專案') + '；' + value +
               '\n\n```jason-facts\n' + json.dumps([{'subject': subject, 'predicate': key, 'value': value}], ensure_ascii=False) + '\n```\n')
    index = read_index(root)
    plan = {'expected_revision': revision_for(data, index), 'updates': {name: content},
            'retire': sorted(set(retype) - {name})}
    result = apply(root, plan, scope, roots)
    result['saved_fact'] = {'subject': subject, 'predicate': key, 'value': value, 'type': kind}
    return result


DESCRIPTION_CAP = 160
# Anything shaped like a reference, including too-long or too-short parts (#35); the ledger's
# `source` parser then decides, so remember/apply and `source --ref` agree on what is valid.
REF_LIKE = re.compile(r'(?<![0-9A-Za-z_])(?:local|[0-9a-f]{8,32}):\d+:[0-9a-f]{6,}(?![0-9A-Za-z_])')
WHY_LINE = re.compile(r'^Why: .*$', re.M)


# Text cited as a ledger source (`ledger X:N:Y`, `來源：ledger ...`): validated even when its store
# or hash part is malformed - 5 characters, non-hex - so a bad citation cannot slip past (#52).
_REF_PART = r'[^\s`*_<>\[\]()（）"\'「」【】《》，。、；;,:：|!?！？]+'
# Markdown and quote wrappers (`**`, `*`, `_`, backticks, <>, quotes, 「」【】) are not part of the
# reference: a valid one wrapped in them is accepted, an invalid one still fails (#55).
EXPLICIT_REF = re.compile(r'(?i)\bledger\s+[`*_<\["\'「【《]*(' + _REF_PART + ':' + _REF_PART + ':' + _REF_PART + ')')
LOOSE_REF = re.compile(r'(?<![0-9A-Za-z_])(?:local|[0-9a-f]{8,32}):\d+:[0-9A-Za-z]+(?![0-9A-Za-z_])', re.I)


# A line that presents a source: there every reference-shaped text is checked, malformed or not.
SOURCE_LABEL = re.compile(r'(?i)來源|出處|出处|\bsources?\b|\bledger\b|\bref\b')
URL = re.compile(r'(?i)\b[a-z][a-z0-9+.\-]*://\S+')


def _cited(ref):
    """Outside a source label, only text with the exact shape the ledger writes is a reference (#89):
    `local:...`, or a 16-hex store with a 12-hex record hash. A git SHA prefix such as
    `abc1234:42:def5678` in an ordinary sentence is content, not a citation."""
    store, _, digest = ref.split(':')
    return (store.lower() == 'local'
            or (len(store) == 16 and len(digest) == 12 and re.fullmatch(r'[0-9a-f]+', store + digest) is not None))


def note_source_refs(content):
    # Keep inline code citations; skip literal blocks and comments using the note parser (#42).
    active = storelib.doctor._active_markdown(content, keep_inline=True)
    refs = set()
    for line in active.splitlines():
        labelled = SOURCE_LABEL.search(line) is not None
        loose = URL.sub(' ', line) if not labelled else line  # A URL path is never a citation (#89).
        found = set(REF_LIKE.findall(loose)) | set(LOOSE_REF.findall(loose))
        refs |= found if labelled else {ref for ref in found if _cited(ref)}
    refs |= {ref.rstrip('.') for ref in EXPLICIT_REF.findall(active)}
    return refs


def default_sources(root, scope):
    """Stores a reference may point into when the caller did not pass the opened roots."""
    root = Path(root)
    if scope == 'global':
        return {'global': root}
    if root.parent.name == 'projects' and (root.parent.parent / 'shared').is_dir():
        return {'global': root.parent.parent / 'shared', 'project': root}  # Global edition.
    return {'project': root}


def check_sources(refs, roots):
    """Every new reference must resolve to a verified raw record before it is published (#33, #36)."""
    import memory_ledger  # Lazy: memory_ledger imports this module.
    for ref in refs:
        try:
            memory_ledger.source(roots, ref)
        except (OSError, ValueError) as error:
            raise ValueError('Source reference does not resolve: ' + ref + ' (' + str(error) + '). Nothing was '
                             'saved; an existing note keeps its current source. If this text is not a ledger citation, '
                             'write it without the STORE:SEQ:HASH shape or away from a 來源/Source/ledger label (#89)')


def upgrade_sources(root, data, names, why, scope, roots=None):
    """Same fact re-sent with a full source reference (#32): replace a legacy `ledger seq N` Why
    line so the note can be traced across projects. Notes that already cite every new reference
    are left untouched (idempotent). New references are verified against the raw ledger first
    (#33); notes holding several facts are never rewritten, so another fact's source is not lost
    (#34) - batch apply migrates those. Returns the apply result, or None when nothing changes."""
    refs = note_source_refs(why)
    if not refs:
        return None
    candidates = []
    for name in names:
        content = data[name]
        if refs <= note_source_refs(content):
            continue
        if len(WHY_LINE.findall(content)) != 1 or len(memory_facts.extract(content)[0]) != 1:
            continue  # Hand-written or multi-fact note: keep it; batch apply can migrate it.
        candidates.append(name)
    if not candidates:
        return None
    updates = {}
    for name in candidates:
        content = data[name]
        updated = WHY_LINE.sub(lambda _: 'Why: ' + why, content, count=1)
        updated = re.sub(r'^updated: .*$', 'updated: ' + date.today().isoformat(), updated, count=1, flags=re.M)
        updates[name] = updated
    if not updates:
        return None
    result = apply(root, {'expected_revision': revision_for(data, read_index(root)),
                          'updates': updates, 'retire': []}, scope, roots)  # apply verifies sources.
    result['source_upgraded'] = sorted(updates)
    return result


def bounded_index(roots, budget=INDEX_BUDGET):
    """Small navigation view for a new session: index lines only, never note bodies."""
    result = {}
    for scope, root in roots.items():
        text = read_index(root)
        raw = text.encode('utf-8')
        truncated = len(raw) > budget
        rows = sum(1 for line in text.splitlines() if line.startswith('- ['))
        if truncated:
            text = raw[:budget].decode('utf-8', 'ignore').rsplit('\n', 1)[0] + '\n'
        entry = {'root': str(root), 'revision': revision(root), 'index': text,
                 'truncated': truncated, 'bytes': len(text.encode('utf-8'))}
        if truncated:
            # Say what is missing and how to get it, not only that something is (#82).
            entry['rows_not_shown'] = rows - sum(1 for line in text.splitlines() if line.startswith('- ['))
            entry['full_list'] = 'rerun index with --full'
        result[scope] = entry
    return result


def export(roots, output):
    """Write every scope's index and active notes to one UTF-8 Markdown file (#27).
    Python writes the file itself, so no console or PowerShell pipe can turn Chinese into '?'."""
    # Collapse '..' and follow symlink/junction aliases of the parent BEFORE the guard (#29).
    output = Path(os.path.abspath(output))
    if output.exists() or output.is_symlink():
        raise ValueError('Export target already exists; choose a new file: ' + str(output))
    target = output.parent.resolve() / output.name
    guarded = [Path(root).resolve() for root in roots.values()]
    if 'global' in roots:
        guarded.append(Path(roots['global']).resolve().parent)  # Every store under <home>/memory.
    for root in guarded:
        if target == root or root in target.parents:
            raise ValueError('Export target must be outside the memory store: ' + str(target))
    output = target
    labels = {'global': '全局共用記憶', 'project': '本專案記憶'}
    parts = ['# 記憶匯出（Jason-memory）\n\n匯出日期：' + date.today().isoformat() +
             '。只含索引與現行筆記，不含封存、收據與原始帳本。\n']
    count = 0
    for scope, root in sorted(roots.items(), key=lambda item: item[0] != 'global'):
        data = notes(root)
        count += len(data)
        parts.append('\n## ' + labels.get(scope, scope) + '（' + str(len(data)) + ' 則）\n\n'
                     '來源：`' + str(root) + '`\n\n### 索引\n\n' + read_index(root).strip() + '\n')
        for name, content in sorted(data.items()):
            parts.append('\n### ' + name + '\n\n' + content.strip() + '\n')
    text = ''.join(parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(text)
    return {'output': str(output), 'notes': count, 'bytes': len(text.encode('utf-8'))}


def read_notes(root, names, budget=MAX_BYTES):
    """Read only the selected notes (no receipt, no write)."""
    if not names:
        raise ValueError('read requires at least one --note relative/path.md')
    contents, used = {}, 0
    for name in names:
        path = note_path(root, name)
        if path.stat().st_size > storelib.doctor.NOTE_READ_CAP:
            raise ValueError('Note exceeds reading limit: ' + name)
        text = path.read_text(encoding='utf-8-sig')
        used += len(text.encode('utf-8'))
        if used > budget:
            raise ValueError('Selected notes exceed --max-bytes; read fewer notes')
        contents[name] = text
    return {'root': str(root), 'contents': contents, 'bytes': used}


def source_roots(cfg, project, roots):
    """Rebuild staging writes to an empty copy; references are verified against the real ledgers."""
    if cfg.get('source_config'):
        return configuration(cfg['source_config'], project)[3]
    return roots


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
    parser.add_argument('command', choices=['context', 'audit', 'apply', 'verify', 'remember', 'index', 'read', 'export'])
    parser.add_argument('--scope', choices=['project', 'global'], default=None,
                        help='default project; index/export without --scope cover every store')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--note', action='append', default=[])
    parser.add_argument('--subject')
    parser.add_argument('--key')
    parser.add_argument('--value')
    parser.add_argument('--why')
    parser.add_argument('--type', dest='kind', choices=NOTE_TYPES, default=None,
                        help='user=identity/role, feedback=preference/rule, project=project constraint, reference=pointer')
    parser.add_argument('--max-bytes', type=int, default=None)
    parser.add_argument('--full', action='store_true', help='index: every row, no navigation budget (#82)')
    parser.add_argument('--output', type=Path, help='export: new UTF-8 Markdown file to create')
    args = parser.parse_args()
    explicit_scope = args.scope is not None
    args.scope = args.scope or 'project'
    try:
        with opened(args.config, args.project) as (cfg, project, roots):
            if args.command in ('index', 'export') and explicit_scope:
                # Same meaning as read/verify (#88): never answer for another store.
                if args.scope not in roots:
                    raise ValueError('Unavailable scope')
                roots = {args.scope: roots[args.scope]}
            if args.command in ('apply', 'remember') and not foreground_writes_allowed(cfg, roots):
                raise ValueError(BACKGROUND_ERROR)
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
                result = apply(roots[args.scope], plan, args.scope, source_roots(cfg, project, roots))
            elif args.command == 'remember':
                if args.scope not in roots:
                    raise ValueError('Unavailable scope')
                sources = source_roots(cfg, project, roots)
                result = remember(roots[args.scope], args.subject, args.key, args.value, args.why, args.scope, args.kind,
                                  sources)
            elif args.command == 'verify':
                if args.scope not in roots:
                    raise ValueError('Unavailable scope')
                result = verify(roots[args.scope], args.note)
            elif args.command == 'index':
                budget = storelib.doctor.INDEX_READ_CAP if args.full else (args.max_bytes or INDEX_BUDGET)
                result = {'project': str(project), 'stores': bounded_index(roots, budget)}
            elif args.command == 'export':
                if args.output is None:
                    raise ValueError('export requires --output FILE.md')
                result = dict(export(roots, args.output), scopes=sorted(roots))
            elif args.command == 'read':
                if args.scope not in roots:
                    raise ValueError('Unavailable scope')
                result = read_notes(roots[args.scope], args.note, args.max_bytes or MAX_BYTES)
            else:
                result = {'project': str(project), 'stores': context(roots)}
        emit_json(result)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        message = str(exc)
        if getattr(exc, 'winerror', None) == 206:
            # "File name too long" is a Windows path limit, not damage or a missing install (#99).
            message += (' - Windows limits paths to 260 characters unless long paths are enabled: move the project '
                        'to a shorter folder or enable LongPathsEnabled')
        emit_json({'error': message}, sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
