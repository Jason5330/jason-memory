#!/usr/bin/env python3
"""Report index size after a write; no default line or byte budget.

    python tools/jason_check.py <path-to-MEMORY.md>

Optional positive environment values: JASON_WARN / JASON_WARN_BYTES for warnings,
JASON_HARD / JASON_HARD_BYTES for an over-budget verdict. Unset, invalid or
non-positive values disable that dimension. No write is intercepted or blocked.

Exit codes: 0 = measured, within any configured budgets; 1 = WARN; 2 = OVER;
64 = usage error; 66 = unreadable input. Size is counted in bounded chunks.
"""
import os
import sys


def _envint(name):
    try:
        return max(0, int(os.environ.get(name, '0')))
    except (TypeError, ValueError):
        return 0


def _kb(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if n < 1024 or unit == 'GiB':
            return f'{n} B' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def _over(lines, nbytes, line_cap, byte_cap):
    parts = []
    if line_cap and lines > line_cap:
        parts.append(f'{lines} lines > {line_cap}')
    if byte_cap and nbytes > byte_cap:
        parts.append(f'{nbytes} bytes > {byte_cap}')
    return ' and '.join(parts)


def main(argv):
    try:
        sys.stdout.reconfigure(errors='backslashreplace')
    except (AttributeError, ValueError, OSError):
        pass
    if any(a in ('-h', '--help') for a in argv[1:]):
        print(__doc__.strip())
        return 0
    if len(argv) != 2:
        print('usage: jason_check.py <path-to-MEMORY.md>')
        return 64
    lines = nbytes = 0
    last = b''
    try:
        with open(argv[1], 'rb') as fh:
            for chunk in iter(lambda: fh.read(65536), b''):
                nbytes += len(chunk)
                lines += chunk.count(b'\n')
                last = chunk[-1:]
    except OSError as exc:
        print(f'jason: cannot read {argv[1]}: {exc}')
        return 66
    if last and last != b'\n':
        lines += 1
    size = f'{lines} lines / {nbytes} bytes ({_kb(nbytes)})'
    hard, hard_b = _envint('JASON_HARD'), _envint('JASON_HARD_BYTES')
    warn, warn_b = _envint('JASON_WARN'), _envint('JASON_WARN_BYTES')
    over = _over(lines, nbytes, hard, hard_b)
    if over:
        print(f'OVER: index is {size}; exceeds your configured budget: {over}. Review and compact the index.')
        return 2
    over = _over(lines, nbytes, warn, warn_b)
    if over:
        print(f'WARN: index is {size}; exceeds your configured warning: {over}. Consider a cleanup.')
        return 1
    budget = 'within configured budgets' if any((hard, hard_b, warn, warn_b)) else 'no size budget configured'
    print(f'OK: index is {size}; {budget}.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
