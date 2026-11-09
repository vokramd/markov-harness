"""Hooks and the tool-call gate, as in Claude Code.

    python3 hooks.py pre    <session> <user_settings> < {reply, approve, memory_dir, mutating_tools}
        -> {calls: [{block, decision, reason, suggest}], post, notes}
    python3 hooks.py post   <session> <user_settings> < {calls}             -> {message, notes}
    python3 hooks.py prompt <session> <user_settings> < {message, prompt}   -> {message, blocked, reason, notes}
    python3 hooks.py stop   <session> <user_settings>                       -> {block, message, notes}
    python3 hooks.py subagent-stop <sub_session> <user_settings> <agent_type>  -> {block, message, notes}
    python3 hooks.py session-start <session> <user_settings> <source>   -> writes <session>.session-start
    python3 hooks.py session-end   <session> <user_settings> <reason>
    python3 hooks.py pre-compact   <session> <user_settings> <trigger> < {instructions}
        -> {blocked, instructions, notes}

Hooks are configured in the settings files (see settings.py) under "hooks": for each event, a list of
{matcher, hooks: [{type: "command", command, timeout}]}. The matcher is a regular expression over
Claude Code's tool name (Bash, Read, Write, Edit, Glob, Grep, Skill, Agent) for the tool events, the
source for SessionStart (startup, resume, compact), the trigger for PreCompact (manual, auto), the
reason for SessionEnd (prompt_input_exit, other) and the agent type for SubagentStop; empty or "*"
matches everything, and UserPromptSubmit and Stop ignore it. A hook command runs with `sh -c` in the work dir,
gets the event as JSON on stdin (session_id, transcript_path, cwd, hook_event_name, permission_mode,
and the event's own fields), and answers with its exit code and output:

    exit 0   success; stdout may be JSON (below). For UserPromptSubmit and SessionStart, plain stdout is
             added as context; for PreCompact, it's added to the summary instructions.
    exit 2   blocking: stderr goes to the model (PreToolUse: the call is denied; PostToolUse: feedback;
             Stop, SubagentStop: the agent keeps going) or, for UserPromptSubmit, to the user, and the
             prompt is dropped; for PreCompact, the compaction is cancelled. SessionStart and
             SessionEnd can't block.
    other    a non-blocking error, shown to the user.

JSON stdout: {"decision": "block", "reason": ...} (PostToolUse, UserPromptSubmit, Stop, SubagentStop,
PreCompact),
{"hookSpecificOutput": {"permissionDecision": "allow"|"deny"|"ask", "permissionDecisionReason": ...,
"updatedInput": {...}}} (PreToolUse), {"hookSpecificOutput": {"additionalContext": ...}} (PostToolUse,
UserPromptSubmit, SessionStart, PreCompact), and {"systemMessage": ...} (any, shown to the user).

`pre` decides every tool call of a reply before it runs: allow (run it), ask (prompt first) or deny
(answer with an error). In order: a hook's deny, a deny rule, (non-mutating tools run), plan mode's
deny of file changes, a hook's ask, an ask rule, a hook's allow, an allow rule; then the permission
mode (session.py): bypassPermissions runs everything, acceptEdits runs file changes in the work dir;
otherwise mutating tools ask unless the path is a .md file in the memory dir. Tool input uses the harness's names (path, not file_path);
hooks see both.
"""
import json
import os
import re
import subprocess
import sys

import history
import memory
import session as session_state
import settings

DEFAULT_TIMEOUT = 60
FILE_TOOLS = ("write_file", "edit_file")
PLAN_DENIED = ("Plan mode is active, so files can't be changed yet. Finish researching, then present your plan "
               "with the exit_plan_mode tool; the user approves it before any changes are made.")
STOP_FEEDBACK = "Stop hook feedback:"


def payload(session, event, mode=None, **fields):
    return {"session_id": os.path.basename(session).removesuffix(".jsonl"), "transcript_path": session,
            "cwd": os.getcwd(), "hook_event_name": event,
            "permission_mode": mode or session_state.mode(main_session(session)), **fields}


