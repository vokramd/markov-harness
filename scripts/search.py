"""The read-only tools: read_file, and glob and grep searches of the work directory.

    python3 search.py read < {path}
    python3 search.py glob < {pattern, path?}
    python3 search.py grep < {pattern, path?, glob?, ignore_case?}

Prints plain text for the tool result; a result starting with "error:" exits 1. Skips .git, node_modules and other bulky directories, and
stops after MAX_RESULTS matches.
"""
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

MAX_RESULTS = 200
MAX_LINE = 300
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox"}


def walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath, name)


def rel(path):
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def read(args):
    # A script rather than a load_artifact step: the model often reads a file that isn't there (an
    # empty MEMORY.md, say), and this returns that as the result text. (load_artifact used to fail the
    # run on a missing file even with ignore_errors; markov has fixed that since.)
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return "error: path is required"
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        return f"error: cannot read {path}: {e}"


def glob(args):
    pattern, root = args.get("pattern"), Path(args.get("path") or ".")
    if not pattern:
        return "error: pattern is required"
    if not root.is_dir():
        return f"error: {root} is not a directory"
    found = []
    for path in root.glob(pattern):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        found.append(path)
        if len(found) > MAX_RESULTS:
            break
    found.sort()
    lines = [rel(p) + ("/" if p.is_dir() else "") for p in found[:MAX_RESULTS]]
    if not lines:
        return f"no paths match {pattern}"
    more = f"\n[stopped after {MAX_RESULTS} results]" if len(found) > MAX_RESULTS else ""
    return "\n".join(lines) + more


def grep(args):
    pattern, root = args.get("pattern"), Path(args.get("path") or ".")
    if not pattern:
        return "error: pattern is required"
    try:
        regex = re.compile(pattern, re.IGNORECASE if args.get("ignore_case") else 0)
    except re.error as e:
        return f"error: bad regular expression: {e}"
    files = [root] if root.is_file() else walk(root) if root.is_dir() else None
    if files is None:
        return f"error: {root} does not exist"
    name_glob = args.get("glob")
    lines = []
    for path in files:
        if name_glob and not fnmatch.fnmatch(path.name, name_glob):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for number, line in enumerate(f, 1):
                    if regex.search(line):
                        text = line.rstrip("\n")
                        if len(text) > MAX_LINE:
                            text = text[:MAX_LINE] + "…"
                        lines.append(f"{rel(path)}:{number}: {text}")
                        if len(lines) >= MAX_RESULTS:
                            return "\n".join(lines) + f"\n[stopped after {MAX_RESULTS} matches]"
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable
    return "\n".join(lines) if lines else f"no matches for {pattern}"


def main(argv):
    tools = {"read": read, "glob": glob, "grep": grep}
    if len(argv) != 2 or argv[1] not in tools:
        sys.exit("usage: search.py read|glob|grep < tool input JSON")
    out = tools[argv[1]](json.load(sys.stdin))
    sys.stdout.write(out)
    if out.startswith("error:"):
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
