#!/usr/bin/env python3
"""Build the separate global-memory ZIP from an explicit public-file allowlist."""
import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_FILES = {
    'tools/memory_facts.py': 'tools/memory_facts.py',
    'tools/memory_runtime.py': 'tools/memory_runtime.py',
    'tools/claude_memory_hook.py': 'tools/claude_memory_hook.py',
    'tools/hook_settings.py': 'tools/hook_settings.py',
    'docs/memory-runtime.md': 'docs/memory-runtime.md',
    'tests/hooks-validation.md': 'tests/hooks-validation.md',
    "global/README.md": "README.md",
    "global/INSTALL.cmd": "INSTALL.cmd",
    "LICENSE": "LICENSE",
    "global/install.py": "global/install.py",
    "global/global_store.py": "global/global_store.py",
    "global/SKILL.md": "global/SKILL.md",
    "templates/MEMORY.md": "templates/MEMORY.md",
    "tools/jason_check.py": "tools/jason_check.py",
    "tools/jason_doctor.py": "tools/jason_doctor.py",
}


def build(output, source=ROOT):
    source = Path(source).resolve()
    output = Path(output).absolute()
    payload = []
    for name, archive_name in PACKAGE_FILES.items():
        path = source / name
        if path.is_symlink() or path.resolve() != path or not path.is_file():
            raise ValueError("Missing or redirected package source: " + name)
        if output.resolve() == path:
            raise ValueError("Output must not replace a package source")
        payload.append((archive_name, path.read_bytes()))
    # Read and validate all source files before creating the output directory.
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name, content in payload:
            archive.writestr("jason-memory-global/" + name, content)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/jason-memory-global.zip")
    args = parser.parse_args()
    try:
        output = build(args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, "Global download build failed: " + str(exc) + "\n")
    print(output)


if __name__ == "__main__":
    main()