def configured(loaded, event, tool=None):
    """The hook commands for an event (and tool), in settings order."""
    out = []
    for _, data in loaded:
        groups = (data.get("hooks") or {}).get(event) if isinstance(data.get("hooks"), dict) else None
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            matcher = str(group.get("matcher") or "")
            if tool and matcher not in ("", "*"):
                try:
                    if not re.fullmatch(matcher, tool):
                        continue
                except re.error:
                    continue
            for hook in group.get("hooks") or []:
                if isinstance(hook, dict) and hook.get("type", "command") == "command" and hook.get("command"):
                    out.append(hook)
    return out


def run(hook, data):
    """-> (exit code or None on timeout, stdout, stderr, parsed JSON or None)"""
    timeout = hook.get("timeout") or DEFAULT_TIMEOUT
    try:
        p = subprocess.run(["sh", "-c", hook["command"]], input=json.dumps(data), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout}s", None
    out = p.stdout.strip()
    parsed = None
    if out.startswith("{"):
        try:
            parsed = json.loads(out)
        except ValueError:
            parsed = None
    return p.returncode, out, p.stderr.strip(), parsed if isinstance(parsed, dict) else None


def inside_workdir(path):
    if not isinstance(path, str) or not path:
        return False
    full, root = os.path.realpath(path), os.path.realpath(os.getcwd())
    return full == root or full.startswith(root + os.sep)


def label(event, tool=None):
    return f"{event}:{tool}" if tool else event


def note_failure(notes, event, hook, code, stderr, tool=None):
    what = "timed out" if code is None else f"failed (exit {code})"
    notes.append(f"{label(event, tool)} hook {what}: {hook['command']}" + (f": {stderr}" if stderr else ""))


def system_message(notes, parsed):
    if parsed and parsed.get("systemMessage"):
        notes.append(str(parsed["systemMessage"]))


def hook_input(name, args):
    """Tool input as hooks see it: Claude Code's file_path next to the harness's path."""
    out = dict(args)
    if "path" in out and name in ("read_file", "write_file", "edit_file"):
        out.setdefault("file_path", out["path"])
    return out


def from_hook_input(name, updated, original):
    out = dict(updated)
    if name in ("read_file", "write_file", "edit_file") and "file_path" in out:
        out["path"] = out.pop("file_path")
    return {**original, **out}


def pre_hooks(loaded, session, mode, block, notes):
    """-> (decision or None, reason, block) from the PreToolUse hooks."""
    name, tool = block.get("name"), settings.CC_NAMES.get(block.get("name"), block.get("name"))
    decisions = {}
    for hook in configured(loaded, "PreToolUse", tool):
        data = payload(session, "PreToolUse", mode, tool_name=tool, tool_use_id=block.get("id"),
                       tool_input=hook_input(name, block.get("input") or {}))
        code, out, err, parsed = run(hook, data)
        system_message(notes, parsed)
        if code == 2:
            decisions.setdefault("deny", err or f"blocked by {hook['command']}")
            continue
        if code != 0:
            note_failure(notes, "PreToolUse", hook, code, err, tool)
            continue
        specific = (parsed or {}).get("hookSpecificOutput") or {}
        decision = specific.get("permissionDecision") or {"approve": "allow", "block": "deny"}.get((parsed or {}).get("decision"))
        reason = specific.get("permissionDecisionReason") or (parsed or {}).get("reason") or ""
        if isinstance(specific.get("updatedInput"), dict):
            block = {**block, "input": from_hook_input(name, specific["updatedInput"], block.get("input") or {})}
        if decision in ("allow", "ask", "deny"):
            decisions.setdefault(decision, reason)
    for decision in ("deny", "ask", "allow"):
        if decision in decisions:
            return decision, decisions[decision], block
    return None, "", block


