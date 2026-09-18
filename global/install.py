#!/usr/bin/env python3
"""Install Jason-memory's shared local protocol without Git or network access."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid
import shlex

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import hook_settings


START = b"<!-- jason-memory-global:start -->"
END = b"<!-- jason-memory-global:end -->"
RECALL_V2 = b"<!-- jason-memory-global:recall-v2 -->"
FILES = {
    "docs/memory-runtime.md": "docs/memory-runtime.md",
    "tools/memory_facts.py": "tools/memory_facts.py",
    "tools/memory_runtime.py": "tools/memory_runtime.py",
    "tools/claude_memory_hook.py": "tools/claude_memory_hook.py",
    "tools/hook_settings.py": "tools/hook_settings.py",
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
    start, end = data.index(START), data.index(END) + len(END)
    if data[start:end].startswith(START + b"\n" + RECALL_V2) and data[end:end+1] == b"\n":
        end += 1  # Separator owned by v2, not part of the original user's bytes.
    return start, end


def claude_import(index, config_dir):
    """Resolve from CLAUDE.md, with Claude's backslash-space import escaping."""
    try:
        path = Path(os.path.relpath(index, config_dir)).as_posix()
        if not path.startswith(("./", "../")):
            # Claude's import recognizer can skip a bare Unicode-leading path.
            path = "./" + path
    except ValueError:  # Different Windows drives.
        path = index.as_posix()
    if any(c in path for c in '\r\n\t`<>"'):
        raise ValueError("Memory path cannot be represented as a Claude import: " + path)
    return "@" + path.replace(" ", "\\ ") + "\n\n"


def instructions(home, claude_dir=None):
    # These are JSON strings, not shell commands: agents must quote for their shell.
    paths = json.dumps({"memory_home": str(home),
                        "protocol": str(home / "framework/SKILL.md"),
                        "runtime": str(home / "framework/tools/memory_runtime.py"),
                        "config": str(home / "framework/memory-config.json")},
                       ensure_ascii=False, indent=2)
    return (
        "\n\n## Jason-memory: recall before any visible reply\n\n"
        + (claude_import(home / "memory/shared/MEMORY.md", claude_dir)
           if claude_dir is not None else "") +
        "Before the FIRST user-visible text (including progress or greetings), run\n"
        "context and read applicable response, opening, language, formatting and task\n"
        "notes. This also applies to trivial questions and every follow-up message.\n"
        "Recall silently: do not announce reading, successful recall, or no-memory changes.\n"
        "Notify only actual saves/updates/retirements, or a material read/write failure.\n"
        "An imported index is only a startup snapshot: refresh with context and read\n"
        "relevant note bodies before replying. Never load other projects' indexes.\n"
        "Apply scope and current user instructions; higher-priority rules still apply.\n"
        "Read the installed protocol at the JSON `protocol` path at task start.\n"
        "Paths below are absolute JSON strings, not executable shell text:\n"
        "```json\n" + paths + "\n```\n"
        "Use the runtime with arguments `--config CONFIG --project ACTIVE_PROJECT_ROOT context`\n"
        "and read BOTH the global index and this project's index plus relevant notes.\n"
        "Resolve ACTIVE_PROJECT_ROOT from the active workspace explicitly; never use the\n"
        "framework directory as the user's project. If unknown, ask before project access.\n"
        "Refresh context for every user message to see other AI writers' latest changes.\n"
        "Use the protocol's versioned batch apply command for memory changes. Every user message,\n"
        "including follow-ups, requires fresh judgment of reusable requirements; a one-off\n"
        "task does not make later requirements one-off. Save clear reusable preferences\n"
        "without asking again, choose global/project scope carefully, and notify the user\n"
        "after a successful save with what, why, scope and note link. No change: stay quiet.\n"
        "The final save notice must include a clickable Markdown link to the returned\n"
        "absolute note path and the validation result; a bare filename is not a link.\n"
        "A memory-success claim requires a current-turn apply/verify tool receipt and\n"
        "matching on-disk content. Preserve runtime JSON output. For an unchanged\n"
        "preference run verify --scope SCOPE --note RELATIVE_NOTE instead of rewriting.\n"
        "Corrections search ALL related active notes, update every occurrence in one batch,\n"
        "preserve unrelated facts, and apply with the current store expected_revision.\n"
        "If the active project has .jason-memory.json, that explicit project configuration\n"
        "takes precedence: use its runtime/context instead of a parallel global store.\n"
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
            unmanaged = original[:span[0]] + original[span[1]:] if span else original
            if args.uninstall:
                updated = unmanaged
            else:
                claude_dir = target.parent if target == claude / "CLAUDE.md" else None
                target_block = (START + b"\n" + RECALL_V2 + instructions(home, claude_dir)
                                + END + b"\n")
                # Keep the managed entrance first without changing any user-owned bytes.
                bom = b"\xef\xbb\xbf" if unmanaged.startswith(b"\xef\xbb\xbf") else b""
                updated = bom + target_block + unmanaged[len(bom):]
            if updated != original:
                edits.append((target, original, updated, target.exists()))

        copies = []
        if args.agent in ('claude', 'both'):
            settings = claude / 'settings.json'
            if settings.is_symlink() or settings.resolve() != settings:
                raise ValueError('Redirected Claude settings are not supported')
            original = settings.read_bytes() if settings.exists() else b''
            command = ' '.join(shlex.quote(p) for p in (Path(sys.executable).as_posix(),
                              (home / 'framework/tools/claude_memory_hook.py').as_posix(),
                              '--config', (home / 'framework/memory-config.json').as_posix(), '--jason-hook'))
            updated = hook_settings.merged(original, None if args.uninstall else command)
            if updated != original and (not args.uninstall or original):
                edits.append((settings, original, updated, settings.exists()))
        index = home / "memory/shared/MEMORY.md"
        if not args.uninstall:
            source_root = Path(__file__).resolve().parents[1]
            # Read every source before changing any destination.
            for source, dest in FILES.items():
                origin = source_root / source
                if origin.is_symlink() or origin.resolve() != origin:
                    raise ValueError('Redirected framework source: ' + str(origin))
                copies.append((home / "framework" / dest, origin.read_bytes()))
            copies.append((home / "GENERIC_INSTRUCTIONS.md", block))
            copies.append((home / 'framework/memory-config.json',
                           b'{"version":1,"mode":"global","home":".."}\n'))
            if index.is_symlink() or index.resolve() != index:
                raise ValueError("Redirected shared index is not supported: " + str(index))
            if index.exists() and not index.is_file():
                raise ValueError("Shared index is not a file: " + str(index))
        plan = {"dry_run": args.dry_run, "uninstall": args.uninstall,
                "home": str(home), "edit": [str(item[0]) for item in edits],
                "copy": [str(item[0]) for item in copies],
                "initialize_if_missing": [] if args.uninstall or index.exists() else [str(index)],
                "memories_preserved": True}
        for destination in [entry[0] for entry in edits] + [entry[0] for entry in copies]:
            if destination.is_symlink() or destination.resolve() != destination:
                raise ValueError('Redirected installation target: ' + str(destination))
        if not args.dry_run:
            for dest, data in copies:
                if not dest.exists() or dest.read_bytes() != data:
                    atomic_write(dest, data)
            if not args.uninstall:
                index.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive create cannot overwrite memory saved during installation.
                try:
                    stream = index.open("xb")
                except FileExistsError:
                    pass
                else:
                    with stream:
                        stream.write(dict(copies)[home / "framework/templates/MEMORY.md"])
                        stream.flush()
                        os.fsync(stream.fileno())
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
