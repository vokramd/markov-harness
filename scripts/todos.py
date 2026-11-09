"""The todo list, Claude Code's TodoWrite: the model keeps a checklist for multi-step work.

    python3 todos.py write <session> < tool_use block   -> the tool_result message (and shows the list)
    python3 todos.py show <session>                      (prints the list)

The list lives in <session>.todos, a JSON array of {content, status, activeForm}; each call replaces
it whole. Only one item should be in_progress at a time; the harness says so in the result rather than
refusing. attachments.py reminds the model of the list when it hasn't used the tool for a while.
"""
import json
import sys

STATUSES = ("pending", "in_progress", "completed")
RESULT = ("Todos have been modified successfully. Ensure that you continue to use the todo list to track your "
          "progress. Please proceed with the current tasks if applicable")
DIM, BOLD, GREEN, RESET, STRIKE = "\033[2m", "\033[1m", "\033[32m", "\033[0m", "\033[9m"


def path(session):
    return session + ".todos"


def load(session):
    try:
        with open(path(session), encoding="utf-8") as f:
            todos = json.load(f)
            return todos if isinstance(todos, list) else []
    except (OSError, ValueError):
        return []


def check(todos):
    """-> error text, or None when the list is well-formed."""
    if not isinstance(todos, list):
        return "todos must be an array"
    for i, t in enumerate(todos):
        if not isinstance(t, dict) or not str(t.get("content") or "").strip():
            return f"todos[{i}] needs a non-empty content"
        if t.get("status") not in STATUSES:
            return f"todos[{i}].status must be one of {', '.join(STATUSES)}"
    return None


def render(todos):
    if not todos:
        return f"  {DIM}⎿ (empty todo list){RESET}"
    rows = []
    for i, t in enumerate(todos):
        lead = "  ⎿ " if i == 0 else "    "
        if t["status"] == "completed":
            rows.append(f"{lead}{GREEN}☒{RESET} {DIM}{STRIKE}{t['content']}{RESET}")
        elif t["status"] == "in_progress":
            rows.append(f"{lead}☐ {BOLD}{t.get('activeForm') or t['content']}{RESET}")
        else:
            rows.append(f"{lead}☐ {t['content']}")
    return "\n".join(rows)


def as_text(todos):
    """The list as the model sees it in a reminder."""
    return json.dumps([{k: t.get(k) for k in ("content", "status", "activeForm") if t.get(k)} for t in todos])


def write(session, block):
    todos = (block.get("input") or {}).get("todos")
    error = check(todos)
    result = {"type": "tool_result", "tool_use_id": block.get("id")}
    if error:
        result.update(is_error=True, content=f"error: {error}")
    else:
        todos = [{"content": str(t["content"]).strip(), "status": t["status"],
                  "activeForm": str(t.get("activeForm") or "").strip()} for t in todos]
        with open(path(session), "w", encoding="utf-8") as f:
            json.dump(todos, f)
        busy = sum(1 for t in todos if t["status"] == "in_progress")
        result["content"] = RESULT + ("" if busy <= 1 else f"\n\nNote: {busy} items are in_progress; keep exactly one in_progress at a time.")
    return {"role": "user", "content": [result]}, error


def main(argv):
    from show import tty   # noqa: PLC0415
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "write" and len(argv) == 3:
        message, error = write(argv[2], json.load(sys.stdin))
        out = tty()
        out.write(f"\n{GREEN}⏺{RESET} {BOLD}Update Todos{RESET}\n")
        out.write((f"\033[31m  ⎿ {error}{RESET}" if error else render(load(argv[2]))) + "\n")
        out.flush()
        json.dump(message, sys.stdout)
    elif cmd == "show" and len(argv) == 3:
        out = tty()
        out.write(render(load(argv[2])) + "\n")
        out.flush()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
