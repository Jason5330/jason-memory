#!/usr/bin/env python3
"""Append-only raw session ledger, byte-exact document mirror and count-triggered background dreaming.

Layer 1 (raw) is the single source of truth: `.raw/ledger.jsonl` only ever grows and every
record is hash-chained to the previous one, so a session can be replayed and any rewrite is
detectable. Documents are mirrored byte-for-byte into `.raw/blobs/<sha256>` with their source
path and fingerprint recorded in the ledger. The Markdown notes/Wiki are derived from this raw
layer and may be deleted and rebuilt from it.

Dreaming (background consolidation) is triggered by the number of new raw records, not by idle
time. The foreground never waits for it: the hook only starts a detached worker when at least
`dream_every` unconsolidated records exist and no worker is running. The cursor advances only
after the configured command succeeds, so a failure is retried later and records are neither
skipped nor processed twice; with no new records nothing runs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import memory_facts
import memory_runtime as runtime

LEDGER = 'ledger.jsonl'
TEXT_CAP = 64 * 1024  # Per-field cap for conversation text; longer content is truncated and marked.
STALE_SECONDS = 3600
DEFAULT_SETTINGS = {'enabled': False, 'dream_every': 20, 'dream_command': None,
                    'dream_agent': 'auto', 'foreground_writes': True}


def settings(config):
    """Ledger settings from the memory configuration; disabled unless explicitly enabled."""
    cfg = runtime.read_json(Path(config))
    value = cfg.get('ledger') or {}
    if not isinstance(value, dict):
        raise ValueError('ledger configuration must be an object')
    merged = dict(DEFAULT_SETTINGS, **value)
    if not isinstance(merged['dream_every'], int) or merged['dream_every'] < 1:
        raise ValueError('ledger.dream_every must be a positive integer')
    command = merged['dream_command']
    if command is not None and (not isinstance(command, list) or not all(isinstance(c, str) for c in command)):
        raise ValueError('ledger.dream_command must be a list of strings')
    if merged['foreground_writes'] is False and not merged['enabled']:
        raise ValueError('Background memory mode (foreground_writes=false) requires ledger.enabled=true')
    if merged['enabled'] and command is None:
        # Default worker: build the consolidation prompt and hand it to an agent CLI.
        merged['dream_command'] = [sys.executable, str(Path(__file__).resolve().with_name('memory_dream.py')),
                                   '--config', str(Path(config).absolute()), 'consolidate']
    return merged


def raw_root(root):
    folder = runtime.storelib.safe(Path(root) / '.raw', Path(root))
    folder.mkdir(exist_ok=True)
    return folder


def _hash(record):
    body = {k: v for k, v in record.items() if k != 'hash'}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def records(root):
    path = raw_root(root) / LEDGER
    if not path.exists():
        return []
    result = []
    with path.open('rb') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                result.append(json.loads(line.decode('utf-8')))
            except ValueError:
                raise ValueError('Ledger line ' + str(number) + ' is not valid JSON')
    return result


def verify(root):
    """Recompute the hash chain. Any edited, removed or reordered record is reported."""
    previous, count = '0' * 64, 0
    for record in records(root):
        count += 1
        if record.get('seq') != count or record.get('prev') != previous or record.get('hash') != _hash(record):
            raise ValueError('Ledger chain broken at seq ' + str(count))
        previous = record['hash']
    return {'records': count, 'head': previous}


def _clip(value):
    if isinstance(value, str):
        value = memory_facts.redact(value)
        raw = value.encode('utf-8')
        if len(raw) > TEXT_CAP:
            return raw[:TEXT_CAP].decode('utf-8', 'ignore') + '…[truncated ' + str(len(raw)) + ' bytes]'
        return value
    if isinstance(value, dict):
        return {str(k): _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value]
    return value


def append(root, kind, data, session=''):
    """Append one record. Callers must hold the store writer lock (runtime.opened)."""
    if not isinstance(kind, str) or not kind:
        raise ValueError('Ledger record needs a kind')
    path = raw_root(root) / LEDGER
    existing = records(root)
    previous = existing[-1]['hash'] if existing else '0' * 64
    record = {'seq': len(existing) + 1, 'ts_ns': time.time_ns(), 'session': str(session),
              'kind': kind, 'data': _clip(data), 'prev': previous}
    record['hash'] = _hash(record)
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    # O_APPEND: existing bytes are never rewritten.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_BINARY', 0), 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return record


def mirror(root, source, session=''):
    """Copy a document byte-for-byte into the blob store and record its fingerprint."""
    source = Path(source).resolve(strict=True)
    if not source.is_file():
        raise ValueError('mirror needs a regular file')
    content = source.read_bytes()
    if len(content) > runtime.MAX_BYTES:
        raise ValueError('Document exceeds mirror limit')
    digest = hashlib.sha256(content).hexdigest()
    blobs = raw_root(root) / 'blobs'
    blobs.mkdir(exist_ok=True)
    blob = runtime.storelib.safe(blobs / digest, blobs)
    if not blob.exists():
        temp = blob.with_name('.tmp-' + digest)
        with open(temp, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, blob)
    elif hashlib.sha256(blob.read_bytes()).hexdigest() != digest:
        raise ValueError('Existing blob does not match its fingerprint: ' + digest)
    record = append(root, 'document', {'path': str(source), 'sha256': digest, 'size': len(content)}, session)
    return {'blob': str(blob), 'sha256': digest, 'size': len(content), 'seq': record['seq']}


def replay(root, session=None):
    """Events in original order, optionally for one session (raw facts for exact recall)."""
    verify(root)
    return [r for r in records(root) if session is None or r['session'] == str(session)]


def search(root, text, limit=20):
    """Exact-text lookup in the raw layer when a precise quote, number or error is needed."""
    needle = text.casefold()
    hits = [r for r in records(root) if needle in json.dumps(r['data'], ensure_ascii=False).casefold()]
    return hits[-limit:]


# --- Dreaming ----------------------------------------------------------------
# One consolidation job at a time per store. The job (normal dream-run or rebuild) is recorded
# in dream.json with an owner token; only that owner may clear it. A job whose process is gone or
# which is older than STALE_SECONDS no longer blocks new work.

def _state_path(root):
    return raw_root(root) / 'dream.json'


def dream_state(root):
    path = _state_path(root)
    state = runtime.read_json(path) if path.exists() else {}
    state.setdefault('cursor', 0)
    return state


def _save_state(root, state):
    runtime.storelib.atomic_write(_state_path(root), json.dumps(state, ensure_ascii=False))


def pending(root):
    state = dream_state(root)
    total = len(records(root))
    return {'cursor': state['cursor'], 'head': total, 'pending': max(0, total - state['cursor']),
            'running': state.get('running'), 'last_error': state.get('last_error')}


def _alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _running(state):
    run = state.get('running')
    if not run or time.time() - run.get('started', 0) >= STALE_SECONDS:
        return False
    return _alive(run['pid']) if run.get('pid') else True  # No pid yet: the job is still starting.


def claim_job(root, kind, upto):
    """Reserve the store's single consolidation slot. Caller holds the writer lock."""
    state = dream_state(root)
    if _running(state):
        return None
    owner = uuid.uuid4().hex
    state['running'] = {'owner': owner, 'kind': kind, 'started': time.time(), 'upto': upto,
                        'pid': os.getpid() if kind == 'rebuild' else None}
    _save_state(root, state)
    return owner


