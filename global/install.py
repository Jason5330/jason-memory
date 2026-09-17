#!/usr/bin/env python3
"""Install Jason-memory's shared local protocol without Git or network access."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid


START = b"<!-- jason-memory-global:start -->"
END = b"<!-- jason-memory-global:end -->"
FILES = {
    "global/SKILL.md": "SKILL.md",
    "global/global_store.py": "global_store.py",
    "templates/MEMORY.md": "templates/MEMORY.md",
    "tools/jason_check.py": "tools/jason_check.py",
    "tools/jason_doctor.py": "tools/jason_doctor.py",
}


def absolute(value):
    return Path(value).expanduser().resolve()


def managed_span(data, path):
    """Reject ambiguous or damaged blocks before any install writes happen."""
    starts, ends = data.count(START), data.count(END)
    if starts == ends == 0:
        return None
    if starts != 1 or ends != 1 or data.index(START) >= data.index(END):
        raise ValueError("Malformed or duplicate Jason-memory managed block: " + str(path))
    return data.index(START), data.index(END) + len(END)


def instructions(home):
    # These are JSON strings, not shell commands: agents must quote for their shell.
    paths = json.dumps({"memory_home": str(home),
                        "protocol": str(home / "framework/SKILL.md"),
                        "runtime": str(home / "framework/global_store.py")},
                       ensure_ascii=False, indent=2)
    return (
        "\n\n## Jason-memory: shared local memory\n\n"
        "Read the installed protocol at the JSON `protocol` path at task start.\n"
        "Paths below are absolute JSON strings, not executable shell text:\n"
        "```json\n" + paths + "\n```\n"
        "Use the runtime with arguments `--home MEMORY_HOME context --project ACTIVE_PROJECT_ROOT`\n"
        "and read BOTH the global index and this project's index plus relevant notes.\n"
        "Resolve ACTIVE_PROJECT_ROOT from the active workspace explicitly; never use the\n"
        "framework directory as the user's project. If unknown, ask before project access.\n"
        "Refresh context for every user message to see other AI writers' latest changes.\n"
        "Use the protocol's read/save commands for memory changes. Every user message,\n"
        "including follow-ups, requires fresh judgment of reusable requirements; a one-off\n"
        "task does not make later requirements one-off. Save clear reusable preferences\n"
        "without asking again, choose global/project scope carefully, and notify the user\n"
        "after a successful save with what, why, scope and note link. No change: stay quiet.\n"
        "The final save notice must include a clickable Markdown link to the returned\n"
        "absolute note path and the validation result; a bare filename is not a link.\n"
        "Corrections reuse the existing note slug: read, then save with --expected-sha.\n"
        "Do not add a replacement and retire the original for a same-fact correction.\n"
        "Stage input files in the active project, never inside the memory stores.\n"
        "Existing higher-priority or project-specific instructions still apply; report\n"
        "conflicts honestly, and do not rewrite those rules as part of installation.\n"
        "No Git commands, initialization, monitoring, commit reminders or network are\n"
        "needed for memory. Do not claim a memory was saved without successful verification.\n\n"
    ).encode("utf-8")


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".jason-install-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("codex", "claude", "both", "generic"), default="both")
    parser.add_argument("--home", help="Shared memory home (default: ~/.jason-memory-global)")
    parser.add_argument("--user-home", help="Explicit isolated profile; ignores host config environment variables")
    parser.add_argument("--codex-home", help="Explicit Codex configuration directory")
    parser.add_argument("--claude-home", help="Explicit Claude configuration directory")
    parser.add_argument("--dry-run", action="store_true", help="Print plans without changing any files")
    parser.add_argument("--uninstall", action="store_true", help="Remove managed host blocks only; retain all memories and framework")
    args = parser.parse_args(argv)
    try:
        profile = absolute(args.user_home) if args.user_home else Path.home().resolve()
        home = absolute(args.home) if args.home else profile / ".jason-memory-global"
        codex_env = None if args.user_home else os.environ.get("CODEX_HOME")
        claude_env = None if args.user_home else os.environ.get("CLAUDE_CONFIG_DIR")
        codex = absolute(args.codex_home or codex_env or profile / ".codex")
        claude = absolute(args.claude_home or claude_env or profile / ".claude")
        targets = []
        if args.agent in ("codex", "both"):
            override = codex / "AGENTS.override.md"
            if args.uninstall:
                # An override may have appeared after an earlier normal-file install.
                targets.extend((codex / "AGENTS.md", override))
            else:
                targets.append(override if override.is_file() and override.stat().st_size else codex / "AGENTS.md")
        if args.agent in ("claude", "both"):
            targets.append(claude / "CLAUDE.md")
        block = START + instructions(home) + END
        edits = []
        for target in targets:
            original = target.read_bytes() if target.exists() else b""
            span = managed_span(original, target)
            if span:
                updated = original[:span[0]] + (b"" if args.uninstall else block) + original[span[1]:]
            else:
                updated = original if args.uninstall else original + block
            if updated != original:
                edits.append((target, original, updated, target.exists()))

        copies = []
        if not args.uninstall:
            source_root = Path(__file__).resolve().parents[1]
            # Read every source before changing any destination.
            for source, dest in FILES.items():
                copies.append((home / "framework" / dest, (source_root / source).read_bytes()))
            copies.append((home / "GENERIC_INSTRUCTIONS.md", block))
        plan = {"dry_run": args.dry_run, "uninstall": args.uninstall,
                "home": str(home), "edit": [str(item[0]) for item in edits],
                "copy": [str(item[0]) for item in copies],
                "memories_preserved": True}
        if not args.dry_run:
            for dest, data in copies:
                if not dest.exists() or dest.read_bytes() != data:
                    atomic_write(dest, data)
            for target, original, updated, existed in edits:
                # Refuse to overwrite an instruction file changed since validation.
                current = target.read_bytes() if target.exists() else b""
                if current != original or target.exists() != existed:
                    raise ValueError("Configuration changed during install; retry: " + str(target))
                if existed:
                    backup = target.with_name(target.name + ".jason-memory-" + uuid.uuid4().hex + ".bak")
                    atomic_write(backup, original)
                atomic_write(target, updated)
        # Console encodings vary on Windows; JSON escapes preserve arbitrary paths.
        print(json.dumps(plan, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
