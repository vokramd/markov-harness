"""What Claude Code attaches to the conversation on its own: reminders before a model call, and the
files a message @-mentions.

    python3 attachments.py before <session>         -> {added: [kinds]}; appends the reminders to the session
    python3 attachments.py track <session> <path>   (after read_file, write_file and edit_file succeed)

`before` runs before each main-loop model call and appends one user message of <system-reminder>
blocks (history.py merges it into the last user message), when any applies:

- plan mode: while the mode is plan (session.py), Claude Code's plan-mode instructions, once per user
  turn; and once after plan mode is left with /mode or /plan, that it was left (exit_plan_mode's own
  result says so when the user approves a plan).
- todos: when the todo tool hasn't been used for TODO_TURNS assistant messages, and no such reminder
  came in as many, a gentle reminder to use it, with the current list (todos.py).
- changed files: a file the session read or wrote (tracked in <session>.reads with its content) that has
  changed since, by the user, a linter or a command: the changed lines, with line numbers, so the model
  doesn't work from a stale copy. Each change is reported once.

`mentions` (for input.py) turns @path words in a message into the reminders Claude Code adds for them:
the file's contents as a Read call's result, or a directory's listing, and tracks the files.
"""
import difflib
import json
import os
import re
import sys

import history
import memory
import session as state
import todos as todo_list

TODO_TURNS = 10
MAX_TRACKED = 256 * 1024     # bigger files are tracked by mtime only (a change is reported without lines)
MAX_DIFF_LINES = 60
MENTION = re.compile(r'(?<![\w@/])@("[^"]+"|[^\s"]+)')

PLAN = """Plan mode is active. The user indicated that they do not want you to execute yet -- you MUST NOT make any edits (with the exception of the todo list), run any non-readonly tools (including changing configs or making commits), or otherwise make any changes to the system. This supersedes any other instructions you have received.

## What to do instead
1. Answer the user's query comprehensively, using read-only tools (read_file, glob, grep, and bash only for commands that change nothing) and the Explore agent to research.
2. When you have a plan, present it by calling the exit_plan_mode tool with the plan in markdown. That asks the user to approve it. Don't ask for approval any other way, and don't call exit_plan_mode for questions that need no plan; just answer them.
3. Do NOT make any file changes or run any tools that modify the system until the user has approved the plan."""
PLAN_EXITED = "## Exited Plan Mode\n\nYou have exited plan mode. You can now make edits, run tools, and take actions."
TODO = ("The todo tool hasn't been used recently. If you're working on tasks that would benefit from tracking progress, "
        "consider using the todo_write tool to track progress. Also consider cleaning up the todo list if has become "
        "stale and no longer matches what you are working on. Only use it if it's relevant to the current work. This is "
        "just a gentle reminder - ignore if not applicable. Make sure that you NEVER mention this reminder to the user\n")
CHANGED = ("Note: {path} was modified, either by the user or by a linter. This change was intentional, so make sure to "
           "take it into account as you proceed (ie. don't revert it unless the user asks you to). Don't tell the user "
           "this, since they are already aware. Here are the relevant changes (shown with line numbers):\n{changes}")