def pre(session, user_settings, data):
    loaded, notes = settings.load(settings.files(user_settings))
    reply, approve = data.get("reply") or {}, data.get("approve") or "ask"
    mode = session_state.mode(main_session(session), "bypassPermissions" if approve == "auto" else "default")
    mutating, invalid = set(data.get("mutating_tools") or []), set(reply.get("invalid") or [])
    calls = []
    for block in reply.get("content") or []:
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        tool = settings.CC_NAMES.get(name, name)
        call = {"block": block, "decision": "allow", "reason": "", "suggest": ""}
        calls.append(call)
        if block.get("id") in invalid:
            continue   # answered as invalid input; never run, so neither hooks nor rules apply
        hook, hook_reason, block = pre_hooks(loaded, session, mode, block, notes)
        call["block"] = block
        args = block.get("input") or {}
        rule_decision, rule = settings.decide(loaded, name, args)
        if hook == "deny":
            call.update(decision="deny", reason=f"PreToolUse:{tool} hook denied this tool call" + (f": {hook_reason}" if hook_reason else "."))
        elif rule_decision == "deny":
            call.update(decision="deny", reason=f"Permission to use {tool} has been denied by the rule {rule}.")
        elif name not in mutating:
            pass   # read-only, skill and agent calls run; a subagent's own tools are gated in its loop
        elif mode == "plan" and name in FILE_TOOLS:
            call.update(decision="deny", reason=PLAN_DENIED)
        elif hook == "ask" or rule_decision == "ask":
            call["decision"] = "ask"
        elif hook == "allow" or rule_decision == "allow":
            call["reason"] = f"allowed by the PreToolUse hook" if hook == "allow" else f"allowed by {rule}"
        elif mode == "bypassPermissions":
            pass
        elif mode == "acceptEdits" and name in FILE_TOOLS and inside_workdir(args.get("path")):
            call["reason"] = "allowed in accept-edits mode"
        elif name in ("write_file", "edit_file") and memory.owns(data.get("memory_dir") or "", args.get("path")):
            pass   # memory files need no approval
        else:
            call["decision"] = "ask"
        if call["decision"] == "ask":
            call["suggest"] = settings.suggest(name, args)
    post = any(configured(loaded, "PostToolUse", settings.CC_NAMES.get(c["block"].get("name"))) for c in calls)
    return {"calls": calls, "post": post, "notes": notes}


