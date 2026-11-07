"""Skills, modelled on Claude Code's: a skill is a directory holding SKILL.md, whose frontmatter names
and describes it and whose body is the instructions. The model sees a listing of the skills and loads
one with the skill tool; the user loads one by typing /<name> [args].

    python3 skills.py context <out> <dir>...       -> writes the listing reminder to out (empty: no skills)
    python3 skills.py load <dir>... < tool_use block   -> the tool_result user message, as JSON
    python3 skills.py user <dir>... < {"value": text}   -> {message, skill, error}

Each <dir> holds one subdirectory per skill (<dir>/<name>/SKILL.md). The dirs are searched in order and
the first skill of a name wins. The harness passes its own skills/ folder, then the personal dir, then
the work dir's .claude/skills, so each shadows the ones after it.

`context` runs once per session, like the memory index: the listing is a snapshot, so the first message
(and the prompt cache) doesn't change when a skill is added mid-session. A skill with
`disable-model-invocation: true` is left out of it, and the tool refuses it; one with
`user-invocable: false` can't be loaded with /<name>.

Loading a skill gives what Claude Code gives: the tool_result says "Launching skill: <name>", and a text
block after it holds "Base directory for this skill: <dir>", then the body. $ARGUMENTS in the body is
replaced with the arguments; without it, they are appended as "ARGUMENTS: <args>". For /<name>, the
user message is the <command-message>/<command-name>/<command-args> tags, then the same text block.
"""
import json
import os
import re
import sys

SKILL_FILE = "SKILL.md"
MAX_DESCRIPTION = 1024
SLASH = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_:.-]*)(?:\s+(.*))?$", re.S)


def frontmatter(text):
    """The `key: value` pairs between the leading --- lines, and the body after them. Enough YAML for
    SKILL.md: plain, quoted and folded (> or |) scalars; nested keys and lists are skipped."""
    if not text.startswith("---"):
        return {}, text
    end = re.search(r"^---\s*$", text[3:], re.M)
    if not end:
        return {}, text
    head, body = text[3:3 + end.start()], text[3 + end.end():]
    meta, key, block = {}, None, []
    for line in head.splitlines():
        if key and (line.startswith((" ", "\t")) or not line.strip()):
            block.append(line.strip())
            continue
        if key:
            meta[key], key, block = " ".join(b for b in block if b), None, []
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            continue
        name, value = m.group(1), m.group(2).strip()
        if value in (">", "|", ">-", "|-", ">+", "|+"):
            key = name
        elif value:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            meta[name] = value
    if key:
        meta[key] = " ".join(b for b in block if b)
    return meta, body.lstrip("\n")


def truthy(value):
    return str(value).strip().lower() in ("true", "yes", "on", "1")


def discover(dirs):
    """{name: skill}, first dir wins. A skill is {name, description, dir, body, model, user}."""
    found = {}
    for root in dirs:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(root, entry, SKILL_FILE)
            try:
                with open(path, encoding="utf-8") as f:
                    meta, body = frontmatter(f.read())
            except (OSError, UnicodeDecodeError):
                continue
            name = meta.get("name") or entry
            if name in found:
                continue
            found[name] = {
                "name": name,
                "description": (meta.get("description") or "").strip()[:MAX_DESCRIPTION],
                "dir": os.path.abspath(os.path.join(root, entry)),
                "body": body,
                "model": not truthy(meta.get("disable-model-invocation", "false")),
                "user": truthy(meta.get("user-invocable", "true")),
            }
    return found


def listing(skills):
    shown = [s for s in skills.values() if s["model"]]
    if not shown:
        return ""
    lines = [f"- {s['name']}: {s['description']}" if s["description"] else f"- {s['name']}" for s in shown]
    return "The following skills are available for use with the skill tool:\n\n" + "\n".join(lines)


def content(skill, args):
    body = skill["body"]
    if "$ARGUMENTS" in body:
        body = body.replace("$ARGUMENTS", args)
    elif args:
        body = body.rstrip("\n") + f"\n\nARGUMENTS: {args}"
    return f"Base directory for this skill: {skill['dir']}\n\n{body}"


def load(dirs, block):
    """The user message answering a skill tool_use block: the tool_result, then the skill's text."""
    tool_input = block.get("input") or {}
    name = str(tool_input.get("skill") or "").strip().lstrip("/")
    args = str(tool_input.get("args") or "").strip()
    skill = discover(dirs).get(name)
    result = {"type": "tool_result", "tool_use_id": block.get("id")}
    if not name:
        return {"role": "user", "content": [{**result, "is_error": True, "content": "skill is required"}]}
    if not skill:
        return {"role": "user", "content": [{**result, "is_error": True, "content": f"Unknown skill: {name}"}]}
    if not skill["model"]:
        return {"role": "user", "content": [{**result, "is_error": True,
                                             "content": f"Skill {name} cannot be used with the skill tool: it has disable-model-invocation set"}]}
    return {"role": "user", "content": [{**result, "content": f"Launching skill: {name}"},
                                        {"type": "text", "text": content(skill, args)}]}


def user(dirs, text):
    """The user message for typed input. Plain text stays as it is; /<name> [args] loads a skill.
    A /<word> that names no skill is an error, as in Claude Code (a path like /usr/bin/x isn't a word)."""
    m = SLASH.match(text.strip())
    if not m:
        return {"message": {"role": "user", "content": text}, "skill": None, "error": None}
    name, args = m.group(1), (m.group(2) or "").strip()
    skill = discover(dirs).get(name)
    if not skill:
        return {"message": None, "skill": None, "error": f"Unknown skill: {name}"}
    if not skill["user"]:
        return {"message": None, "skill": None, "error": f"Skill {name} can only be used by the model (user-invocable: false)"}
    tags = (f"<command-message>{name} is running…</command-message>\n"
            f"<command-name>/{name}</command-name>\n"
            f"<command-args>{args}</command-args>")
    return {"message": {"role": "user", "content": [{"type": "text", "text": tags},
                                                    {"type": "text", "text": content(skill, args)}]},
            "skill": name, "error": None}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "context" and len(argv) >= 3:
        with open(argv[2], "w", encoding="utf-8") as f:
            f.write(listing(discover(argv[3:])))
    elif cmd == "load":
        json.dump(load(argv[2:], json.load(sys.stdin)), sys.stdout)
    elif cmd == "user":
        json.dump(user(argv[2:], json.load(sys.stdin).get("value") or ""), sys.stdout)
    else:
        sys.exit("usage: skills.py context <out> <dir>... | load <dir>... < block | user <dir>... < {value}")


if __name__ == "__main__":
    main(sys.argv)
