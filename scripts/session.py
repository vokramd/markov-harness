"""Session state and the session commands: permission modes, /model, /clear, /cost, /resume, /help.

    python3 session.py init <session> <approve> <permission_mode> <host_workdir>
    python3 session.py status <session> <default_model>          -> {model, mode, label}
    python3 session.py exit-plan <session> <y|a|n|""> < tool_use block   -> the tool_result message
    python3 session.py command <session> < {name, args, default_model, user_settings, prices, sessions_dir, host_workdir}
    python3 session.py list <sessions_dir> <host_workdir> [latest]   (bin/harness, on the host)

<session>.state holds what a command can change mid-session: the permission `mode` and the `model`
("" is the model var). The gate (hooks.py pre) reads the mode, and turn reads both at the start of
each turn (`status`) and passes the model on, shadowing the global, to everything it calls.

Permission modes, as in Claude Code (`/mode` cycles default -> acceptEdits -> plan):
  default            mutating tools ask (unless a rule or hook decides)
  acceptEdits        write_file and edit_file in the work dir run without asking; bash still asks
  plan               write_file and edit_file are denied; the model researches and presents a plan
                     with exit_plan_mode, which asks the user to approve it (attachments.py reminds it)
  bypassPermissions  nothing asks (approve=auto starts here); deny and ask rules still apply

<session>.meta records the host work dir and start time, for `list` (bin/harness --resume, --continue).
<session>.costs has one line per model call ({model, kind, usage}); /cost prices them with `prices`.
"""
import datetime
import json
import os
import shutil
import sys

MODES = ["default", "acceptEdits", "plan", "bypassPermissions"]
CYCLE = ["default", "acceptEdits", "plan"]
ALIASES = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
LABELS = {"acceptEdits": "⏵⏵ accept edits on", "plan": "⏸ plan mode on", "bypassPermissions": "⏵⏵ bypass permissions on"}
HELP = """Commands:
  /help                 this list
  /clear                start a fresh conversation (the old one is kept in an archive file)
  /compact [focus]      summarize the conversation to free context
  /context              how full the context window is
  /cost                 tokens used this session, and what they cost at API prices
  /model [name]         show or switch the model (haiku, sonnet, opus or a model id)
  /mode [name]          cycle or set the permission mode (default, acceptEdits, plan, bypassPermissions)
  /plan                 toggle plan mode
  /resume               list this work dir's sessions (bin/harness --resume picks one)
  /agent <type> <task>  run a subagent directly
  /<skill> [args]       run a skill
  /exit                 quit
  @path                 attach a file or directory listing to the message"""


def state_path(session):
    return session + ".state"


def load(session):
    try:
        with open(state_path(session), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(session, state):
    tmp = state_path(session) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, state_path(session))


def update(session, **changes):
    state = load(session)
    state.update(changes)
    save(session, state)
    return state


def mode(session, fallback="default"):
    m = load(session).get("mode")
    return m if m in MODES else fallback


def set_mode(session, new):
    """Change the mode; leaving plan mode some other way than exit_plan_mode tells the model."""
    old = mode(session)
    changes = {"mode": new}
    if old == "plan" and new != "plan":
        changes["plan_exited"] = True
    if new == "plan":
        changes["plan_exited"] = False
    update(session, **changes)
    return old


def init(session, approve, permission_mode, host_workdir):
    start = "bypassPermissions" if approve == "auto" else (permission_mode if permission_mode in MODES else "default")
    update(session, mode=start, plan_exited=False)
    meta = session + ".meta"
    if not os.path.exists(meta):
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"workdir": host_workdir, "started": datetime.datetime.now().isoformat(timespec="seconds")}, f)


def status(session, default_model):
    state = load(session)
    m = mode(session)
    label = LABELS.get(m, "")
    if label:
        label += " (/mode to change)"
    return {"model": state.get("model") or default_model, "mode": m, "label": label}


def tty():
    try:
        return open("/dev/tty", "w", encoding="utf-8")
    except OSError:
        return sys.stderr


DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def note(out, text):
    out.write(f"{DIM}  ✻ {text}{RESET}\n")


# ---- /cost ----

def price_of(prices, model, usage):
    """USD for one call, or None when the model has no price."""
    p = prices.get(model)
    if not isinstance(p, dict):
        return None
    prompt = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    if p.get("long") and prompt > int(p.get("long_above") or 0):
        p = {**p, **p["long"]}
    per = lambda key: float(p.get(key) or 0) / 1e6
    return (int(usage.get("input_tokens") or 0) * per("input") + int(usage.get("output_tokens") or 0) * per("output")
            + int(usage.get("cache_read_input_tokens") or 0) * per("cache_read")
            + int(usage.get("cache_creation_input_tokens") or 0) * per("cache_write"))


