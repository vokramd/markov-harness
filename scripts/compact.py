"""Compaction: replace a long conversation with a summary of it, as Claude Code does.

    python3 compact.py check <session> <compact_at>                  -> prints true or false
    python3 compact.py request <keep> <session> [<context>...] < {instructions}   -> {ok, error, messages}
    python3 compact.py apply <keep> <session> <manual> < assembled reply    -> {ok, error, archive, chars, kept}

`check` reads <session>.usage, the size of the context at the last model call (input, cache read,
cache write and output tokens, written by agent_step), and says whether it reached compact_at. It
runs before each main-loop model call, so a long turn is compacted between its tool rounds too.

`request` is the conversation (as history.py messages builds it, reminders included) with the
summary instructions after it. When the session ends with a message the user just typed (an automatic
compaction before a turn's first call), that message is left out of the summary and kept verbatim
after it (`split`), so the model still answers it, as text in the last user message or a new one. So
are the latest messages that fit in <keep> tokens (compact_keep), so the model sees its recent work
exactly rather than as a summary. The workflow sends it
with the main loop's system blocks and tools, so it reads their prompt cache; the instructions tell
the model not to call tools.

`apply` takes the summary from the reply (inside <summary> tags if it used them), copies the old session
to <session>.compacted-<n>.jsonl, and rewrites the session as one user message: the summary, the
archive's path (the model can read_file it for details), and for an automatic compaction in the
middle of a turn with no typed message kept, an instruction to carry on without asking; then the kept messages. The usage file is reset, and so is the list of nested
instruction files already sent (instructions.py), so they are sent again when needed. Nothing is removed: each compaction
keeps its own archive.
"""
import json
import os
import re
import shutil
import sys

import history
import memory

PROMPT = """Your task is to create a detailed summary of the conversation so far. It will replace the conversation: after this, you will see only the summary, so it must hold everything needed to continue the work without losing context. Do not call any tools; answer with text only.

First, inside <analysis> tags, go through the conversation in order and note each request, your approach, the decisions made, the files, code and commands involved, the errors and how they were fixed, and the user's feedback, especially where they asked for something to be done differently.

Then write the summary inside <summary> tags, with these sections:

1. Primary Request and Intent: everything the user asked for, in detail.
2. Key Technical Concepts: the technologies, conventions and facts the work relies on.
3. Files and Code Sections: each file read, changed or created, why it matters, and the important code, with snippets where they help.
4. Errors and fixes: each error, how it was fixed, and any user feedback about it.
5. Problem Solving: what was solved and what is still open.
6. All user messages: every message the user wrote (not tool results), in order.
7. Pending Tasks: what was asked for and not done yet.
8. Current Work: exactly what was being done just before this summary, with file names and code.
9. Optional Next Step: the next step, only if it follows directly from the user's latest request, quoting the request so the task is not misread."""

CONTINUE = ("Continue the conversation from where it left off without asking the user any further questions. "
            "Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface "
            "with \"I'll continue\" or similar. Pick up the last task as if the break never happened.")


def usage_path(session):
    return session + ".usage"


def check(session, compact_at):
    try:
        with open(usage_path(session), encoding="utf-8") as f:
            used = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return False
    return int(compact_at) > 0 and used >= int(compact_at)


def size(m):
    """A rough token count of one message (4 characters a token), for compact_keep."""
    return len(json.dumps(m["content"])) // 4 + 1


def has_results(m):
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in history.blocks(m["content"]))


def split(lines, keep=0):
    """(to summarize, to keep verbatim after the summary).

    When the session ends with what the user just typed (the turn's first model call), that is always
    kept, or the summary would swallow the question and report it answered. Then, back from there,
    the most recent messages that fit in `keep` tokens (compact_keep) are kept too, so the model still
    sees its latest work exactly. The kept part starts at an assistant message or at a user message
    without tool results, so no tool_result is left without its tool_use, and at least one assistant
    message is left to summarize."""
    i = len(lines)
    while i > 0 and lines[i - 1]["role"] == "user" and not has_results(lines[i - 1]):
        i -= 1
    if not any(memory.user_text(m) is not None for m in lines[i:]):
        i = len(lines)
    first = next((n for n, m in enumerate(lines) if m["role"] == "assistant"), len(lines))
    budget, cut, j = int(keep or 0) - sum(size(m) for m in lines[i:]), i, i
    while j - 1 > first:
        j -= 1
        budget -= size(lines[j])
        if budget < 0:
            break
        if lines[j]["role"] == "assistant" or not has_results(lines[j]):
            cut = j
    return lines[:cut], lines[cut:]


