"""Settings files and permission rules, as in Claude Code.

    python3 settings.py add <rule>   -> saves an allow rule to .claude/settings.local.json

Settings come from three JSON files, read in this order and merged: the user's (bin/harness passes
/state/settings.json), then the work dir's .claude/settings.json (checked in) and
.claude/settings.local.json (not checked in; answering `a` at an approval prompt saves an allow rule
there). Each may hold:

    {"permissions": {"allow": [...], "ask": [...], "deny": [...]},
     "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "..."}]}], ...}}

A rule is a tool name, optionally with a specifier: `Bash` (every command), `Bash(npm test)` (exactly
that command), `Bash(git diff *)` or the older `Bash(git diff:*)` (that command with or without more
words), `Edit(src/**)` (a path glob, relative to the work dir; `//` starts an absolute one; `*` stays in a
directory, `**` crosses them). Edit rules cover both write_file and edit_file, Write rules write_file
only, Read rules read_file. The harness's tool names work too (`bash(...)`, `edit_file(...)`).

A command with shell control characters (; & | < > ` $( or a newline) only matches an allow rule written
for exactly that command, so `Bash(git diff *)` can't let `git diff; rm -rf x` through. Ask and deny
rules also match any part of a compound command; they are a best effort, not a sandbox: the container
is the sandbox.
"""
import json
import os
import re
import sys

PROJECT_FILES = [".claude/settings.json", ".claude/settings.local.json"]
LOCAL_FILE = ".claude/settings.local.json"
# Claude Code's tool names, as hooks and rules use them.
CC_NAMES = {"bash": "Bash", "read_file": "Read", "write_file": "Write", "edit_file": "Edit", "glob": "Glob",
            "grep": "Grep", "skill": "Skill", "agent": "Agent", "todo_write": "TodoWrite",
            "exit_plan_mode": "ExitPlanMode"}
# Which tools a rule's tool name covers.
COVERS = {"bash": {"bash"}, "edit": {"edit_file", "write_file"}, "edit_file": {"edit_file"},
          "write": {"write_file"}, "write_file": {"write_file"}, "read": {"read_file"}, "read_file": {"read_file"}}
CONTROL = re.compile(r"[;&|<>`\n]|\$\(")
SEGMENTS = re.compile(r"&&|\|\||[;&|\n]")
RULE = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*(?:\((.*)\))?\s*$", re.S)


def files(user_settings, cwd=None):
    cwd = cwd or os.getcwd()
    paths = [user_settings] if user_settings else []
    return paths + [os.path.join(cwd, p) for p in PROJECT_FILES]


def load(paths):
    """[(path, settings dict)] for the files that exist; a file that isn't valid JSON is skipped
    with a warning in `problems`."""
    found, problems = [], []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            problems.append(f"settings: ignoring {path}: {e}")
            continue
        if isinstance(data, dict):
            found.append((path, data))
        else:
            problems.append(f"settings: ignoring {path}: not a JSON object")
    return found, problems


def rules(settings, kind):
    out = []
    for _, data in settings:
        perms = data.get("permissions")
        if isinstance(perms, dict) and isinstance(perms.get(kind), list):
            out.extend(r for r in perms[kind] if isinstance(r, str))
    return out


def glob_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_matches(spec, path, cwd):
    if not isinstance(path, str) or not path:
        return False
    target = os.path.normpath(os.path.join(cwd, path))
    if spec.startswith("//"):
        pattern = spec[1:]
    elif spec.startswith("~/"):
        pattern = os.path.expanduser(spec)
    else:
        pattern = os.path.join(cwd, spec.removeprefix("./").lstrip("/"))
    return bool(glob_regex(os.path.normpath(pattern)).match(target))


def command_matches(spec, command):
    command = command.strip()
    spec = spec.strip()
    if spec.endswith(":*"):
        prefix = spec[:-2].rstrip()
        return command == prefix or command.startswith(prefix + " ")
    if spec.endswith(" *") and command == spec[:-2].rstrip():
        return True
    if "*" in spec:
        return bool(re.fullmatch(".*".join(re.escape(p) for p in spec.split("*")), command, re.S))
    return command == spec


def rule_matches(rule, tool, args, cwd, allow):
    m = RULE.match(rule)
    if not m:
        return False
    name, spec = m.group(1), m.group(2)
    covered = COVERS.get(name.lower()) or ({tool} if name in (tool, CC_NAMES.get(tool)) else set())
    if tool not in covered:
        return False
    if spec is None or spec.strip() in ("", "*"):
        return True
    if tool == "bash":
        command = str(args.get("command") or "")
        if allow:
            if CONTROL.search(command):
                return command.strip() == spec.strip()
            return command_matches(spec, command)
        parts = [command] + [p for p in SEGMENTS.split(command) if p.strip()]
        return any(command_matches(spec, p) for p in parts)
    if tool in ("read_file", "write_file", "edit_file"):
        return path_matches(spec.strip(), args.get("path"), cwd)
    return False


def decide(settings, tool, args, cwd=None):
    """(decision, rule): 'deny', 'ask' or 'allow' with the first rule that says so, deny first;
    (None, None) when no rule applies."""
    cwd = cwd or os.getcwd()
    for kind in ("deny", "ask", "allow"):
        for rule in rules(settings, kind):
            if rule_matches(rule, tool, args, cwd, allow=(kind == "allow")):
                return kind, rule
    return None, None


def suggest(tool, args, cwd=None):
    """The allow rule an `a` (always) answer saves: this exact command, or this exact file."""
    cwd = cwd or os.getcwd()
    if tool == "bash":
        return f"Bash({str(args.get('command') or '').strip()})"
    if tool in ("write_file", "edit_file"):
        path = os.path.normpath(os.path.join(cwd, str(args.get("path") or "")))
        rel = os.path.relpath(path, cwd)
        return f"Edit({rel})" if not rel.startswith("..") else f"Edit(/{path})"
    return ""


def add(rule, cwd=None):
    path = os.path.join(cwd or os.getcwd(), LOCAL_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    if rule not in allow:
        allow.append(rule)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return path


def main(argv):
    if len(argv) == 3 and argv[1] == "add":
        try:
            path = add(argv[2])
        except (OSError, ValueError) as e:
            sys.stdout.write(f"could not save {argv[2]}: {e}")
            return
        sys.stdout.write(f"saved {argv[2]} to {os.path.relpath(path)}")
    else:
        sys.exit("usage: settings.py add <rule>")


if __name__ == "__main__":
    main(sys.argv)
