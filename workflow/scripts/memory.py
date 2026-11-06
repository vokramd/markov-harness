"""Persistent memory, modelled on Claude Code's auto-memory: one file per memory in a directory under
the harness state dir, plus a MEMORY.md index that is loaded into each session.

    python3 memory.py context <memory_dir> <out>   -> creates memory_dir; writes the index reminder to out
    python3 memory.py owns <memory_dir> <path>     -> prints true if path is a .md file inside memory_dir
    python3 memory.py fork <session> <memory_dir> <fork>   -> {run, reason}; when run, writes the fork
    python3 memory.py tools <memory_dir> < assembled reply  -> {message, written}

`context` runs once per session, so the reminder is a snapshot: memories written during the session
don't change the first message, which keeps the prompt cache valid. The index is cut at 200 lines,
then 25,000 characters (at a line end), with a warning appended, as Claude Code does. out is empty
when there is no index yet. `owns` is the approval check: writes and edits it accepts skip the prompt.

`fork` and `tools` are the extraction pass that runs after each turn. `fork` decides whether the turn
is worth a pass: the turn finished, its user message has at least 3 words, and the main agent didn't
already write to memory in it. If so it writes the fork session, the parent's messages plus Claude
Code's extraction prompt, so the fork's requests share the parent's cached prefix. `tools` runs the
fork's tool calls itself, without approval: read_file, glob and grep anywhere, write_file and
edit_file only inside memory_dir, and bash only as `rm [-f]` of .md files inside it. Anything else is
answered with an error. `message` is the tool_result user message (null when the reply has no tool
calls); `written` lists the memory files it changed.
"""
import json
import os
import shlex
import sys
import time

import edit
import search

INDEX = "MEMORY.md"
MAX_LINES = 200
MAX_CHARS = 25000
MAX_TOOL_OUTPUT = 30000
MIN_WORDS = 3

EXTRACT_PROMPT = """You are now acting as the memory extraction subagent. Analyze the most recent ~{n} messages above and use them to update your persistent memory systems.

Available tools: read_file, grep, glob, write_file and edit_file for paths inside the memory directory only, and bash `rm` of .md files inside the memory directory only (rm takes no flags except -f). All other tools — any other bash command included — will be denied.

You have a limited turn budget, so the efficient strategy is: turn 1 — issue all read_file calls in parallel for every file you might update; turn 2 — issue all write_file/edit_file calls in parallel. Do not interleave reads and writes across multiple turns.

You MUST only use content from the last ~{n} messages to update your persistent memories. Do not waste any turns attempting to investigate or verify that content further — no grepping source files, no reading code to confirm a pattern exists, no git commands.

## Existing memory files

{files}

Check this list before writing — update an existing file rather than creating a duplicate.

If nothing is worth saving, output only 'Nothing to save.' Do not explain why.

If the user explicitly asks you to remember something, save it immediately as whichever type fits best. If they ask you to forget something, find and remove the relevant entry.

Apply the memory types, what-not-to-save criteria, and frontmatter format from the Memory section of your system prompt — it is already in your context above."""


def truncate(text):
    lines = text.splitlines()
    kept, notes = lines, []
    if len(lines) > MAX_LINES:
        kept = lines[:MAX_LINES]
        notes.append(f"{len(lines)} lines (limit: {MAX_LINES})")
    body = "\n".join(kept)
    if len(body) > MAX_CHARS:
        cut = body.rfind("\n", 0, MAX_CHARS)
        body = body[:cut if cut > 0 else MAX_CHARS]
        notes.append(f"{len(text)} characters (limit: {MAX_CHARS})")
    if notes:
        body += (f"\n\n> WARNING: {INDEX} is {' and '.join(notes)}. Only part of it was loaded. "
                 "Keep index entries to one line under ~200 chars; move detail into topic files.")
    return body


def context(memory_dir):
    os.makedirs(memory_dir, exist_ok=True)
    path = os.path.join(memory_dir, INDEX)
    try:
        with open(path, encoding="utf-8") as f:
            index = f.read().strip()
    except FileNotFoundError:
        return ""
    if not index:
        return ""
    return f"Contents of {path} (your memory index, persists across sessions):\n\n{truncate(index)}"


def owns(memory_dir, path):
    if not isinstance(path, str) or not path:
        return False
    root = os.path.realpath(memory_dir)
    target = os.path.realpath(path)
    return target.endswith(".md") and os.path.commonpath([root, target]) == root and target != root


# ---- extraction ----

def frontmatter(path):
    """The type and description from a memory file's frontmatter (both may be empty)."""
    kind = description = ""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read(4096).splitlines()
    except (OSError, UnicodeDecodeError):
        return kind, description
    if not lines or lines[0].strip() != "---":
        return kind, description
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.strip().partition(":")
        if key == "type":
            kind = value.strip()
        elif key == "description":
            description = value.strip()
    return kind, description


