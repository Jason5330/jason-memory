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


# HTTP credentials in plain text (#50): `Authorization: <scheme> VALUE` and bare `Bearer VALUE`.
# With a scheme any token counts; without one, only token-like values (digits+letters or long).
_AUTH_HEADER = re.compile(
    r'(?i)\b(?:proxy-)?authorization\b["\']?\s*[:=]\s*["\']?'
    r'(?P<scheme>(?:basic|bearer|token|negotiate|apikey|api-key|key)\s+)?(?P<value>[A-Za-z0-9._~+/=:-]{6,})')
_BEARER = re.compile(r'(?i)\bbearer\s+(?P<value>[A-Za-z0-9._~+/=-]{12,})')


def _auth_values(text):
    for match in _AUTH_HEADER.finditer(text):
        value = match.group('value')
        if match.group('scheme') or len(value) >= 16 or (re.search(r'\d', value) and re.search(r'[A-Za-z]', value)):
            yield value
    for match in _BEARER.finditer(text):
        yield match.group('value')


def secret_values(text):
    """Return credential-looking values in text. Conservative: env var NAMES are allowed."""
    found = [m.group(0) for m in _SECRET_PREFIX.finditer(text)]
    found += list(_auth_values(text))
    for keyword in re.finditer(_SECRET_WORD, text, re.I):
        if re.match(r'[A-Za-z0-9_]', text[keyword.end():keyword.end() + 1]):
            continue  # `password_expires_days`: the keyword starts a longer name, not a credential label.
        window = text[keyword.end():keyword.end() + 60]
        for match in _CANDIDATE.finditer(window):
            value = match.group(0)
            before = window[:match.start()]
            if len(before) > 32 or re.search(r'[,，。;；&|\n<>\\]|\.\s', before):
                break  # A value belongs to its keyword only within the same short clause (no `&& echo X`).
            if not (re.search(r'\d', value) and re.search(r'[A-Za-z]', value)):
                continue  # Plain words or identifiers without digits are not treated as values.
            if _ENV_HINT.search(before) or re.fullmatch(r'\$\{?[A-Z_][A-Z0-9_]*\}?', value):
                continue
            if re.fullmatch(r'[A-Z][A-Z0-9_]*', value) and re.search(r'\b(?:env|環境|环境)', text, re.I):
                continue
            found.append(value)
            break
    return sorted(set(found))


# Raw-ledger redaction is broader than the note guard above: a credential NAME followed by an
# assignment or header separator marks its value as secret, whatever the value looks like
# (letters only included). False positives only hide text in the raw layer; notes keep the
# conservative secret_values() check.
_NAME = (r'(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|'
         r'private[_-]?key|credentials?)')
# A secret value is either quoted - then the WHOLE quoted text, spaces included (#54) - or one bare token.
# A bare value ends at whitespace, quotes, separators or a backslash: inside one shell string,
# `a=1\npassword=2` holds two assignments separated by a literal `\n`.
_VALUE = r'(?:"(?P<dq>(?:[^"\\\n]|\\.)*)"|\'(?P<sq>(?:[^\'\\\n]|\\.)*)\'|(?P<value>(?:[^\s"\'&;,<>\\]|\\(?![nrt]))+))'
# A name starts after a non-alphanumeric character or after a literal `\n`, `\r`, `\t` escape.
_START = r'(?:(?<![A-Za-z0-9\\])|(?<=\\[nrt]))'
_ASSIGNED = re.compile(
    r'(?i)' + _START + r'(?P<name>(?:[A-Za-z0-9]+[_.-])*' + _NAME + r')(?![A-Za-z0-9])["\']?\s*(?:=|:)\s*' + _VALUE)
_FLAG = re.compile(r'(?i)(?:(?<![A-Za-z0-9-])|(?<=\\[nrt]))--?(?:password|passwd|pass|token|api-?key|secret|client-secret)'
                   r'(?:=|\s+)' + _VALUE)
_QUOTED_AUTH = re.compile(r'(?i)\b(?:proxy-)?authorization\b["\']?\s*[:=]\s*(?:"(?P<dq>[^"\n]*)"|\'(?P<sq>[^\'\n]*)\')')
_SCHEME = re.compile(r'(?i)^(\s*(?:basic|bearer|token|negotiate|digest|apikey|api-key|key)\s+)?.*$', re.S)
_COOKIE_LINE = re.compile(r'(?im)^(?P<head>[^\S\n]*(?:set-)?cookie\s*:)(?P<body>[^\n]*)$')
_COOKIE_VALUE = re.compile(r'=(?P<value>[^;\s]+)')
_USERINFO = re.compile(r'(?i)\b(?P<head>[a-z][a-z0-9+.-]*://[^/\s:@]+):(?P<value>[^@\s/]+)@')
_PRIVATE_BLOCK = re.compile(r'(-----BEGIN (?P<kind>[A-Z ]*)PRIVATE KEY-----).*?(-----END (?P=kind)PRIVATE KEY-----|\Z)', re.S)


