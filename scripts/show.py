"""Print harness output straight to the terminal.

markov captures a step's stdout/stderr, so this writes to /dev/tty (falling back to stderr).

    show.py banner <model> <session>
    show.py gap              (a blank line before a streamed reply)
    show.py reply            < assembled reply JSON (its text has already streamed; prints usage)
    show.py error <status>   < response body JSON or text
    show.py tool <Name> <arg>
    show.py result           < tool_result message JSON
    show.py call <Name> <arg> < tool_result message JSON   (a read-only tool: header and result at once,
                                                             so parallel tools don't interleave)
    show.py exit <code>      (after a command whose output streamed live)
    show.py diff             < edit.py diff JSON
    show.py note <text>      (a dim line: something the harness did on its own)
    show.py skill <name>     < skills.py load message (the skill tool: loaded, or why not)
    show.py agent <type> <description>   (a subagent starts)
    show.py agent-done <uses> <rounds> < its tool_result message
    show.py memory           < memory.py tools JSON (a line naming the memory files it changed, if any)
    show.py memory-error <status> < response body JSON or text (the extraction pass's model call failed)
"""
import json
import os
import re
import sys

DIM, BOLD, RED, GREEN, CYAN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[36m", "\033[0m"
MAX_DIFF_LINES = 40
# bin/harness's memory dir, /state/projects/<work dir with every non-alphanumeric as ->/memory: the
# key is the whole host path, so a tool line naming a memory file would mostly be the key.
MEMORY_DIR = re.compile(r"/state/projects/[^/\s]+/memory(?=/|\s|$)")


def tty():
    try:
        return open("/dev/tty", "w", encoding="utf-8")
    except OSError:
        return sys.stderr


def read_json():
    raw = sys.stdin.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def short(arg):
    """A tool's argument as shown: the memory dir reads as `memory`."""
    return MEMORY_DIR.sub("memory", arg)


def preview(text, lines=6):
    rows = text.rstrip("\n").splitlines() or [""]
    shown = rows[:lines]
    extra = len(rows) - len(shown)
    out = "\n".join("  ⎿ " + r if i == 0 else "    " + r for i, r in enumerate(shown))
    return out + (f"\n    {DIM}… +{extra} lines{RESET}" if extra > 0 else "")


def main(argv):
    out = tty()
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "banner":
        out.write(f"{BOLD}markov-harness{RESET} {DIM}model={argv[2]} session={argv[3]}{RESET}\n")
        out.write(f"{DIM}type /exit to quit{RESET}\n")
    elif cmd == "gap":
        out.write("\n")
    elif cmd == "reply":
        body = read_json()
        usage = body.get("usage", {})
        cache = ""
        if usage.get("cache_read_input_tokens") or usage.get("cache_creation_input_tokens"):
            cache = f" · cache read {usage.get('cache_read_input_tokens', 0)} write {usage.get('cache_creation_input_tokens', 0)}"
        out.write(f"{DIM}  [{body.get('stop_reason')} · in {usage.get('input_tokens')}{cache} · out {usage.get('output_tokens')}]{RESET}\n")
    elif cmd == "error":
        body = read_json()
        msg = body.get("error", {}).get("message", body) if isinstance(body, dict) else body
        kind = body.get("error", {}).get("type", "") if isinstance(body, dict) else ""
        out.write(f"\n{RED}✗ API error {argv[2]} {kind}: {msg}{RESET}\n")
    elif cmd == "tool":
        out.write(f"\n{GREEN}⏺{RESET} {BOLD}{argv[2]}{RESET}({short(argv[3]) if len(argv) > 3 else ''})\n")
    elif cmd in ("result", "call"):
        msg = read_json()
        if cmd == "call":
            out.write(f"\n{GREEN}⏺{RESET} {BOLD}{argv[2]}{RESET}({short(argv[3]) if len(argv) > 3 else ''})\n")
        for block in msg.get("content", []):
            text = block.get("content", "")
            color = RED if block.get("is_error") else DIM
            out.write(f"{color}{preview(text)}{RESET}\n")
    elif cmd == "exit":
        code = argv[2]
        out.write(f"{DIM if code == '0' else RED}  ⎿ exit {code}{RESET}\n")
    elif cmd == "diff":
        result = read_json()
        if not result.get("ok"):
            out.write(f"{RED}  ⎿ {result.get('error')}{RESET}\n")
        else:
            rows = result.get("diff", "").splitlines()
            for row in rows[2:2 + MAX_DIFF_LINES]:  # skip the ---/+++ header
                color = GREEN if row.startswith("+") else RED if row.startswith("-") else CYAN if row.startswith("@@") else DIM
                out.write(f"    {color}{row}{RESET}\n")
            if len(rows) - 2 > MAX_DIFF_LINES:
                out.write(f"    {DIM}… +{len(rows) - 2 - MAX_DIFF_LINES} lines{RESET}\n")
    elif cmd == "skill":
        result = next((b for b in read_json().get("content", []) if b.get("type") == "tool_result"), {})
        out.write(f"\n{GREEN}⏺{RESET} {BOLD}Skill{RESET}({argv[2] if len(argv) > 2 else ''})\n")
        if result.get("is_error"):
            out.write(f"{RED}  ⎿ {result.get('content')}{RESET}\n")
        else:
            out.write(f"{DIM}  ⎿ Successfully loaded skill{RESET}\n")
    elif cmd == "agent":
        out.write(f"\n{GREEN}⏺{RESET} {BOLD}{argv[2]}{RESET}({argv[3] if len(argv) > 3 else ''})\n")
    elif cmd == "agent-done":
        result = next((b for b in read_json().get("content", []) if b.get("type") == "tool_result"), {})
        if result.get("is_error"):
            out.write(f"{RED}  ⎿ {str(result.get('content')).splitlines()[0]}{RESET}\n")
        else:
            uses = argv[2]
            out.write(f"{DIM}  ⎿ Done ({uses} tool use{'' if uses == '1' else 's'} · {argv[3]} model calls){RESET}\n")
    elif cmd == "note":
        out.write(f"{DIM}  ✻ {argv[2]}{RESET}\n")
    elif cmd == "memory":
        names = sorted({os.path.basename(p) for p in read_json().get("written", [])})
        if names:
            out.write(f"{DIM}  ✻ memory updated: {', '.join(names)}{RESET}\n")
    elif cmd == "memory-error":
        body = read_json()
        msg = body.get("error", {}).get("message", body) if isinstance(body, dict) else body
        out.write(f"{DIM}  ✻ memory extraction skipped: model call failed ({argv[2]}): {msg}{RESET}\n")
    else:
        sys.exit(f"unknown command {cmd!r}")
    out.flush()


if __name__ == "__main__":
    main(sys.argv)