def request(session, contexts, instructions, keep=0):
    head, _ = split(history.read_lines(session), keep)
    msgs = history.merge(head, *contexts)
    if not any(m["role"] == "assistant" for m in msgs):
        return {"ok": False, "error": "nothing to compact yet", "messages": []}
    prompt = PROMPT + (f"\n\nThe user's instructions for this summary: {instructions}" if instructions else "")
    text = {"type": "text", "text": prompt}
    if msgs[-1]["role"] == "user":
        msgs[-1]["content"] = history.blocks(msgs[-1]["content"]) + [text]
    else:
        msgs.append({"role": "user", "content": [text]})
    return {"ok": True, "error": None, "messages": msgs}


def summary_text(reply):
    text = "\n".join(b.get("text", "") for b in reply.get("content") or [] if b.get("type") == "text").strip()
    m = re.search(r"<summary>(.*?)(?:</summary>|$)", text, re.S)
    if m:
        text = m.group(1).strip()
    else:
        text = re.sub(r"<analysis>.*?(?:</analysis>|$)", "", text, flags=re.S).strip()
    return text


def apply(session, manual, reply, keep=0):
    if reply.get("error"):
        error = reply["error"]
        if isinstance(error, dict):
            error = (error.get("error") or {}).get("message") or json.dumps(error)
        return {"ok": False, "error": f"the summary call failed ({reply.get('status')}): {error}"}
    summary = summary_text(reply)
    if not summary:
        return {"ok": False, "error": f"the model wrote no summary (stop_reason {reply.get('stop_reason')})"}
    n = 1
    while os.path.exists(f"{session}.compacted-{n}.jsonl"):
        n += 1
    archive = f"{session}.compacted-{n}.jsonl"
    _, tail = split(history.read_lines(session), keep)
    shutil.copyfile(session, archive)
    text = ("This session is being continued from a previous conversation that ran out of context. "
            "The summary below covers the earlier portion of the conversation.\n\n"
            f"{summary}\n\n"
            "If you need specific details from before compaction (like exact code snippets, error messages, "
            f"or content you generated), read the full transcript at: {archive}")
    if tail:
        text += "\n\nThe most recent messages follow verbatim."
    if not manual and not any(memory.user_text(m) is not None for m in tail):
        text += "\n\n" + CONTINUE
    message = {"role": "user", "content": [{"type": "text", "text": text}]}
    tmp = session + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for m in [message] + tail:
            f.write(json.dumps(m) + "\n")
    os.replace(tmp, session)
    with open(usage_path(session), "w", encoding="utf-8") as f:
        f.write("0")
    if os.path.exists(session + ".nested"):   # nested instructions sent before are gone with the old messages
        open(session + ".nested", "w", encoding="utf-8").close()
    return {"ok": True, "error": None, "archive": archive, "chars": len(summary), "kept": len(tail)}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "check" and len(argv) == 4:
        sys.stdout.write("true" if check(argv[2], argv[3]) else "false")
    elif cmd == "request" and len(argv) >= 4:
        data = json.load(sys.stdin)
        json.dump(request(argv[3], argv[4:], (data.get("instructions") or "").strip(), argv[2]), sys.stdout)
    elif cmd == "apply" and len(argv) == 5:
        json.dump(apply(argv[3], argv[4] == "true", json.load(sys.stdin), argv[2]), sys.stdout)
    else:
        sys.exit("usage: compact.py check <session> <compact_at> | request <keep> <session> [<context>...] | apply <keep> <session> <manual>")


if __name__ == "__main__":
    main(sys.argv)
