#!/usr/bin/env python3
"""Shared local Markdown memory. No network or Git; serialized, checked writes."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
ASSETS = HERE if (HERE / 'tools/jason_doctor.py').is_file() else HERE.parent
spec = importlib.util.spec_from_file_location('jason_doctor', ASSETS / 'tools/jason_doctor.py')
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)
check_spec = importlib.util.spec_from_file_location('jason_check', ASSETS / 'tools/jason_check.py')
check = importlib.util.module_from_spec(check_spec)
check_spec.loader.exec_module(check)
facts_spec = importlib.util.spec_from_file_location('memory_facts', ASSETS / 'tools/memory_facts.py')
facts = importlib.util.module_from_spec(facts_spec)
facts_spec.loader.exec_module(facts)


def safe(path, root):
    if path.is_symlink() or path.resolve() != path:
        raise ValueError('Redirected memory path is not supported: ' + str(path))
    path.resolve().relative_to(root)
    return path


@contextlib.contextmanager
def locked(home):
    home.mkdir(parents=True, exist_ok=True)
    path = safe(home / '.writer.lock', home)
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        deadline = time.monotonic() + 10
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError('Memory busy; retry later. No lock was stolen.')
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_write(path, text):
    safe(path, path.parent)
    fd, temp = tempfile.mkstemp(prefix='.write-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def slug_check(slug):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug) or slug == 'memory' or len(slug) > 100:
        raise ValueError('Use a lowercase kebab-case note slug, excluding memory')


def validate(store, slug, content, index, retire=False):
    if (store / '.transaction.json').exists():
        raise ValueError('Pending batch transaction: use memory_runtime.py context to recover first')
    slug_check(slug)
    if len(content.encode('utf-8')) > doctor.NOTE_READ_CAP:
        raise ValueError('Note exceeds validation limit')
    fm, problems, _ = doctor._frontmatter(content)
    if not fm or problems or fm.get('name') != slug:
        raise ValueError('Invalid frontmatter or name does not match slug')
    with tempfile.TemporaryDirectory(prefix='jason-validate-') as temporary:
        stage = Path(temporary).resolve()
        for path in store.glob('*.md'):
            safe(path,store)
            if retire and path.name == slug + '.md':
                continue
            (stage / path.name).write_bytes(path.read_bytes())
        if not retire:
            atomic_write(stage / (slug + '.md'),content)
        archive_index = safe(store / 'archive/MEMORY.md',store)
        if archive_index.exists() or retire:
            (stage / 'archive').mkdir()
            atomic_write(stage / 'archive/MEMORY.md',archive_index.read_text(encoding='utf-8')
                         if archive_index.exists() else '# Archived memories\n')
        atomic_write(stage / 'MEMORY.md',index)
        report = facts.audit({p.name: p.read_text(encoding='utf-8-sig') for p in stage.glob('*.md')
                              if p.name != 'MEMORY.md'})
        if report['conflicts'] or report['invalid_facts']:
            raise ValueError('Fact conflict: use versioned batch apply to correct all related notes')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = doctor.main(['doctor',str(stage)])
            size_code = check.main(['check',str(stage / 'MEMORY.md')])
        if code or size_code:
            raise ValueError('Memory validation failed: ' + output.getvalue().strip())
    return fm, output.getvalue().strip()


def recover(store):
    journal = safe(store / '.pending.json',store)
    if not journal.exists():
        return
    # JSON can expand one control character to six bytes (e.g. \u0000).
    if journal.stat().st_size > 6 * (doctor.INDEX_READ_CAP + doctor.NOTE_READ_CAP) + 4096:
        raise ValueError('Oversized recovery journal; manual review required')
    pending = json.loads(journal.read_text(encoding='utf-8'))
    if not isinstance(pending,dict) or set(pending) not in ({'slug','content','index'}, {'slug','content','index','archive'}) or not all(isinstance(v,str) for v in pending.values()):
        raise ValueError('Invalid recovery journal; manual review required')
    retired = 'archive' in pending
    validate(store, pending['slug'], pending['content'], pending['index'], retire=retired)
    path = safe(store / (pending['slug'] + '.md'),store)
    if retired:
        expected_archive = pending['slug'] + '-' + digest(pending['content']) + '.md'
        if pending['archive'] != expected_archive:
            raise ValueError('Invalid archive recovery target')
        archive_dir = safe(store / 'archive',store)
        archive_dir.mkdir(exist_ok=True)
        archive = safe(archive_dir / expected_archive,store)
        atomic_write(archive,pending['content'])
        manifest = '# 封存記憶（歷史資料，不作現行規範）\n\n'
        for archived in sorted(archive_dir.glob('*.md')):
            safe(archived,store)
            if archived.name != 'MEMORY.md':
                manifest += '- [' + archived.stem + '](' + archived.name + ')\n'
        atomic_write(archive_dir / 'MEMORY.md',manifest)
        if path.exists():
            path.unlink()
    else:
        atomic_write(path,pending['content'])
    atomic_write(store / 'MEMORY.md',pending['index'])
    journal.unlink()


def stores(home, project):
    project = Path(project).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise ValueError('Project root must be an existing directory')
    key = hashlib.sha256(os.path.normcase(str(project)).encode('utf-8')).hexdigest()
    paths = {'global': home / 'memory/shared', 'project': home / 'memory/projects' / key}
    template = (ASSETS / 'templates/MEMORY.md').read_text(encoding='utf-8-sig')
    for path in paths.values():
        safe(path,home)
        path.mkdir(parents=True,exist_ok=True)
        index = safe(path / 'MEMORY.md',home)
        if not index.exists():
            atomic_write(index,template)
        recover(path)
    return paths,project


def digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def read_note(store,slug):
    slug_check(slug)
    path = safe(store / (slug + '.md'),store)
    if path.stat().st_size > doctor.NOTE_READ_CAP:
        raise ValueError('Note exceeds reading limit')
    content = path.read_text(encoding='utf-8-sig')
    return {'path':str(path),'content':content,'sha256':digest(content)}


def save_note(store,slug,content,summary,expected):
    slug_check(slug)
    if not summary or len(summary) > 300 or any(ord(c) < 32 for c in summary):
        raise ValueError('Summary must be one short nonempty line')
    path = safe(store / (slug + '.md'),store)
    old = path.read_text(encoding='utf-8-sig') if path.exists() else None
    if old is not None and old != content and expected != digest(old):
        raise ValueError('Revision conflict: read current note and supply --expected-sha before updating')
    if old is None and expected:
        raise ValueError('Revision conflict: note no longer exists')
    original = (store / 'MEMORY.md').read_text(encoding='utf-8-sig')
    # Validate even duplicates: a damaged index must not be reported healthy.
    fm, problems, _ = doctor._frontmatter(content)
    if not fm or problems:
        raise ValueError('Invalid note frontmatter')
    lines = [line for line in original.splitlines() if slug + '.md' not in doctor._index_targets(line)]
    entry = '- [' + slug + '](' + slug + '.md) — ' + summary
    heading = '## ' + fm.get('type','')
    try:
        position = lines.index(heading) + 1
    except ValueError:
        lines.extend(['',heading])
        position = len(lines)
    lines.insert(position,entry)
    index = '\n'.join(lines) + '\n'
    if old == content and original.splitlines().count(entry) == 1:
        index = original
    _, checks = validate(store,slug,content,index)
    changed = old != content or original != index
    if changed:
        pending = {'slug':slug,'content':content,'index':index}
        atomic_write(store / '.pending.json',json.dumps(pending,ensure_ascii=False))
        # Replayable two-file commit; subsequent operations finish an interrupted write.
        recover(store)
    return {'path':str(path),'sha256':digest(content),'changed':changed,'checks':checks,
            'index_lines':len(index.splitlines()),'index_bytes':len(index.encode('utf-8'))}


def retire_note(store,slug,expected):
    current = read_note(store,slug)
    if current['sha256'] != expected:
        raise ValueError('Revision conflict: read current note before retiring')
    original = (store / 'MEMORY.md').read_text(encoding='utf-8-sig')
    index = '\n'.join(line for line in original.splitlines()
                      if slug + '.md' not in doctor._index_targets(line)) + '\n'
    if 'archive/MEMORY.md' not in doctor._index_targets(index):
        index += '\n- [封存記憶](archive/MEMORY.md) — 歷史資料，不作現行規範\n'
    _, checks = validate(store,slug,current['content'],index,retire=True)
    archive_name = slug + '-' + current['sha256'] + '.md'
    pending = {'slug':slug,'content':current['content'],'index':index,'archive':archive_name}
    atomic_write(store / '.pending.json',json.dumps(pending,ensure_ascii=False))
    recover(store)
    return {'changed':True,'archive_path':str(store / 'archive' / archive_name),
            'checks':checks,'index_lines':len(index.splitlines()),'index_bytes':len(index.encode('utf-8'))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path,default=Path.home() / '.jason-memory-global')
    sub = parser.add_subparsers(dest='command',required=True)
    for command in ('context','read','save','retire'):
        item = sub.add_parser(command)
        item.add_argument('--project',required=True,type=Path)
        if command != 'context':
            item.add_argument('--scope',choices=['global','project'],required=True)
            item.add_argument('--slug',required=True)
        if command == 'save':
            item.add_argument('--input',type=Path,required=True)
            item.add_argument('--summary',required=True)
            item.add_argument('--expected-sha')
        if command == 'retire':
            item.add_argument('--expected-sha',required=True)
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    try:
        with locked(home):
            paths,project = stores(home,args.project)
            if args.command == 'context':
                result = {'home':str(home),'project_root':str(project),
                          'global_store':str(paths['global']),'project_store':str(paths['project']),
                          'global_index':(paths['global'] / 'MEMORY.md').read_text(encoding='utf-8-sig'),
                          'project_index':(paths['project'] / 'MEMORY.md').read_text(encoding='utf-8-sig')}
            elif args.command == 'read':
                result = read_note(paths[args.scope],args.slug)
                result['scope'] = args.scope
            elif args.command == 'retire':
                result = retire_note(paths[args.scope],args.slug,args.expected_sha)
                result['scope'] = args.scope
            else:
                if args.input.stat().st_size > doctor.NOTE_READ_CAP:
                    raise ValueError('Input note exceeds limit')
                content = args.input.read_text(encoding='utf-8-sig')
                fm, _, _ = doctor._frontmatter(content)
                if args.scope == 'global' and fm and fm.get('type') == 'project':
                    raise ValueError('Project notes require --scope project')
                result = save_note(paths[args.scope],args.slug,content,args.summary,args.expected_sha)
                result['scope'] = args.scope
        print(json.dumps(result,ensure_ascii=True))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=True),file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