def memory_files(memory_dir):
    """One line per memory file, newest first: - [type] name.md (modified): description"""
    found = []
    for dirpath, dirnames, filenames in os.walk(memory_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, memory_dir)
            if name.endswith(".md") and rel != INDEX:
                found.append((os.path.getmtime(path), rel, path))
    lines = []
    for mtime, rel, path in sorted(found, reverse=True):
        kind, description = frontmatter(path)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
        lines.append(f"- [{kind or 'unknown'}] {rel} ({stamp}): {description}")
    return lines


def read_session(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def fork(session, memory_dir, fork_path):
    lines = read_session(session)
    if not lines or lines[-1]["role"] != "assistant":
        return {"run": False, "reason": "the turn did not finish"}
    starts = [i for i, m in enumerate(lines) if m["role"] == "user" and isinstance(m["content"], str)]
    if not starts:
        return {"run": False, "reason": "no user message"}
    turn = lines[starts[-1]:]
    if len(turn[0]["content"].split()) < MIN_WORDS:
        return {"run": False, "reason": f"user message under {MIN_WORDS} words"}
    for m in turn:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_use" and b.get("name") in ("write_file", "edit_file") \
                        and owns(memory_dir, (b.get("input") or {}).get("path")):
                    return {"run": False, "reason": "the main agent already wrote memory this turn"}
    n = 1 + sum(1 for a, b in zip(turn, turn[1:]) if a["role"] != b["role"])   # messages, as the API sees them
    files = "\n".join(memory_files(memory_dir)) or "(none yet)"
    prompt = {"role": "user", "content": EXTRACT_PROMPT.format(n=n, files=files)}
    with open(fork_path, "w", encoding="utf-8") as f:
        for m in lines + [prompt]:
            f.write(json.dumps(m) + "\n")
    return {"run": True, "reason": f"{n} messages"}


def run_tool(memory_dir, block, written):
    """One fork tool call -> (content, is_error)."""
    name, args = block.get("name"), block.get("input") or {}
    if name in ("read_file", "glob", "grep"):
        out = getattr(search, {"read_file": "read"}.get(name, name))(args)
        return out[:MAX_TOOL_OUTPUT], out.startswith("error:")
    if name in ("write_file", "edit_file"):
        path = args.get("path")
        if not owns(memory_dir, path):
            return f"denied: the memory extraction agent may only write .md files inside {memory_dir}", True
        if name == "write_file":
            content = args.get("content")
            if not isinstance(content, str):
                return "error: content must be a string", True
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            written.append(path)
            return f"wrote {len(content.encode())} bytes to {path}", False
        before, after, count = edit.plan(args)
        if before is None:
            return f"error: {count}", True
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(after)
        written.append(path)
        return f"edited {path}: replaced {count} occurrence{'s' if count != 1 else ''}", False
    if name == "bash":
        try:
            words = shlex.split(args.get("command", ""))
        except ValueError:
            words = []
        paths = [w for w in words[1:] if w != "-f"]
        if words[:1] == ["rm"] and paths and all(owns(memory_dir, p) for p in paths):
            removed = []
            for p in paths:
                try:
                    os.remove(p)
                    removed.append(p)
                except FileNotFoundError:
                    if "-f" not in words:
                        return f"error: {p} does not exist", True
            written.extend(removed)
            return f"removed {', '.join(removed) or 'nothing'}", False
        return f"denied: the memory extraction agent may only run bash as rm of .md files inside {memory_dir}", True
    return f"denied: {name} is not available to the memory extraction agent", True


def tools(memory_dir, reply):
    written, results = [], []
    for block in reply.get("content") or []:
        if block.get("type") != "tool_use":
            continue
        if block.get("id") in (reply.get("invalid") or []):
            content, is_error = f"The input for {block.get('name')} was not a complete JSON object, so it was not run.", True
        else:
            content, is_error = run_tool(memory_dir, block, written)
        result = {"type": "tool_result", "tool_use_id": block.get("id"), "content": content}
        if is_error:
            result["is_error"] = True
        results.append(result)
    message = {"role": "user", "content": results} if results else None
    return {"message": message, "written": written}


def main(argv):
    if len(argv) == 4 and argv[1] == "context":
        with open(argv[3], "w", encoding="utf-8") as f:
            f.write(context(argv[2]))
    elif len(argv) == 4 and argv[1] == "owns":
        sys.stdout.write("true" if owns(argv[2], argv[3]) else "false")
    elif len(argv) == 5 and argv[1] == "fork":
        json.dump(fork(argv[2], argv[3], argv[4]), sys.stdout)
    elif len(argv) == 3 and argv[1] == "tools":
        json.dump(tools(argv[2], json.load(sys.stdin)), sys.stdout)
    else:
        sys.exit("usage: memory.py context <memory_dir> <out> | owns <memory_dir> <path> | "
                 "fork <session> <memory_dir> <fork> | tools <memory_dir> < reply")


if __name__ == "__main__":
    main(sys.argv)
