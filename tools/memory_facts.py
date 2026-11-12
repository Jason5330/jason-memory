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


# --- Credential guard -------------------------------------------------------
# Memory may record WHERE a secret lives (an environment variable name or vault
# path) but never the secret value itself.
_SECRET_PREFIX = re.compile(
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|'
    r'\bgithub_pat_[A-Za-z0-9_]{20,}|\bAKIA[0-9A-Z]{16}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}|'
    r'\bAIza[0-9A-Za-z_-]{30,}|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}')
_SECRET_WORD = (r'(?:api[\s_-]*key|access[\s_-]*key|secret|token|password|passwd|pwd|credential|bearer|'
                r'private[\s_-]*key|密碼|密码|口令|金鑰|金钥|密鑰|密钥|憑證|凭证|權杖|令牌)')
_ENV_HINT = re.compile(r'(?:環境變數|环境变量|變數名|变量名|env(?:ironment)?(?:\s+var(?:iable)?)?|\$|%|vault|保管|存放於|存放在)\s*[:：=]?\s*$', re.I)
_CANDIDATE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_\-./+=]{7,}')


def secret_values(text):
    """Return credential-looking values in text. Conservative: env var NAMES are allowed."""
    found = [m.group(0) for m in _SECRET_PREFIX.finditer(text)]
    for keyword in re.finditer(_SECRET_WORD, text, re.I):
        window = text[keyword.end():keyword.end() + 60]
        for match in _CANDIDATE.finditer(window):
            value = match.group(0)
            before = window[:match.start()]
            if not (re.search(r'\d', value) and re.search(r'[A-Za-z]', value)):
                continue  # Plain words or identifiers without digits are not treated as values.
            if _ENV_HINT.search(before) or re.fullmatch(r'\$\{?[A-Z_][A-Z0-9_]*\}?', value):
                continue
            if re.fullmatch(r'[A-Z][A-Z0-9_]*', value) and re.search(r'\b(?:env|環境|环境)', text, re.I):
                continue
            found.append(value)
            break
    return sorted(set(found))


def redact(text):
    for value in secret_values(text):
        text = text.replace(value, '[REDACTED]')
    return text