def reminder(text):
    return {"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}


# ---- tracked files ----

def reads_path(session):
    return session + ".reads"


def load_reads(session):
    try:
        with open(reads_path(session), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_reads(session, reads):
    tmp = reads_path(session) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reads, f)
    os.replace(tmp, reads_path(session))


def snapshot(path):
    """{mtime, text} of a file now (text None when too big or not text), or None when it's gone."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    text = None
    if st.st_size <= MAX_TRACKED:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            text = None
    return {"mtime": st.st_mtime_ns, "text": text}


def track(session, path):
    full = os.path.abspath(path)
    if not os.path.isfile(full):
        return
    reads = load_reads(session)
    reads[full] = snapshot(full)
    save_reads(session, reads)


def numbered(lines, start):
    return "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(lines, start))


def changes(old, new):
    """The changed lines of new, with their line numbers and a line of context, or a note."""
    if old is None or new is None:
        return "(the file is too big or not text; read it again to see it)"
    a, b = old.splitlines(), new.splitlines()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            out.append(f"(lines {i1 + 1}-{i2} of the old version were removed; they were after line {j1} now)")
            continue
        lo, hi = max(j1 - 1, 0), min(j2 + 1, len(b))
        out.append(numbered(b[lo:hi], lo + 1))
    rows = "\n...\n".join(out).splitlines()
    if len(rows) > MAX_DIFF_LINES:
        rows = rows[:MAX_DIFF_LINES] + [f"... ({len(rows) - MAX_DIFF_LINES} more lines; read the file to see them)"]
    return "\n".join(rows)


def changed_files(session):
    reads, texts, dirty = load_reads(session), [], False
    for path, seen in list(reads.items()):
        now = snapshot(path)
        if now is None:
            del reads[path]     # deleted: nothing to show; a later read fails and says so
            dirty = True
            continue
        if seen and now["mtime"] == seen.get("mtime"):
            continue
        reads[path], dirty = now, True
        if seen and now["text"] is not None and now["text"] == seen.get("text"):
            continue            # touched, not changed
        texts.append(CHANGED.format(path=path, changes=changes((seen or {}).get("text"), now["text"])))
    if dirty:
        save_reads(session, reads)
    return texts


# ---- before a model call ----

def turn_start(lines):
    """True when the session ends with what the user just typed: the turn's first call."""
    return bool(lines) and lines[-1]["role"] == "user" and memory.user_text(lines[-1]) is not None


def stale_todos(session, lines, st):
    assistants = [m for m in lines if m["role"] == "assistant"]
    count = len(assistants)
    last = max((i for i, m in enumerate(assistants) for b in m["content"]
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "todo_write"), default=-1)
    since_write = count - (last + 1)
    since_reminder = count - int(st.get("todo_reminded_at") or 0)
    if since_write >= TODO_TURNS and since_reminder >= TODO_TURNS:
        state.update(session, todo_reminded_at=count)
        items = todo_list.load(session)
        return TODO + (f"\n\nHere are the existing contents of your todo list:\n\n{todo_list.as_text(items)}" if items else "")
    return None


def before(session):
    lines = history.read_lines(session)
    if not lines or lines[-1]["role"] != "user":
        return {"added": []}
    st, blocks, added = state.load(session), [], []
    if st.get("mode") == "plan" and turn_start(lines):
        blocks.append(reminder(PLAN))
        added.append("plan")
    elif st.get("plan_exited") and st.get("mode") != "plan":
        blocks.append(reminder(PLAN_EXITED))
        added.append("plan_exited")
    if st.get("plan_exited"):
        state.update(session, plan_exited=False)
    todo = stale_todos(session, lines, st)
    if todo:
        blocks.append(reminder(todo))
        added.append("todos")
    for text in changed_files(session):
        blocks.append(reminder(text))
        added.append("changed_file")
    if blocks:
        with open(session, "a", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "content": blocks}) + "\n")
    return {"added": added}


# ---- @mentions ----

def mentions(session, text, max_chars):
    """-> (reminder blocks, attached paths) for the @path words in text that name a file or directory."""
    blocks, attached = [], []
    for raw in MENTION.findall(text):
        word = raw.strip('"')
        if word.startswith("agent-"):
            continue                       # @agent-<name>: input.py's
        path = word
        while path and not os.path.exists(path) and path[-1] in ".,;:!?)'":
            path = path[:-1]
        if not path or not os.path.exists(path):
            continue
        full = os.path.abspath(path)
        if full in attached:
            continue
        if os.path.isdir(full):
            entries = sorted(e + ("/" if os.path.isdir(os.path.join(full, e)) else "") for e in os.listdir(full))
            listing = "\n".join(entries[:500]) + (f"\n... ({len(entries) - 500} more)" if len(entries) > 500 else "")
            call = json.dumps({"command": f"ls {full}", "description": f"Lists files in {full}"})
            blocks += [reminder(f"Called the Bash tool with the following input: {call}"),
                       reminder(f"Result of calling the Bash tool:\n{listing}")]
        else:
            try:
                with open(full, encoding="utf-8") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError) as e:
                content = f"error: cannot read {full}: {e}"
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n... (truncated at {max_chars} characters; read_file shows the rest)"
            call = json.dumps({"file_path": full})
            blocks += [reminder(f"Called the Read tool with the following input: {call}"),
                       reminder(f"Result of calling the Read tool:\n{numbered(content.splitlines(), 1)}")]
            if session:
                track(session, full)
        attached.append(full)
    return blocks, attached


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "before" and len(argv) == 3:
        json.dump(before(argv[2]), sys.stdout)
    elif cmd == "track" and len(argv) == 4:
        track(argv[2], argv[3])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
