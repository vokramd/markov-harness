"""/context: how full the context window is, and with what, like Claude Code's /context.

    python3 window.py request <session> <context>... < {model, system, tools, thinking}   -> {base, system, tools, full}
    python3 window.py report <session> <context_window> <compact_at> <context>... < {counts, model, system, tools}

`request` builds four count_tokens bodies, each adding a part to the one before: a one-word message
alone, then the system blocks, then the tool definitions, then the real messages in place of the
word (as history.py messages builds them, context reminders included). The workflow (context.yaml)
sends them to /v1/messages/count_tokens, and the differences are the exact size of the system
prompt, the tools and the messages, as the next model call would send them.

`report` prints the breakdown to the terminal. The messages are split further, by their share of the
characters, into the context reminders (CLAUDE.md files, the memory index, skills, agent types,
SessionStart hook context) and the conversation itself. If a count failed, every part is an
estimate at 4 characters a token, and the report says so. It also shows compact_at and what the last
model call used (<session>.usage).
"""
import json
import os
import sys

import history
import show

WORD = [{"role": "user", "content": "x"}]
# context file suffix -> label, in the order history.py sends them
LABELS = {".instructions": "CLAUDE.md", ".context": "Memory", ".skills": "Skills", ".agents": "Agent types",
          ".session-start": "SessionStart hooks"}
COLORS = {"System prompt": "\033[90m", "Tools": "\033[35m", "CLAUDE.md": "\033[33m", "Memory": "\033[33m",
          "Skills": "\033[33m", "Agent types": "\033[33m", "SessionStart hooks": "\033[33m", "Messages": "\033[36m"}
CELLS = 40


def bodies(session, contexts, data):
    base = {"model": data.get("model")}
    if data.get("thinking"):
        base["thinking"] = {k: v for k, v in data["thinking"].items() if k == "type"}
    with_system = {**base, "system": data.get("system") or []}
    with_tools = {**with_system, "tools": data.get("tools") or []}
    msgs = history.messages(session, *contexts)
    return {"base": {**base, "messages": WORD}, "system": {**with_system, "messages": WORD},
            "tools": {**with_tools, "messages": WORD}, "full": {**with_tools, "messages": msgs or WORD}}


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def chars(value):
    return len(json.dumps(value, ensure_ascii=False))


def count(response):
    if isinstance(response, dict) and response.get("status_code") == 200 and isinstance(response.get("body"), dict):
        n = response["body"].get("input_tokens")
        return n if isinstance(n, int) else None
    return None


def parts(session, contexts, data):
    """-> ([(label, tokens)], exact, message count)"""
    lines = history.read_lines(session)
    reminders = [(LABELS.get(next((s for s in LABELS if c.endswith(s)), ""), os.path.basename(c)), read(c))
                 for c in contexts]
    reminders = [(label, len(text) + 40) for label, text in reminders if text]
    talk = chars([m["content"] for m in lines]) if lines else 0
    counts = [count(r) for r in data.get("counts") or []]
    exact = len(counts) == 4 and None not in counts
    if exact:
        base, with_system, with_tools, full = counts
        system, tools = with_system - base, with_tools - with_system
        messages = full - with_tools + base if lines else 0
    else:
        system, tools = chars(data.get("system") or []) // 4, chars(data.get("tools") or []) // 4
        messages = (talk + sum(n for _, n in reminders)) // 4 if lines else 0
    out = [("System prompt", system), ("Tools", tools)]
    total_chars = talk + sum(n for _, n in reminders)
    if lines and total_chars:
        split = [(label, round(messages * n / total_chars)) for label, n in reminders]
        out += split + [("Messages", messages - sum(n for _, n in split))]
    return out, exact, len(lines)


def k(n):
    return f"{n / 1000:.1f}".removesuffix(".0") + "k" if n >= 1000 else str(n)


def report(session, window, compact_at, contexts, data):
    rows, exact, n_messages = parts(session, contexts, data)
    window, compact_at = int(window), int(compact_at)
    total = sum(n for _, n in rows)
    out = show.tty()
    D, B, R = show.DIM, show.BOLD, show.RESET
    bar, used = "", 0
    for label, n in rows:
        cells = round(CELLS * n / window) if window else 0
        if n and not cells:
            cells = 1
        cells = min(cells, CELLS - used)
        bar += COLORS.get(label, "") + "⛁" * cells + R
        used += cells
    bar += D + "⛶" * (CELLS - used) + R
    pct = lambda n: f"{100 * n / window:.1f}%" if window else ""
    approx = "" if exact else "~"
    out.write(f"\n  {B}Context usage{R} {D}· {data.get('model')}{R}\n")
    out.write(f"  {bar}  {approx}{k(total)} / {k(window)} tokens ({pct(total)})\n\n")
    width = max(len(label) for label, _ in rows + [("Free space", 0)])
    for label, n in rows:
        if not n and label not in ("System prompt", "Tools"):
            continue
        extra = f"  {D}({n_messages} message{'' if n_messages == 1 else 's'}){R}" if label == "Messages" else ""
        out.write(f"  {COLORS.get(label, '')}⛁{R} {label:<{width}}  {approx}{k(n):>7}  {D}{pct(n):>6}{R}{extra}\n")
    free = max(window - total, 0)
    out.write(f"  {D}⛶{R} {'Free space':<{width}}  {approx}{k(free):>7}  {D}{pct(free):>6}{R}\n\n")
    notes = []
    if compact_at > 0:
        notes.append(f"auto-compacts at {k(compact_at)}" + (f" ({k(compact_at - total)} to go)" if compact_at > total else ""))
    else:
        notes.append("auto-compaction is off")
    last = read(session + ".usage")
    if last.isdigit() and int(last):
        notes.append(f"the last model call used {k(int(last))}")
    if not exact:
        notes.append("estimated at 4 characters a token (token counting failed)")
    out.write(f"  {D}{' · '.join(notes)}{R}\n")
    out.flush()
    return {"total": total, "exact": exact, "rows": rows}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "request" and len(argv) >= 3:
        json.dump(bodies(argv[2], argv[3:], json.load(sys.stdin)), sys.stdout)
    elif cmd == "report" and len(argv) >= 5:
        json.dump(report(argv[2], argv[3], argv[4], argv[5:], json.load(sys.stdin)), sys.stdout)
    else:
        sys.exit("usage: window.py request <session> <context>... | report <session> <window> <compact_at> <context>...")


if __name__ == "__main__":
    main(sys.argv)
