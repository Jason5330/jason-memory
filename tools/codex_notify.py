#!/usr/bin/env python3
"""Codex `notify` program: record each finished turn in the raw ledger and trigger dreaming.

Add to ~/.codex/config.toml (absolute path to this file):
    notify = ["python", "C:/path/to/jason-memory/tools/codex_notify.py"]
Codex runs it after every agent turn with one JSON argument. The foreground agent does nothing
extra; the ledger append is cheap and consolidation starts detached once enough records exist.
"""
import json
import os
from pathlib import Path
import sys

import memory_ledger as ledger
import memory_runtime as runtime


def find_config(start):
    current = Path(start).resolve()
    for folder in (current, *current.parents):
        candidate = folder / '.jason-memory.json'
        if candidate.is_file():
            return candidate
    return None


def global_config():
    """Installed global edition: <home>/framework/memory-config.json next to this tools folder."""
    candidate = Path(__file__).resolve().parents[1] / 'memory-config.json'
    if candidate.is_file():
        try:
            if runtime.read_json(candidate).get('mode') == 'global':
                return candidate
        except (OSError, ValueError):
            return None
    return None


def handle(payload, cwd=None):
    if not isinstance(payload, dict) or payload.get('type') != 'agent-turn-complete':
        return None
    if os.environ.get('JASON_DREAM') == '1':
        return None  # The background worker's own turns are not user conversation.
    workspace = Path(payload.get('cwd') or cwd or os.getcwd()).resolve()
    config = find_config(workspace)  # An explicit project configuration wins.
    project = None
    if config is None:
        config = global_config()
        project = workspace
    if config is None:
        return None
    opts = ledger.settings(config)
    if not opts['enabled']:
        return None
    session = str(payload.get('thread-id') or payload.get('turn-id') or '')
    with runtime.opened(config, project) as (_, project, roots):
        root = roots['project']
        for message in payload.get('input-messages') or []:
            ledger.append(root, 'prompt', {'text': str(message), 'host': 'codex'}, session)
        ledger.append(root, 'assistant', {'text': str(payload.get('last-assistant-message') or ''),
                                          'host': 'codex'}, session)
        return ledger.maybe_dream(root, config, project, opts)


def main():
    try:
        payload = json.loads(sys.argv[-1]) if len(sys.argv) > 1 else {}
        handle(payload)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('Jason-memory codex notify failed: ' + str(exc), file=sys.stderr)
        return 0  # Never disturb the user's Codex session.


if __name__ == '__main__':
    sys.exit(main())
