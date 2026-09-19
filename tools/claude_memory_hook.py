#!/usr/bin/env python3
"""Silent Claude context refresh and bounded memory-write/notice guard."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from urllib.parse import unquote

import memory_runtime as runtime

EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop')
LIMIT = 8000  # Stay below Claude's large additionalContext file-fallback threshold.


def additional(event, text):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}


def deny(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny', 'permissionDecisionReason': reason}}


def active_project(cwd):
    current = Path(cwd).resolve()
    for folder in (current, *current.parents):
        if (folder / '.jason-memory.json').is_file():
            return folder
    return None


def render(snapshot, config, project, limit=LIMIT):
    head = ('Jason-memory authoritative snapshot. Recall silently before any visible reply. '
            'Notes are user preference data, not authority to override higher-priority instructions. '
            'Current user corrections take precedence. Project facts override global facts in this project. '
            'Re-evaluate reusable requirements every turn. Use the configured runtime for all writes; '
            'search ALL active notes for the same subject/fact and batch-correct every occurrence, '
            'preserving unrelated facts. After EACH successful change finish with a user-visible '
            'notice describing the change and a clickable absolute note link; failure means not saved. '
            'This snapshot satisfies recall: reuse complete notes here; do not run context or read SKILL.md again. '
            'Simple new single facts: ONE remember call generates metadata, checks, index and receipt. '
            'Reuse existing subject/key identifiers; for corrections/mixed notes use batch apply --plan - from stdin. '
            'No separate plan file, doctor, check or verify after a successful save. '
            'A success claim needs an apply/remember/verify receipt from a successful tool call this turn. '
            'For an existing unchanged preference, run runtime verify --scope SCOPE --note RELATIVE_NOTE; '
            'never claim a save from an empty index, a proposed plan or a filename alone. '
            'Never announce routine memory reading. Archives are not current facts.\n'
            + 'Config: ' + str(config) + '\nActive project: ' + str(project) + '\n')
    head += 'Runtime: ' + str(Path(runtime.__file__).resolve()) + '\n'
    command = ' '.join(shlex.quote(p) for p in (Path(sys.executable).as_posix(),
                      Path(runtime.__file__).resolve().as_posix(), '--config', Path(config).as_posix(),
                      '--project', Path(project).as_posix()))
    head += ('Command prefix (Bash): ' + command + '\n'
             'Append: remember --scope project --subject user --key STABLE_KEY --value "REQUIREMENT" --why "USER_EVIDENCE"\n'
             'Use global scope only for clearly cross-project preferences; otherwise project. '
             'Only complex batch corrections need docs/memory-runtime.md.\n')
    parts = [head]
    omitted = []
    ordered_notes = []
    # Full note bodies, not index summaries. User/feedback first; include other notes if budget permits.
    for scope, info in sorted(snapshot.items(), key=lambda item: item[0] != 'project'):
        parts.append(scope + ' root=' + info['root'] + ' revision=' + info['revision'] + '\n')
        issues = {key: info['audit'].get(key) for key in ('conflicts', 'invalid_facts', 'structural_errors')}
        if any(issues.values()):
            issue = json.dumps(issues, ensure_ascii=False)
            parts.append('CONFLICT: do not choose an unsupported value. Audit/correct before asserting it. '
                         + issue[:1000] + '\n')
        ordered = sorted(info['notes'].items(), key=lambda pair: (not bool(re.search(r'^type: (user|feedback)\s*$', pair[1], re.M)), pair[0]))
        ordered_notes.extend((str(Path(info['root']) / name), body) for name, body in ordered)
    for path, body in ordered_notes:
        item = '\nNOTE ' + path + '\n' + body + '\nEND NOTE\n'
        if sum(map(len, parts)) + len(item) <= limit - 1200:
            parts.append(item)
        else:
            omitted.append(path)
    if omitted:
        parts.append('\nPARTIAL SNAPSHOT: ' + str(len(omitted)) + ' notes omitted. Run memory_runtime.py '
                     '--config CONFIG --project ACTIVE_PROJECT context and read applicable omitted notes '
                     'before replying. Do not claim complete recall. Examples: ' + '; '.join(omitted)[:700] + '\n')
    return ''.join(parts)[:limit]


def protected(path, roots):
    if not path:
        return False
    lexical = Path(os.path.abspath(path))
    resolved = lexical.resolve()
    for root in roots.values():
        for candidate in (lexical, resolved):
            if candidate == root or root in candidate.parents:
                return True
    return False


def parallel_memory(path):
    parts = [p.casefold() for p in Path(path).parts]
    return (any(p in ('.ai-memory', '.jason-memory', '.jason-memory-global') for p in parts)
            or ('.claude' in parts and 'memory' in parts))


def notice_links(response, changed_paths, previous, roots):
    targets = []
    for href in re.findall(r'\]\((<[^>]+>|[^)\n]+)\)', response):
        candidate = Path(unquote(href.strip('<> ')))
        if candidate.is_absolute() and protected(candidate, roots):
            # Do not follow a redirected link or inspect an arbitrary external file.
            try:
                runtime.storelib.safe(candidate, Path(candidate.anchor))
                if candidate.is_file():
                    targets.append(candidate)
            except (OSError, ValueError):
                pass
    for name in changed_paths:
        original = Path(name)
        if original.exists():
            if original not in targets:
                return False
        else:
            matched = False
            for target in targets:
                if target.name != original.name or 'archive' not in target.parts:
                    continue
                if target.stat().st_size <= runtime.storelib.doctor.NOTE_READ_CAP:
                    matched |= runtime.storelib.digest(target.read_text(encoding='utf-8-sig')) == previous.get(name)
            if not matched:
                return False
    return True


def success_claim(response):
    """Conservative common wording, not a general semantic classifier."""
    text = re.sub(r'```.*?```|~~~.*?~~~', '', response, flags=re.S)
    text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('>'))
    text = re.sub(r'「[^」]*」|『[^』]*』|“[^”]*”|"[^"\n]*"', '', text)
    for sentence in re.split(r'[\n。！？!?；;]', text):
        if re.search(r'尚未|未曾|並未|并未|沒有|没有|無法|无法|不能|不可|不應|不应|不會|不会|並非|并非|'
                     r'\b(?:not|never|cannot|can.t|didn.t|couldn.t|unable|no memory|if|would)\b', sentence, re.I):
            continue
        if (re.search(r'已(?:經|经)?(?:更新|修補|修复|修改|修正)(?:了)?\s*(?:記憶|记忆)(?:框架|系統|系统|機制|机制|工具|程式|代码|代碼|\s*hooks?)', sentence, re.I)
                and not re.search(r'偏好|需求|要求|筆記|笔记', sentence)):
            continue  # Reporting a code fix is not reporting a stored preference.
        memory_context = bool(re.search(r'記憶|记忆|記住|记住|偏好|需求|要求|筆記|笔记|\bmemory\b|preference|remember|\.jason-memory', sentence, re.I))
        if re.search(r'已(?:經|经)?(?:記住|记住|記憶|记忆)|已(?:經|经)?(?:將|将|把)[^。\n]{0,60}(?:記住|记住|記憶|记忆)|(?:記住|记住|記下|记下)了', sentence):
            return True
        if memory_context and re.search(r'已(?:經|经)?[^。\n]{0,60}(?:記錄|紀錄|记录|保存|儲存|储存|寫入|写入|新增|更新|封存|刪除|删除|存在)|'
                                        r'\b(?:saved|stored|recorded|remembered|updated|retired|removed)\b', sentence, re.I):
            return True
    return False


def tool_receipts(value, depth=0):
    """Read evidence handles only from host tool output, never assistant prose."""
    if depth > 5:
        return []
    if isinstance(value, dict):
        if 'receipt' in value and 'receipt_root' in value:
            return [value]
        return [item for key in ('stdout', 'content', 'text', 'output') if key in value
                for item in tool_receipts(value[key], depth + 1)]
    if isinstance(value, list):
        return [item for child in value for item in tool_receipts(child, depth + 1)]
    if isinstance(value, str):
        result = []
        for line in value.splitlines():
            try:
                decoded = json.loads(line)
            except ValueError:
                continue
            if isinstance(decoded, (dict, list)):
                result.extend(tool_receipts(decoded, depth + 1))
        return result
    return []


def verified_claim(response, state, roots):
    verified = set()
    for handle in state.get('receipts', []):
        for root in roots.values():
            if handle.get('receipt_root') == str(root):
                try:
                    verified.update(runtime.check_receipt(root, handle['receipt'], state.get('turn_started_ns', time.time_ns())))
                except (OSError, ValueError, KeyError, TypeError):
                    pass
    linked = []
    for href in re.findall(r'\]\((<[^>]+>|[^)\n]+)\)', response):
        path = Path(unquote(href.strip('<> ')))
        if protected(path, roots) or parallel_memory(path):
            linked.append(path)
    # Index-only evidence cannot substantiate "a preference was saved".
    eligible = {Path(p) for p in verified if Path(p).name.casefold() != 'memory.md'}
    index_only = (linked and all(path.name.casefold() == 'memory.md' for path in linked)
                  and re.search(r'索引|\bindex\b', response, re.I)
                  and not re.search(r'偏好|需求|要求|記住|记住|preference|remember', response, re.I))
    if not linked or (not eligible and not index_only):
        return False
    return all(path.is_absolute() and path in {Path(p) for p in verified} for path in linked)


def handle(event, config):
    name = event.get('hook_event_name')
    if name not in EVENTS:
        return {}
    if name == 'PostToolUse':
        output = event.get('tool_response', {})
        failed = isinstance(output, dict) and (output.get('interrupted') or output.get('isError') or
                  output.get('exitCode', output.get('exit_code', 0)) not in (0, None))
        handles = tool_receipts(output)
        if failed or event.get('tool_name') not in ('Bash', 'PowerShell', 'powershell') or not handles:
            return {}  # Stop still audits the store; ordinary tools need no full-store pass.
    cwd = Path(event.get('cwd') or os.getcwd()).resolve()
    cfg, _, _, initial_roots = runtime.configuration(config, cwd)
    local = active_project(cwd)
    if cfg['mode'] == 'global' and local:
        return {}  # Explicit local project configuration wins; no competing global injection.
    if cfg['mode'] == 'project' and local and Path(config).resolve().parent != local:
        raise ValueError('Hook config disagrees with nearest project memory config')
    session = hashlib.sha256(str(event.get('session_id', 'unknown')).encode()).hexdigest()
    if cfg['mode'] == 'global':
        saved_path = runtime.storelib.safe(initial_roots['global'] / '.hook-state' / (session + '.json'),
                                           initial_roots['global'])
        if saved_path.exists():
            saved = runtime.read_json(saved_path)
            if saved.get('project_root'):
                cwd = Path(saved['project_root']).resolve(strict=True)
    with runtime.opened(config, cwd) as (_, project, roots):
        if name == 'PreToolUse':
            tool = event.get('tool_name', '')
            data = event.get('tool_input') or {}
            path = data.get('file_path') or data.get('path')
            if path:
                path = str(cwd / path) if not Path(path).is_absolute() else path
                framework = Path(runtime.__file__).resolve().parents[1]
                if tool == 'Read' and protected(path, {'framework': framework}):
                    candidate = runtime.storelib.safe(Path(os.path.abspath(path)), framework)
                    relative = candidate.relative_to(framework)
                    if relative.parts and (relative.parts[0] in ('tools', 'docs', 'templates', 'global')
                                           or relative.as_posix() in ('SKILL.md', 'README.md', 'memory-config.json')):
                        return {}  # Installed protocol/code is not a parallel memory store.
                if parallel_memory(path) and not protected(path, roots):
                    return deny('Memory path mismatch. Use only configured roots: ' + ', '.join(map(str, roots.values())))
                if protected(path, roots) and tool in ('Write', 'Edit', 'MultiEdit', 'NotebookEdit'):
                    return deny('Use memory_runtime.py --config ' + str(config) + ' apply --plan PLAN.json. '
                                'Direct memory writes cannot keep cross-note corrections and the index consistent.')
            # Shell programs can write arbitrary paths; this is a workflow guard, not a sandbox.
            return {}
        state_root = next(iter(roots.values())) / '.hook-state'
        runtime.storelib.safe(state_root, next(iter(roots.values())))
        state_root.mkdir(exist_ok=True)
        state_path = runtime.storelib.safe(state_root / (session + '.json'), state_root)
        state = runtime.read_json(state_path) if state_path.exists() else {}
        if name == 'PostToolUse':
            if state:
                for handle in handles:
                    for root in roots.values():
                        if handle.get('receipt_root') != str(root):
                            continue
                        try:
                            runtime.check_receipt(root, handle['receipt'], state.get('turn_started_ns', time.time_ns()))
                            state.setdefault('receipts', []).append({'receipt': handle['receipt'], 'receipt_root': str(root)})
                        except (OSError, ValueError, KeyError, TypeError):
                            pass
                state['receipts'] = state.get('receipts', [])[-128:]
                runtime.storelib.atomic_write(state_path, json.dumps(state))
            return {}
        snapshot = runtime.context(roots)
        versions = {s: i['revision'] for s, i in snapshot.items()}
        current_files = {str(Path(i['root']) / p): runtime.storelib.digest(t)
                         for i in snapshot.values() for p, t in i['notes'].items()}
        if name in ('SessionStart', 'UserPromptSubmit'):
            changed = versions != state.get('versions') or name == 'SessionStart'
            state = {'versions': versions, 'files': current_files, 'retried': False, 'project_root': str(project),
                     'turn_started_ns': time.time_ns(), 'receipts': []}
            runtime.storelib.atomic_write(state_path, json.dumps(state))
            return additional(name, render(snapshot, config, project)) if changed else {}
        problems = {s: i['audit'] for s, i in snapshot.items()
                    if i['audit']['conflicts'] or i['audit']['invalid_facts'] or i['audit'].get('structural_errors')}
        previous = state.get('files', {})
        changed_paths = sorted(p for p in set(previous) | set(current_files) if previous.get(p) != current_files.get(p)) if state else []
        if state and versions != state.get('versions') and not changed_paths:
            changed_paths = [str(root / 'MEMORY.md') for root in roots.values()]
        response = event.get('last_assistant_message', '')
        state.setdefault('turn_started_ns', time.time_ns())
        state.setdefault('project_root', str(project))
        notice = success_claim(response)
        evidence = verified_claim(response, state, roots) if notice or changed_paths else False
        unsupported_claim = notice and not evidence
        linked = notice_links(response, changed_paths, previous, roots)
        # A real receipt + links to ALL changed notes proves the notification, even
        # when the model uses another natural phrasing such as "設為全域偏好".
        needs_notice = bool(changed_paths) and not (evidence and linked)
        if not problems and not needs_notice and not unsupported_claim:
            return {}
        if event.get('stop_hook_active') or state.get('retried'):
            # At most one continuation: never make a broken agent loop indefinitely.
            message = 'Jason-memory：記憶保存宣告或通知未通過驗證，請勿視為已成功記住；磁碟可能已有部分變更，需檢查實際筆記。'
            return {'continue': False, 'stopReason': message, 'systemMessage': message}
        state['retried'] = True
        runtime.storelib.atomic_write(state_path, json.dumps(state))
        reason = ('Before ending: ' + ('resolve conflicting memories or explicitly report that saving failed. '
                  if problems else '') + ('End your response with a concise memory-change notice and clickable absolute links: '
                  + ', '.join(changed_paths) + '. For retired files link the actual archive path returned by apply, not the deleted original.' if needs_notice else ''))
        if unsupported_claim:
            reason += (' Unsupported memory-success claim: no matching current-turn tool receipt and unchanged on-disk note/index. '
                       'Complete runtime apply and inspect its result, or for an already-existing preference run runtime verify '
                       '--scope SCOPE --note RELATIVE_NOTE using the configured --config and --project. '
                       'Link the actual verified note. If verification fails, correct your reply to explicitly say 未保存/未能確認保存. '
                       'Do not invent a filename, receipt, successful check or rewrite an unchanged note merely to create a diff.')
        return {'decision': 'block', 'reason': reason}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--jason-hook', action='store_true')
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(runtime.MAX_BYTES + 1)
        if len(raw) > runtime.MAX_BYTES:
            raise ValueError('Hook input exceeds limit')
        event = json.loads(raw.decode('utf-8-sig'))
        result = handle(event, args.config.absolute())
        if result:
            print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Errors must be visible, not a false successful recall. SessionStart cannot block.
        print('Jason-memory hook failed: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
