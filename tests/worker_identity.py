"""Test helper: act as the background consolidation worker of one store (#81).

Background-mode writes need JASON_DREAM=1 AND the owner token of the job reserved for that store.
This reserves a job exactly like a starting worker would (owner, no pid yet) and removes it after."""
import contextlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import memory_runtime as runtime  # noqa: E402


@contextlib.contextmanager
def as_worker(config, project=None, env=None):
    """Yield the worker environment; `env` (a dict) is updated in place when given, else os.environ."""
    root = Path(runtime.configuration(config, project)[3]['project'])
    state_path = root / '.raw/dream.json'
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'cursor': 0}
    owner = uuid.uuid4().hex
    state['running'] = {'owner': owner, 'kind': 'dream', 'started': time.time(), 'upto': 0, 'pid': None}
    state_path.write_text(json.dumps(state), encoding='utf-8')
    target = os.environ if env is None else env
    saved = {key: target.get(key) for key in ('JASON_DREAM', 'JASON_DREAM_OWNER')}
    target.update(JASON_DREAM='1', JASON_DREAM_OWNER=owner)
    try:
        yield target
    finally:
        for key, value in saved.items():
            if value is None:
                target.pop(key, None)
            else:
                target[key] = value
        current = json.loads(state_path.read_text(encoding='utf-8'))
        if (current.get('running') or {}).get('owner') == owner:
            current.pop('running')
            state_path.write_text(json.dumps(current), encoding='utf-8')