def release_job(root, owner, upto=None, error=None):
    """Finish a job: advance the cursor on success, record the error otherwise. Only the current
    owner may finish: a stale or replaced worker changes nothing (no cursor, no error, no marker)."""
    state = dream_state(root)
    if (state.get('running') or {}).get('owner') != owner:
        return False
    state.pop('running', None)
    if error is None and upto is not None:
        existing = records(root)
        if upto > state['cursor'] and upto <= len(existing):
            state['cursor'] = upto
            state['cursor_hash'] = existing[upto - 1]['hash']
        state.pop('last_error', None)
        state['last_success'] = time.time()
    elif error is not None:
        state['last_error'] = dict(error, at=time.time(), upto=upto)
    _save_state(root, state)
    return True


def verified_records(root):
    """Records after a full chain check and a check that nothing at or below the consolidated
    cursor was truncated or rewritten (checkpoint stored when the cursor advanced)."""
    verify(root)
    existing = records(root)
    state = dream_state(root)
    cursor = state['cursor']
    if cursor:
        if len(existing) < cursor:
            raise ValueError('Ledger truncated below the consolidated cursor (' + str(cursor) + ')')
        if state.get('cursor_hash') and existing[cursor - 1]['hash'] != state['cursor_hash']:
            raise ValueError('Ledger rewritten below the consolidated cursor (' + str(cursor) + ')')
    return existing


