"""What the user typed, turned into what the harness does with it.

    python3 input.py < {value, skill_dirs, agent_dirs}   -> {message, skill, mentions, command, error}

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
import skills

AGENT_COMMAND = re.compile(r"^/agent(?:\s+(\S+))?(?:\s+(.*))?$", re.S)
MENTION = re.compile(r"(?<![\w@])@agent-([A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9]|[A-Za-z0-9])")
REMINDER = ('<system-reminder>The user has expressed a desire to invoke the agent "{name}". Please invoke the '
            "agent appropriately, passing in the required context to it.</system-reminder>")


def expand(value, skill_dirs, agent_dirs):
    out = {"message": None, "skill": None, "mentions": [], "command": None, "error": None}
    text = value.strip()
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
    if not mentions:
        return {**out, "message": {"role": "user", "content": value}}
    blocks = [{"type": "text", "text": value}] + [{"type": "text", "text": REMINDER.format(name=n)} for n in mentions]
    return {**out, "message": {"role": "user", "content": blocks}, "mentions": mentions}


def main():
    data = json.load(sys.stdin)
    json.dump(expand(data.get("value") or "", data.get("skill_dirs") or [], data.get("agent_dirs") or []), sys.stdout)


if __name__ == "__main__":
    main()
