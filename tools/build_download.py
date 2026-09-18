#!/usr/bin/env python3
"""Build a clean ready-to-use ZIP without Git, personal notes or local artifacts."""
import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent.parent
# Explicit files only: never scan the developer's working tree for release data.
PACKAGE_FILES = (
    '.jason-memory.json', '.claude/settings.json', 'INSTALL.cmd',
    'tools/memory_facts.py', 'tools/memory_runtime.py', 'tools/claude_memory_hook.py',
    'tools/hook_settings.py', 'tools/install_hooks.py', 'global/global_store.py',
    'docs/memory-runtime.md', 'tests/test_memory_runtime.py', 'tests/hooks-validation.md',
    'START_HERE.md', 'README.md', 'AGENTS.md', 'CLAUDE.md', 'SKILL.md',
    'LICENSE', '.gitignore', 'templates/MEMORY.md',
    'templates/standing-rules.md', 'templates/example-feedback.md',
    'templates/example-project.md', 'tools/jason_check.py',
    'tools/jason_doctor.py', 'tools/build_download.py',
    'tests/test_memory_tools.py', 'tests/test_download.py',
    'tests/behavioral-memory.md', 'tests/recall-validation.md', 'evals/README.md', 'evals/evals.json',
    'evals/fixtures/header-preview.html', 'evals/results/2026-09-16.md',
)


def build(output, source=ROOT):
    source = Path(source).resolve()
    output = Path(output).resolve()
    payload = []
    for name in PACKAGE_FILES:
        path = source / name
        # Do not silently package a symlink aimed at an unrelated local file.
        if path.is_symlink() or path.resolve() != path or not path.is_file():
            raise ValueError('Missing or redirected package source: ' + name)
        if output == path:
            raise ValueError('Output must not replace a package source')
        payload.append((name, path.read_bytes()))
    # Always use the pristine template, never the maintainer's real memory store.
    payload.append(('.jason-memory/MEMORY.md', dict(payload)['templates/MEMORY.md']))
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids overwriting an unrelated file or existing release.
    with ZipFile(output, 'x', compression=ZIP_DEFLATED) as archive:
        for name, content in payload:
            archive.writestr('jason-memory/' + name, content)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/jason-memory.zip')
    args = parser.parse_args()
    try:
        result = build(args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, 'Download build failed: ' + str(exc) + '\n')
    print(result)


if __name__ == '__main__':
    main()