def maybe_dream(root, config, project, opts, spawn=None):
    """Start a detached worker once enough new records exist. Never blocks the foreground.
    Caller holds the writer lock."""
    if not opts.get('enabled') or not opts.get('dream_command'):
        return None
    state = dream_state(root)
    head = len(records(root))
    if head - state['cursor'] < opts['dream_every'] or _running(state):
        return None
    owner = claim_job(root, 'dream', head)
    if owner is None:
        return None
    command = [sys.executable, str(Path(__file__).resolve()), '--config', str(config),
               '--project', str(project), 'dream-run', '--upto', str(head), '--owner', owner]
    try:
        process = (spawn or _spawn)(command)
    except OSError as exc:
        release_job(root, owner, head, {'code': 126, 'error': 'spawn failed: ' + type(exc).__name__})
        return None
    if getattr(process, 'pid', None):
        state = dream_state(root)
        if (state.get('running') or {}).get('owner') == owner:
            state['running']['pid'] = process.pid
            _save_state(root, state)
    return head


def _spawn(command):
    kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
              'close_fds': True}
    if os.name == 'nt':
        kwargs['creationflags'] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    return subprocess.Popen(command, **kwargs)


def dream_run(config, project, upto, opts, owner=None):
    """Run the consolidation command for records (cursor, upto]; advance the cursor only on success.
    Every failure path (bad ledger, missing executable, non-zero exit) clears this job and records
    last_error so the next trigger retries."""
    with runtime.opened(config, project) as (_, _, roots):
        root = roots['project']
        if owner is None:
            owner = claim_job(root, 'dream', upto)
            if owner is None:
                return 75  # Another job is running; try again later.
        elif (dream_state(root).get('running') or {}).get('owner') != owner:
            return 75  # Our reservation was replaced (stale); do not race the new owner.
        start = dream_state(root)['cursor']
        try:
            existing = verified_records(root)
        except ValueError as exc:
            release_job(root, owner, upto, {'code': 65, 'error': str(exc)})
            return 65
        upto = min(upto, len(existing))
    env = dict(os.environ, JASON_DREAM='1', JASON_MEMORY_CONFIG=str(config), JASON_MEMORY_PROJECT=str(project),
               JASON_DREAM_FROM=str(start + 1), JASON_DREAM_UPTO=str(upto), JASON_MEMORY_ROOT=str(root))
    error = None
    if upto > start:
        # The command runs outside the writer lock so foreground recall and saves never wait for it.
        try:
            code = subprocess.call(opts['dream_command'], env=env, cwd=str(project), stdin=subprocess.DEVNULL)
        except OSError as exc:
            code = 127
            error = 'cannot start dream_command: ' + type(exc).__name__
    else:
        code = 0
    with runtime.opened(config, project) as (_, _, roots):
        finished = release_job(root, owner, upto, None if code == 0 else {'code': code, 'error': error or 'exit code'})
        if finished and code == 0:
            # Records that arrived while this worker was busy start the next run now, instead of
            # waiting for another prompt/tool/Stop event. Failures wait for the next event (no hot loop).
            maybe_dream(root, config, project, opts)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--project', type=Path)
    parser.add_argument('command', choices=['append', 'verify', 'replay', 'search', 'mirror',
                                            'pending', 'dream-run'])
    parser.add_argument('--kind')
    parser.add_argument('--data', help='JSON value for append')
    parser.add_argument('--session', default='')
    parser.add_argument('--file', type=Path)
    parser.add_argument('--text')
    parser.add_argument('--upto', type=int)
    parser.add_argument('--owner')
    args = parser.parse_args()
    try:
        if args.command == 'dream-run':
            cfg, project, _, _ = runtime.configuration(args.config, args.project)
            return dream_run(args.config.absolute(), project, args.upto, settings(args.config), args.owner)
        with runtime.opened(args.config, args.project) as (_, _, roots):
            root = roots['project']
            if args.command == 'append':
                result = append(root, args.kind, json.loads(args.data or 'null'), args.session)
            elif args.command == 'verify':
                result = verify(root)
            elif args.command == 'replay':
                result = replay(root, args.session or None)
            elif args.command == 'search':
                result = search(root, args.text or '')
            elif args.command == 'mirror':
                result = mirror(root, args.file, args.session)
            else:
                result = pending(root)
        runtime.emit_json(result)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        runtime.emit_json({'error': str(exc)}, sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
