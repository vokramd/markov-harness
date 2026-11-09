"""What the user typed, turned into what the harness does with it.

    python3 input.py < {value, skill_dirs, agent_dirs}   -> {message, skill, mentions, command, compact, context, session_command, files, error}

- `/help`, `/clear`, `/cost`, `/resume`, `/plan`, `/model [name]` and `/mode [name]` are session commands
  (session.py command): `session_command` is {name, args}, and `message` is null.
- `@path` words naming a file or directory attach it (attachments.mentions): its contents, or its
  listing, as reminders after the text, the way Claude Code adds a Read call; `files` lists them.
- `/context` shows how full the context window is (context workflow): `context` is true, and
  `message` is null.
- `/compact [instructions]` compacts the conversation now (compact workflow): `compact` is
  {instructions}, and `message` is null.

- `/agent <type> <task>` is a command the workflow runs itself, with no model call: `command` is
  {subagent_type, prompt}, and `message` is null (tools.agent's loop runs the subagent; turn records it).
- Another `/<name> [args]` loads a skill (skills.user): the command tags and the skill's text.
- `@agent-<name>` anywhere in plain text, naming an agent type, adds Claude Code's reminder that the user
  wants that agent used, after the text. The model still decides how to call it. `mentions` lists them;
  a name that is no agent type is left as plain text.
- `error` is set, and nothing is sent, for an unknown /<word> or a malformed /agent.
"""
import json
import re
import sys

import agents
import attachments
import skills

CONTEXT_COMMAND = re.compile(r"^/context\s*$")
SESSION_COMMAND = re.compile(r"^/(help|clear|cost|resume|plan|model|mode)(?:\s+(.*))?$", re.S)
NO_ARGS = ("help", "clear", "cost", "resume", "plan")
COMPACT_COMMAND = re.compile(r"^/compact(?:\s+(.*))?$", re.S)
AGENT_COMMAND = re.compile(r"^/agent(?:\s+(\S+))?(?:\s+(.*))?$", re.S)
MENTION = re.compile(r"(?<![\w@])@agent-([A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9]|[A-Za-z0-9])")
REMINDER = ('<system-reminder>The user has expressed a desire to invoke the agent "{name}". Please invoke the '
            "agent appropriately, passing in the required context to it.</system-reminder>")


def expand(value, skill_dirs, agent_dirs, session="", max_chars=30000):
    out = {"message": None, "skill": None, "mentions": [], "command": None, "compact": None, "context": False,
           "session_command": None, "files": [], "error": None}
    text = value.strip()
    if CONTEXT_COMMAND.match(text):
        return {**out, "context": True}
    m = SESSION_COMMAND.match(text)
    if m and not (m.group(1) in NO_ARGS and m.group(2)):
        return {**out, "session_command": {"name": m.group(1), "args": (m.group(2) or "").strip()}}
    m = COMPACT_COMMAND.match(text)
    if m:
        return {**out, "compact": {"instructions": (m.group(1) or "").strip()}}
    m = AGENT_COMMAND.match(text)
    if m:
        kind, task = m.group(1), (m.group(2) or "").strip()
        types = agents.discover(agent_dirs)
        if not kind or not task:
            out["error"] = f"Usage: /agent <type> <task>. Types: {', '.join(types) or 'none'}"
        elif kind not in types:
            out["error"] = f"Unknown agent type: {kind}. Types: {', '.join(types) or 'none'}"
        else:
            out["command"] = {"subagent_type": kind, "prompt": task, "args": f"{kind} {task}"}
        return out
    if text.startswith("/"):
        return {**out, **skills.user(skill_dirs, value)}
    types = agents.discover(agent_dirs)
    mentions = []
    for name in MENTION.findall(value):
        if name in types and name not in mentions:
            mentions.append(name)
    files, attached = attachments.mentions(session, value, int(max_chars))
    if not mentions and not files:
        return {**out, "message": {"role": "user", "content": value}}
    blocks = [{"type": "text", "text": value}] + files + [{"type": "text", "text": REMINDER.format(name=n)} for n in mentions]
    return {**out, "message": {"role": "user", "content": blocks}, "mentions": mentions, "files": attached}


def main():
    data = json.load(sys.stdin)
    json.dump(expand(data.get("value") or "", data.get("skill_dirs") or [], data.get("agent_dirs") or [],
                     data.get("session") or "", data.get("max_chars") or 30000), sys.stdout)


if __name__ == "__main__":
    main()