def results(session):
    """tool_use_id -> its tool_result block, from the session file."""
    found = {}
    try:
        with open(session, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                m = json.loads(line)
                if m.get("role") == "user" and isinstance(m.get("content"), list):
                    for b in m["content"]:
                        if b.get("type") == "tool_result":
                            found[b.get("tool_use_id")] = b
    except FileNotFoundError:
        pass
    return found


def reminder(text):
    return {"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}


def post(session, user_settings, data):
    loaded, notes = settings.load(settings.files(user_settings))
    done = results(session)
    texts = []
    for call in data.get("calls") or []:
        block = call.get("block") or {}
        if call.get("decision") == "deny" or block.get("id") not in done:
            continue
        name = block.get("name")
        tool = settings.CC_NAMES.get(name, name)
        result = done[block["id"]]
        content = result.get("content")
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        for hook in configured(loaded, "PostToolUse", tool):
            code, out, err, parsed = run(hook, payload(
                session, "PostToolUse", tool_name=tool, tool_use_id=block["id"],
                tool_input=hook_input(name, block.get("input") or {}),
                tool_response={"content": content, "is_error": bool(result.get("is_error"))}))
            system_message(notes, parsed)
            if code == 2:
                texts.append(f"PostToolUse:{tool} hook blocking error from command \"{hook['command']}\": {err}")
            elif code != 0:
                note_failure(notes, "PostToolUse", hook, code, err, tool)
            elif parsed:
                if parsed.get("decision") == "block":
                    texts.append(f"PostToolUse:{tool} hook feedback: {parsed.get('reason', '')}")
                extra = (parsed.get("hookSpecificOutput") or {}).get("additionalContext")
                if extra:
                    texts.append(f"PostToolUse:{tool} hook additional context: {extra}")
    message = {"role": "user", "content": [reminder(t) for t in texts]} if texts else None
    return {"message": message, "notes": notes}


def blocked(reason, notes):
    notes.append(f"UserPromptSubmit hook blocked the prompt: {reason}")
    return {"message": None, "blocked": True, "reason": reason, "notes": notes}


def prompt(session, user_settings, data):
    loaded, notes = settings.load(settings.files(user_settings))
    message, text = data.get("message"), data.get("prompt") or ""
    context = []
    for hook in configured(loaded, "UserPromptSubmit"):
        code, out, err, parsed = run(hook, payload(session, "UserPromptSubmit", prompt=text))
        system_message(notes, parsed)
        if code == 2:
            return blocked(err or f"blocked by {hook['command']}", notes)
        if code != 0:
            note_failure(notes, "UserPromptSubmit", hook, code, err)
            continue
        if parsed:
            if parsed.get("decision") == "block":
                return blocked(parsed.get("reason") or "blocked by a hook", notes)
            extra = (parsed.get("hookSpecificOutput") or {}).get("additionalContext")
            if extra:
                context.append(str(extra))
        elif out:
            context.append(out)
    if context and message:
        content = message["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
        blocks += [reminder(f"UserPromptSubmit hook additional context: {c}") for c in context]
        message = {**message, "content": blocks}
    return {"message": message, "blocked": False, "reason": "", "notes": notes}


def last_text(lines):
    if not lines or lines[-1]["role"] != "assistant":
        return ""
    return next((b.get("text", "") for b in reversed(lines[-1]["content"]) if b.get("type") == "text"), "")


def main_session(session):
    """The main session of a subagent's session file (agents.py: <session>.agent-<id>.jsonl)."""
    return re.sub(r"\.agent-[^/]*\.jsonl$", "", session)


def stop(session, user_settings, agent_type=None):
    """Stop hooks when the main agent is done, or SubagentStop hooks (matched on the agent type) when
    a subagent is: one may send it back to work with a reason."""
    event = "SubagentStop" if agent_type else "Stop"
    loaded, notes = settings.load(settings.files(user_settings))
    hooks = configured(loaded, event, agent_type)
    if not hooks:
        return {"block": False, "message": None, "notes": notes}
    lines = history.read_lines(session)
    feedback = f"{event} hook feedback:"
    users = [m for m in lines if m["role"] == "user"]
    last = users[-1]["content"] if users else ""
    active = isinstance(last, list) and bool(last) and str(last[0].get("text", "")).startswith(feedback)
    fields = {"stop_hook_active": active, "last_assistant_message": last_text(lines)}
    if agent_type:
        main = main_session(session)
        fields.update(agent_type=agent_type, agent_transcript_path=session,
                      agent_id=os.path.basename(session)[len(os.path.basename(main)) + len(".agent-"):].removesuffix(".jsonl"))
        session = main
    reasons = []
    for hook in hooks:
        code, out, err, parsed = run(hook, payload(session, event, **fields))
        system_message(notes, parsed)
        if code == 2:
            reasons.append(err or f"blocked by {hook['command']}")
        elif code != 0:
            note_failure(notes, event, hook, code, err)
        elif parsed and parsed.get("decision") == "block":
            reasons.append(parsed.get("reason") or f"blocked by {hook['command']}")
    if not reasons:
        return {"block": False, "message": None, "notes": notes}
    text = feedback + "\n" + "\n".join(reasons)
    who = f"{agent_type} agent" if agent_type else "agent"
    notes.append(f"{event} hook sent the {who} back: " + " / ".join(reasons))
    return {"block": True, "message": {"role": "user", "content": [{"type": "text", "text": text}]}, "notes": notes}


def context_hooks(loaded, session, event, matcher, notes, **fields):
    """Run an event's hooks; -> (blocked reason or None, the context they added). Plain stdout or
    hookSpecificOutput.additionalContext on exit 0 is context; exit 2 blocks."""
    context = []
    for hook in configured(loaded, event, matcher):
        code, out, err, parsed = run(hook, payload(session, event, **fields))
        system_message(notes, parsed)
        if code == 2:
            return err or f"blocked by {hook['command']}", context
        if code != 0:
            note_failure(notes, event, hook, code, err)
            continue
        if parsed:
            if parsed.get("decision") == "block":
                return parsed.get("reason") or f"blocked by {hook['command']}", context
            extra = (parsed.get("hookSpecificOutput") or {}).get("additionalContext")
            if extra:
                context.append(str(extra))
        elif out:
            context.append(out)
    return None, context


def session_start(session, user_settings, source):
    """SessionStart hooks (matched on the source: startup, resume or compact). What they add is written
    to <session>.session-start, a context file every main-loop call sends (history.py messages), so it
    lasts the session, and is made again after a compaction. Exit 2 doesn't block anything."""
    loaded, notes = settings.load(settings.files(user_settings))
    _, context = context_hooks(loaded, session, "SessionStart", source, notes, source=source)
    with open(session + ".session-start", "w", encoding="utf-8") as f:
        f.write("\n\n".join(f"SessionStart:{source} hook additional context: {c}" for c in context))
    return {"context": len(context), "notes": notes}


def session_end(session, user_settings, reason):
    """SessionEnd hooks (matched on the reason): for cleanup and logging; they can't block."""
    loaded, notes = settings.load(settings.files(user_settings))
    for hook in configured(loaded, "SessionEnd", reason):
        code, out, err, parsed = run(hook, payload(session, "SessionEnd", reason=reason))
        system_message(notes, parsed)
        if code not in (0, 2):
            note_failure(notes, "SessionEnd", hook, code, err)
    return {"notes": notes}


def pre_compact(session, user_settings, trigger, data):
    """PreCompact hooks (matched on the trigger: manual or auto). What they print is added to the
    summary instructions; exit 2 (or decision: block) cancels this compaction."""
    loaded, notes = settings.load(settings.files(user_settings))
    instructions = (data.get("instructions") or "").strip()
    reason, context = context_hooks(loaded, session, "PreCompact", trigger, notes, trigger=trigger,
                                    custom_instructions=instructions)
    if reason:
        notes.append(f"PreCompact hook blocked the compaction: {reason}")
        return {"blocked": True, "instructions": instructions, "notes": notes}
    return {"blocked": False, "instructions": "\n\n".join([instructions] * bool(instructions) + context), "notes": notes}


def show_notes(notes):
    """For the events whose workflow step has nothing else to do with the output: print the notes."""
    if notes:
        import show
        out = show.tty()
        for note in notes:
            out.write(f"{show.DIM}  ✻ {note}{show.RESET}\n")
        out.flush()


USAGE = """usage: hooks.py pre|post|prompt <session> <user_settings> < input JSON
       hooks.py stop <session> <user_settings>
       hooks.py subagent-stop <sub_session> <user_settings> <agent_type>
       hooks.py session-start <session> <user_settings> <source>      (prints its notes)
       hooks.py session-end <session> <user_settings> <reason>        (prints its notes)
       hooks.py pre-compact <session> <user_settings> <trigger> < {instructions}"""


def main(argv):
    cmd, rest = (argv[1], argv[2:]) if len(argv) > 1 else ("", [])
    if cmd in ("pre", "post", "prompt") and len(rest) == 2:
        out = {"pre": pre, "post": post, "prompt": prompt}[cmd](*rest, json.load(sys.stdin))
    elif cmd == "stop" and len(rest) == 2:
        out = stop(*rest)
    elif cmd == "subagent-stop" and len(rest) == 3:
        out = stop(*rest)
    elif cmd == "session-start" and len(rest) == 3:
        out = session_start(*rest)
        show_notes(out["notes"])
    elif cmd == "session-end" and len(rest) == 3:
        out = session_end(*rest)
        show_notes(out["notes"])
    elif cmd == "pre-compact" and len(rest) == 3:
        out = pre_compact(*rest, json.load(sys.stdin))
    else:
        sys.exit(USAGE)
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main(sys.argv)