def _mask_value(match, keep=None):
    """Replace only the value group of a match; quotes and surrounding text stay."""
    group = next(g for g in ('dq', 'sq', 'value') if match.group(g) is not None)
    value = match.group(group)
    if keep is not None and keep(value):
        return match.group(0)
    start, end = match.start(group) - match.start(), match.end(group) - match.start()
    return match.group(0)[:start] + '[REDACTED]' + match.group(0)[end:]


_ENV_REF = re.compile(r'\$[A-Za-z_]\w*|\$\{[A-Za-z_]\w*\}|\$env:[A-Za-z_]\w*|%[A-Za-z_]\w*%', re.I)
_PLACEHOLDER = {'null', 'none', 'nil', 'true', 'false', 'undefined', 'required', 'optional', 'yes', 'no',
                'n/a', 'na', 'empty', 'unset', 'changeme', 'redacted', '<redacted>', '[redacted]'}
# `max_token: 4096`, `password_min_length: 8`, `password_expires_days=90`: a name that describes a
# limit or count of the credential, not the credential itself.
_MEASURE = re.compile(r'(?i)(?:^|[_.-])(?:max|min|num|limit|count|len|length|size|ttl|timeout|expires?|days|'
                      r'hours|minutes|seconds|age|retries|attempts)(?:[_.-]|$)')


def credential_value(name, value):
    """One judgement for the raw-ledger masker and the note guard: is `value`, assigned to the
    credential-looking `name`, an actual secret? Ordinary values stay readable (env refs, empty
    placeholders, paths for `pwd`, numbers for limit/count names, prose that is not ASCII)."""
    stripped = (value or '').strip().strip('.')
    name = (name or '').lower()
    if not stripped or stripped.lower() in _PLACEHOLDER or _ENV_REF.fullmatch(stripped):
        return False
    if re.fullmatch(r'[*xX.\-_]+', stripped):
        return False  # Masks such as ****, xxx.
    if re.search(r'[^\x00-\x7f]', stripped):
        return False  # `token: 放在環境變數 ...` is prose about where a secret lives, not the secret.
    if re.search(r'(?:^|[_.-])pwd$', name) and re.match(r'(?:[/~.]|[A-Za-z]:[\\/])', stripped):
        return False  # PWD / pwd is the working directory.
    if _MEASURE.search(name) and re.fullmatch(r'\d+(?:\.\d+)?[a-z]{0,2}', stripped, re.I):
        return False
    return True


def _keep(match):
    try:
        name = match.group('name')
    except IndexError:
        name = 'password'
    return lambda value: not credential_value(name, value)


def note_has_credential(text):
    """Notes never publish a credential: prefixed tokens (sk-, ghp_ ...) or any value explicitly
    assigned to a credential name (password=0427, token: lettersonly), judged like the ledger masker."""
    if secret_values(text):
        return True
    for pattern in (_ASSIGNED, _FLAG):
        for match in pattern.finditer(text):
            value = next(v for v in (match.group('dq'), match.group('sq'), match.group('value')) if v is not None)
            if not _keep(match)(value):
                return True
    return False


def redact(text):
    """Mask credential values before anything reaches the raw ledger (#38, #43, #50, #54)."""
    text = _PRIVATE_BLOCK.sub('[REDACTED PRIVATE KEY]', text)
    for value in secret_values(text):
        text = text.replace(value, '[REDACTED]')
    text = _QUOTED_AUTH.sub(lambda m: m.group(0)[:m.start('dq' if m.group('dq') is not None else 'sq') - m.start()]
                            + _SCHEME.sub(lambda s: (s.group(1) or '') + '[REDACTED]',
                                          m.group('dq') if m.group('dq') is not None else m.group('sq'))
                            + m.group(0)[-1], text)
    text = _COOKIE_LINE.sub(lambda m: m.group('head') + _COOKIE_VALUE.sub('=[REDACTED]', m.group('body')), text)
    text = _USERINFO.sub(lambda m: m.group('head') + ':[REDACTED]@', text)
    text = _FLAG.sub(lambda m: _mask_value(m, _keep(m)), text)
    return _ASSIGNED.sub(lambda m: _mask_value(m, _keep(m)), text)
