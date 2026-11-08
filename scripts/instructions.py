"""CLAUDE.md instruction files, loaded the way Claude Code loads them.

    python3 instructions.py context <out> <user_file>   -> writes the session's instructions reminder to out
    python3 instructions.py nested <session> <path>      -> prints a user message line, or nothing

`context` runs once per session in the work dir. In order:
  - <user_file> (bin/harness passes /state/CLAUDE.md): the user's instructions for every project.
  - CLAUDE.md and .claude/CLAUDE.md in the work dir: the project's, checked in. AGENTS.md is read
    instead when the work dir has neither, since other agents use that name.
  - CLAUDE.local.md in the work dir: the user's own for this project, not checked in.
`history.py messages` puts the result at the start of the first user message, inside
<system-reminder> tags, before the memory index; like the index, it is fixed for the session.

`nested` runs after read_file: when the file read is in a subdirectory of the work dir, the
instruction files of each directory from the work dir down to the file's (not the work dir's own,
which `context` loaded) are added once per session, as a reminder after the tool result: Claude
Code's nested memory. The ones already sent are listed in <session>.nested, under a lock, since
read_file calls run in parallel.

Not (yet): @path imports inside the files, and the instructions of the work dir's parents (only the
work dir is mounted in the container).
"""
import fcntl
import json
import os
import sys

HEADER = ("As you answer the user's questions, you can use the following context:\n# claudeMd\n"
          "Codebase and user instructions are shown below. Be sure to adhere to these instructions. "
          "IMPORTANT: These instructions OVERRIDE any default behavior and you MUST follow them exactly as written.")
USER = "user's private global instructions for all projects"
PROJECT = "project instructions, checked into the codebase"
LOCAL = "user's private project instructions, not checked in"
MAX_CHARS = 40000


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n\n[cut at {MAX_CHARS} characters]"
    return text


def directory_files(directory):
    """[(path, kind)] of the instruction files in one directory, in load order."""
    project = [os.path.join(directory, n) for n in ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"))]
    project = [p for p in project if os.path.isfile(p)]
    if not project and os.path.isfile(os.path.join(directory, "AGENTS.md")):
        project = [os.path.join(directory, "AGENTS.md")]
    local = os.path.join(directory, "CLAUDE.local.md")
    return [(p, PROJECT) for p in project] + ([(local, LOCAL)] if os.path.isfile(local) else [])


def section(path, kind, text):
    return f"Contents of {path} ({kind}):\n\n{text}"


def context(user_file, cwd=None):
    cwd = cwd or os.getcwd()
    found = [(user_file, USER)] if user_file and os.path.isfile(user_file) else []
    found += directory_files(cwd)
    parts = [section(p, k, t) for p, k in found if (t := read(p))]
    return HEADER + "\n\n" + "\n\n".join(parts) if parts else ""


def nested(session, path, cwd=None):
    cwd = os.path.realpath(cwd or os.getcwd())
    target = os.path.realpath(os.path.join(cwd, path))
    directory = os.path.dirname(target)
    if directory == cwd or os.path.commonpath([cwd, directory]) != cwd:
        return None
    dirs, d = [], directory
    while d != cwd:
        dirs.append(d)
        d = os.path.dirname(d)
    candidates = [f for d in reversed(dirs) for f in directory_files(d)]
    if not candidates:
        return None
    with open(session + ".nested", "a+", encoding="utf-8") as seen_file:
        fcntl.flock(seen_file, fcntl.LOCK_EX)
        seen_file.seek(0)
        seen = {line.strip() for line in seen_file if line.strip()}
        fresh = [(p, k) for p, k in candidates if p not in seen]
        for p, _ in fresh:
            seen_file.write(p + "\n")
    parts = [section(p, k, t) for p, k in fresh if (t := read(p))]
    if not parts:
        return None
    text = "<system-reminder>\n" + "\n\n".join(parts) + "\n</system-reminder>"
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def main(argv):
    if len(argv) == 4 and argv[1] == "context":
        with open(argv[2], "w", encoding="utf-8") as f:
            f.write(context(argv[3]))
    elif len(argv) == 4 and argv[1] == "nested":
        message = nested(argv[2], argv[3]) if argv[3] else None
        if message:
            sys.stdout.write(json.dumps(message) + "\n")
    else:
        sys.exit("usage: instructions.py context <out> <user_file> | nested <session> <path>")


if __name__ == "__main__":
    main(sys.argv)
