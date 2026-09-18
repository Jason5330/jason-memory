"""Conservative fact checks, not a general natural-language truth detector."""
import json
import re

ALIASES = {'醫生': 'doctor', '医生': 'doctor', '醫師': 'doctor', '医师': 'doctor',
           '可靠性工程師': 'reliability engineer', '可靠性工程师': 'reliability engineer',
           '老師': 'teacher', '老师': 'teacher'}


def canonical(value):
    value = ' '.join(str(value).casefold().split())
    return ALIASES.get(value, value)


def extract(content):
    facts = []
    blocks = re.findall(r'```jason-facts\s*\n(.*?)\n```', content, re.S)
    for block in blocks:
        rows = json.loads(block)
        if not isinstance(rows, list) or not rows:
            raise ValueError('jason-facts must be a nonempty JSON array')
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'subject', 'predicate', 'value'}
                    or not all(isinstance(v, str) and v.strip() for v in row.values())):
                raise ValueError('Each fact needs nonempty subject, predicate and value strings')
            predicate = canonical(row['predicate'])
            # Formatting preferences can depend on case and exact whitespace.
            # Only the narrow occupation vocabulary has semantic aliases.
            value = canonical(row['value']) if predicate == 'occupation' else row['value']
            facts.append((canonical(row['subject']), predicate, value))
    # Legacy recognition deliberately limited to standalone affirmative occupation clauses.
    # Strip metadata, fenced examples and quoted lines; never extract "不是醫生" as affirmative.
    body = re.sub(r'\A---\s*\n.*?\n---\s*\n', '', content, flags=re.S)
    body = re.sub(r'```.*?```', '', body, flags=re.S)
    occupations = '|'.join(map(re.escape, sorted(ALIASES, key=len, reverse=True)))
    pattern = r'^\s*(?:[-*]\s+)?([A-Za-z][\w-]*)\s*(?:的職業|的职业)?\s*是\s*(' + occupations + r')(?=[\s，。,！!；;、]|$)'
    for line in body.splitlines():
        match = re.search(pattern, line)
        if match:
            facts.append(('user:' + canonical(match[1]), 'occupation', canonical(match[2])))
    return facts, bool(blocks)


def audit(notes):
    groups, review, errors = {}, [], []
    for path, content in sorted(notes.items()):
        try:
            facts, structured = extract(content)
        except (ValueError, TypeError) as exc:
            errors.append({'path': path, 'error': str(exc)})
            continue
        if not structured:
            review.append(path)
        for subject, predicate, value in facts:
            groups.setdefault((subject, predicate), {}).setdefault(value, []).append(path)
    conflicts = [{'subject': key[0], 'predicate': key[1], 'values': values}
                 for key, values in sorted(groups.items()) if len(values) > 1]
    return {'conflicts': conflicts, 'invalid_facts': errors, 'semantic_review_needed': review,
            'facts': [{'subject': k[0], 'predicate': k[1], 'values': v} for k, v in sorted(groups.items())]}