def k(n):
    return f"{n / 1000:.1f}".removesuffix(".0") + "k" if n >= 1000 else str(n)


def cost(session, prices):
    by_model, kinds, total, unpriced = {}, {}, 0.0, set()
    try:
        with open(session + ".costs", encoding="utf-8") as f:
            calls = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        calls = []
    for call in calls:
        model, usage = call.get("model") or "?", call.get("usage") or {}
        row = by_model.setdefault(model, {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0})
        row["calls"] += 1
        row["input"] += int(usage.get("input_tokens") or 0)
        row["output"] += int(usage.get("output_tokens") or 0)
        row["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        row["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        usd = price_of(prices, model, usage)
        if usd is None:
            unpriced.add(model)
        else:
            row["usd"] += usd
            total += usd
        kinds[call.get("kind") or "main"] = kinds.get(call.get("kind") or "main", 0) + 1
    return {"calls": len(calls), "by_model": by_model, "kinds": kinds, "usd": total, "unpriced": sorted(unpriced)}


def show_cost(out, session, prices):
    c = cost(session, prices)
    if not c["calls"]:
        note(out, "no model calls yet")
        return
    kinds = ", ".join(f"{n} {kind}" for kind, n in sorted(c["kinds"].items()))
    out.write(f"\n  {BOLD}Total cost:{RESET}  ${c['usd']:.4f}  {DIM}(at API prices){RESET}\n")
    out.write(f"  {BOLD}Model calls:{RESET} {c['calls']}  {DIM}({kinds}){RESET}\n")
    out.write(f"  {BOLD}Usage by model:{RESET}\n")
    for model, r in c["by_model"].items():
        usd = "no price" if model in c["unpriced"] else f"${r['usd']:.4f}"
        out.write(f"    {model}:  {k(r['input'])} input, {k(r['output'])} output, {k(r['cache_read'])} cache read, "
                  f"{k(r['cache_write'])} cache write  {DIM}({usd}){RESET}\n")


# ---- sessions ----

def first_prompt(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = json.loads(line)
                if m.get("role") != "user":
                    continue
                content = m["content"]
                text = content if isinstance(content, str) else next(
                    (b.get("text", "") for b in content if b.get("type") == "text" and not b.get("text", "").startswith("<")), "")
                if text and not text.startswith("This session is being continued"):
                    return " ".join(text.split())
    except (OSError, ValueError):
        pass
    return ""


def sessions(sessions_dir, host_workdir):
    """This work dir's sessions, newest first: [{path, name, modified, messages, prompt}]."""
    found = []
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        return []
    for name in names:
        if not name.endswith(".jsonl") or name.count(".") != 1:
            continue          # <id>.jsonl only: not archives, forks or subagent sessions
        path = os.path.join(sessions_dir, name)
        try:
            with open(path + ".meta", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if meta.get("workdir") != host_workdir:
            continue
        with open(path, encoding="utf-8") as f:
            count = sum(1 for line in f if line.strip())
        if not count:
            continue
        found.append({"path": path, "name": name, "modified": os.path.getmtime(path), "messages": count,
                      "prompt": first_prompt(path)})
    return sorted(found, key=lambda s: s["modified"], reverse=True)


def listing(found, limit=20):
    rows = []
    for i, s in enumerate(found[:limit], 1):
        when = datetime.datetime.fromtimestamp(s["modified"]).strftime("%Y-%m-%d %H:%M")
        prompt = s["prompt"][:60] + ("…" if len(s["prompt"]) > 60 else "")
        rows.append(f"  {i:>2}. {when}  {s['messages']:>4} msgs  {prompt or '(no prompt)'}")
    return "\n".join(rows)


# ---- /clear ----

def clear(session):
    """Archive the conversation and start an empty one in the same file. The mode and model stay."""
    n = 1
    while os.path.exists(f"{session}.cleared-{n}.jsonl"):
        n += 1
    archive = f"{session}.cleared-{n}.jsonl"
    if os.path.exists(session):
        shutil.copyfile(session, archive)
    open(session, "w", encoding="utf-8").close()
    for suffix, empty in ((".usage", "0"), (".nested", ""), (".todos", "[]"), (".reads", "{}")):
        if os.path.exists(session + suffix):
            with open(session + suffix, "w", encoding="utf-8") as f:
                f.write(empty)
    update(session, todo_reminded_at=0, plan_exited=False)
    return archive


# ---- exit_plan_mode ----

APPROVED = ("User has approved your plan. You can now start coding. Start with updating your todo list if applicable"
            "\n\n## Approved Plan:\n{plan}")
REJECTED = ("The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, "
            "the new_string was NOT written to the file). STOP what you are doing and wait for the user to tell you how "
            "to proceed. You are still in plan mode.")
NOT_PLANNING = "You are not in plan mode. This tool is only for presenting a plan for approval in plan mode."


def exit_plan(session, choice, block):
    """The tool_result for exit_plan_mode, after the user's answer (empty: not in plan mode)."""
    result = {"type": "tool_result", "tool_use_id": block.get("id")}
    out = tty()
    if not choice:
        result.update(is_error=True, content=NOT_PLANNING)
    elif choice.lower() in ("y", "a"):
        new = "acceptEdits" if choice.lower() == "a" else "default"
        update(session, mode=new, plan_exited=False)    # the result says so; no reminder needed
        result["content"] = APPROVED.format(plan=(block.get("input") or {}).get("plan") or "")
        note(out, f"plan approved; permission mode: {new}")
    else:
        result.update(is_error=True, content=REJECTED)
        note(out, "plan not approved; still in plan mode")
    out.flush()
    return {"role": "user", "content": [result]}


# ---- commands ----

def command(session, data):
    import hooks   # only here: hooks imports this module for the mode
    out = tty()
    name, args = data.get("name"), (data.get("args") or "").strip()
    if name == "help":
        out.write("\n" + HELP + "\n")
    elif name in ("mode", "plan"):
        current = mode(session)
        if name == "plan":
            new = "default" if current == "plan" else "plan"
        elif args:
            new = next((m for m in MODES if m.lower() == args.lower() or m.lower().startswith(args.lower())), None)
            if not new:
                note(out, f"unknown mode {args!r}: {', '.join(MODES)}")
                new = current
        else:
            new = CYCLE[(CYCLE.index(current) + 1) % len(CYCLE)] if current in CYCLE else "default"
        if new != current:
            set_mode(session, new)
        note(out, f"permission mode: {new}" + (f" ({LABELS[new]})" if new in LABELS else ""))
    elif name == "model":
        state, default = load(session), data.get("default_model") or ""
        if not args:
            note(out, f"model: {state.get('model') or default} (aliases: {', '.join(f'{a}={m}' for a, m in ALIASES.items())})")
        else:
            model = ALIASES.get(args.lower(), args)
            update(session, model="" if model == default else model)
            note(out, f"model set to {model} for this session")
    elif name == "cost":
        show_cost(out, session, data.get("prices") or {})
    elif name == "clear":
        settings = data.get("user_settings") or ""
        hooks.show_notes(hooks.session_end(session, settings, "clear")["notes"])
        archive = clear(session)
        hooks.show_notes(hooks.session_start(session, settings, "clear")["notes"])
        note(out, f"conversation cleared (the old one is in {archive})")
    elif name == "resume":
        found = sessions(data.get("sessions_dir") or "", data.get("host_workdir") or "")
        found = [s for s in found if os.path.basename(s["path"]) != os.path.basename(session)]
        if not found:
            note(out, "no other sessions for this work dir")
        else:
            out.write(f"\n{listing(found)}\n")
            note(out, "to switch, quit and run bin/harness --resume (pick one) or bin/harness --continue (the latest)")
    else:
        note(out, f"unknown command /{name}")
    out.flush()
    return {"ok": True}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "init" and len(argv) == 6:
        init(*argv[2:])
    elif cmd == "status" and len(argv) == 4:
        json.dump(status(argv[2], argv[3]), sys.stdout)
    elif cmd == "exit-plan" and len(argv) == 4:
        json.dump(exit_plan(argv[2], argv[3], json.load(sys.stdin)), sys.stdout)
    elif cmd == "command" and len(argv) == 3:
        json.dump(command(argv[2], json.load(sys.stdin)), sys.stdout)
    elif cmd == "list" and len(argv) in (4, 5):
        found = sessions(argv[2], argv[3])
        if len(argv) == 5:   # latest: its file name, or nothing
            sys.stdout.write(found[0]["name"] if found else "")
        else:
            sys.stdout.write(json.dumps([{"name": s["name"], "line": line}
                                         for s, line in zip(found, listing(found).split("\n"))]) if found else "[]")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
