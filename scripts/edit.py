"""The edit_file tool: replace an exact piece of text in a file.

    python3 edit.py diff  < tool input JSON   -> {ok, error?, diff, count}; changes nothing
    python3 edit.py apply < tool input JSON   -> {ok, error?, count}; writes the file

The input is {path, old_string, new_string, replace_all?}. old_string must occur exactly once,
unless replace_all is true; new_string must differ from it. `apply` checks again, so a file that
changed after `diff` is never edited blindly.
"""
import difflib
import json
import sys


def plan(args):
    path, old, new = args.get("path"), args.get("old_string"), args.get("new_string")
    if not isinstance(path, str) or not path:
        return None, None, "path is required"
    if not isinstance(old, str) or old == "":
        return None, None, "old_string must be a non-empty string"
    if not isinstance(new, str):
        return None, None, "new_string must be a string"
    if old == new:
        return None, None, "old_string and new_string are the same"
    try:
        with open(path, encoding="utf-8", newline="") as f:
            before = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return None, None, f"cannot read {path}: {e}"
    count = before.count(old)
    if count == 0:
        return None, None, f"old_string was not found in {path}"
    if count > 1 and not args.get("replace_all"):
        return None, None, f"old_string occurs {count} times in {path}; add context to make it unique, or set replace_all"
    return before, before.replace(old, new), count


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode not in ("diff", "apply"):
        sys.exit("usage: edit.py diff|apply < tool input JSON")
    args = json.load(sys.stdin)
    before, after, count = plan(args)
    if before is None:
        json.dump({"ok": False, "error": count}, sys.stdout)
        return
    if mode == "diff":
        diff = difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=args["path"], tofile=args["path"], n=2)
        json.dump({"ok": True, "count": count, "diff": "".join(diff)}, sys.stdout)
        return
    try:
        with open(args["path"], "w", encoding="utf-8", newline="") as f:
            f.write(after)
    except OSError as e:
        json.dump({"ok": False, "error": f"cannot write {args['path']}: {e}"}, sys.stdout)
        return
    json.dump({"ok": True, "count": count}, sys.stdout)


if __name__ == "__main__":
    main(sys.argv)
