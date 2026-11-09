"""Subagents, modelled on Claude Code's: the agent tool hands a task to a fresh agent with its own system
prompt, tools and conversation, and only its final answer comes back as the tool result.

    python3 agents.py context <out> <dir>...                 -> writes the listing reminder to out
    python3 agents.py start <session> <model> <dir>... < {block, tools}   -> {ok, error, ...} (see start)
    python3 agents.py result <sub_session> <status> < tool_use block       -> {message, text, error, uses, rounds}

An agent type is a markdown file, <dir>/<name>.md: frontmatter with `name`, `description`, and optionally
`tools` (a comma-separated or [bracketed] list; default every tool but the agent tool) and `model`
(haiku, sonnet, opus, inherit, or a model id; default inherit), then the system prompt. The dirs are
searched in order and the first agent of a name wins: the harness passes its own agents/ folder (the
built-in general-purpose and Explore), then the personal dir, then the work dir's .claude/agents.

Tool names may be Claude Code's (Read, Write, Edit, Bash, Glob, Grep, Skill): they map to the harness's.
A subagent never gets the agent tool, so it can't start subagents of its own.

`start` writes the subagent's session file, <session>.agent-<tool_use_id>.jsonl, whose first message is
the task, and returns what its loop needs: `sub_session`, `system` (the identity block is added by the
workflow), `tools` (the definitions it may use), `allowed` (their names) and `model`. When it
can't start one (no prompt, an unknown type), `ok` is false and `message` is the error tool_result.
"""
import json
import os
import re
import sys

from skills import frontmatter

AGENT_TOOL = "agent"
MAIN_ONLY = {AGENT_TOOL, "todo_write", "exit_plan_mode"}   # the main agent's: subagents never get them
ALIASES = {"read": "read_file", "write": "write_file", "edit": "edit_file", "multiedit": "edit_file",
           "bash": "bash", "glob": "glob", "grep": "grep", "skill": "skill"}
MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}

PREAMBLE = """You are a subagent of markov-harness, started by its main agent to do one task. The task is the first user message. You can't ask the user or the main agent anything, so work with what you have.

Your final message is all the main agent sees of your work: make it a complete, self-contained answer to the task, with the file paths, names and facts it asked for. Don't narrate what you did.

The working directory is {cwd}."""


def names(value):
    """A frontmatter tools value as harness tool names: "Read, Grep", "[read_file, grep]" or "*"."""
    if value is None or str(value).strip() in ("", "*"):
        return None
    parts = [p.strip().strip("\"'") for p in str(value).strip().strip("[]").split(",")]
    return [ALIASES.get(p.lower(), p) for p in parts if p]


def discover(dirs):
    found = {}
    for root in dirs:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".md") or entry.lower() == "readme.md":
                continue
            try:
                with open(os.path.join(root, entry), encoding="utf-8") as f:
                    meta, body = frontmatter(f.read())
            except (OSError, UnicodeDecodeError):
                continue
            name = meta.get("name") or entry[:-3]
            if name in found or not body.strip():
                continue
            found[name] = {"name": name, "description": (meta.get("description") or "").strip(),
                           "tools": names(meta.get("tools")), "model": (meta.get("model") or "inherit").strip(),
                           "prompt": body.strip()}
    return found


def listing(agents):
    if not agents:
        return ""
    lines = []
    for a in agents.values():
        tools = ", ".join(a["tools"]) if a["tools"] else "all tools except agent"
        lines.append(f"- {a['name']}: {a['description']} (Tools: {tools})")
    return "Available agent types for the agent tool:\n" + "\n".join(lines)


def refusal(block, error):
    return {"ok": False, "error": error, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": block.get("id"), "is_error": True, "content": error}]}}


def start(session, model, dirs, block, tools):
    tool_input = block.get("input") or {}
    kind = str(tool_input.get("subagent_type") or "general-purpose").strip()
    task = str(tool_input.get("prompt") or "").strip()
    agents = discover(dirs)
    if not task:
        return refusal(block, "prompt is required")
    if kind not in agents:
        return refusal(block, f"Unknown agent type: {kind}. Available: {', '.join(agents) or 'none'}")
    agent = agents[kind]
    wanted = agent["tools"]
    defs = [t for t in tools if t["name"] not in MAIN_ONLY and (wanted is None or t["name"] in wanted)]
    sub_session = f"{session}.agent-{re.sub(r'[^A-Za-z0-9_-]', '', str(block.get('id')))}.jsonl"
    with open(sub_session, "w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": task}) + "\n")
    chosen = agent["model"]
    return {"ok": True, "error": None, "name": kind, "sub_session": sub_session,
            "system": PREAMBLE.format(cwd=os.getcwd()) + "\n\n" + agent["prompt"],
            "tools": defs, "allowed": [t["name"] for t in defs],
            "model": model if chosen in ("", "inherit") else MODELS.get(chosen.lower(), chosen)}


def result(sub_session, status, block):
    """The tool_result for the main agent: the subagent's last text, or why there is none."""
    msgs = []
    try:
        with open(sub_session, encoding="utf-8") as f:
            msgs = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        pass
    replies = [m for m in msgs if m["role"] == "assistant"]
    uses = sum(1 for m in replies for b in m["content"] if b.get("type") == "tool_use")
    text = ""
    if replies:
        text = "\n".join(b.get("text", "") for b in replies[-1]["content"] if b.get("type") == "text").strip()
    error = None
    if status == "failed" or not replies:
        error = "The subagent's model call failed before it answered."
    elif status == "max":
        error = "The subagent ran out of tool rounds before finishing." + (f" Its last message:\n\n{text}" if text else "")
    elif not text:
        error = "The subagent finished without a final message."
    content = {"type": "tool_result", "tool_use_id": block.get("id"), "content": error or text}
    if error:
        content["is_error"] = True
    return {"message": {"role": "user", "content": [content]}, "text": text, "error": error,
            "uses": uses, "rounds": len(replies)}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "context" and len(argv) >= 3:
        with open(argv[2], "w", encoding="utf-8") as f:
            f.write(listing(discover(argv[3:])))
    elif cmd == "start" and len(argv) >= 4:
        data = json.load(sys.stdin)
        json.dump(start(argv[2], argv[3], argv[4:], data["block"], data["tools"]), sys.stdout)
    elif cmd == "result" and len(argv) == 4:
        json.dump(result(argv[2], argv[3], json.load(sys.stdin)), sys.stdout)
    else:
        sys.exit("usage: agents.py context <out> <dir>... | start <session> <model> <dir>... < {block, tools} "
                 "| result <sub_session> <status> < block")


if __name__ == "__main__":
    main(sys.argv)
