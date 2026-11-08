"""Unit tests for the scripts the workflow runs: no container, no model.

    python3 tests/test_scripts.py            # all tests
    python3 tests/test_scripts.py hooks      # tests whose name contains "hooks"
"""
import json
import os
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "scripts"))

import compact  # noqa: E402
import hooks  # noqa: E402
import instructions  # noqa: E402
import memory  # noqa: E402
import settings  # noqa: E402


class workdir:
    """A temporary work dir, made the cwd for the test."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.getcwd()
        os.chdir(self.tmp.name)
        return os.getcwd()

    def __exit__(self, *exc):
        os.chdir(self.old)
        self.tmp.cleanup()


def write(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_settings(data, name=".claude/settings.json"):
    write(name, json.dumps(data))


def loaded():
    return settings.load(settings.files(""))[0]


def tool_use(name, **args):
    return {"type": "tool_use", "id": f"toolu_{name}_{time.monotonic_ns()}", "name": name, "input": args}


def gate(*blocks, approve="ask", memory_dir="/nonexistent"):
    reply = {"content": list(blocks), "invalid": []}
    return hooks.pre("session.jsonl", "", {"reply": reply, "approve": approve, "memory_dir": memory_dir,
                                            "mutating_tools": ["bash", "write_file", "edit_file"]})


# ---- permission rules ----

def test_bash_rules_match_exact_prefix_and_wildcard():
    m = settings.command_matches
    assert m("npm test", "npm test") and not m("npm test", "npm test --watch")
    assert m("git diff:*", "git diff") and m("git diff:*", "git diff HEAD") and not m("git diff:*", "git diffx")
    assert m("git diff *", "git diff") and m("git diff *", "git diff --stat") and not m("git diff *", "git log")
    assert m("echo allowed-*", "echo allowed-1") and not m("echo allowed-*", "echo nope")


def test_allow_rules_never_match_compound_commands():
    with workdir() as cwd:
        write_settings({"permissions": {"allow": ["Bash(git diff *)", "Bash(ls; pwd)"]}})
        decide = lambda c: settings.decide(loaded(), "bash", {"command": c}, cwd)[0]
        assert decide("git diff --stat") == "allow"
        assert decide("git diff; rm -rf x") is None
        assert decide("git diff $(rm -rf x)") is None
        assert decide("git diff | sh") is None
        assert decide("ls; pwd") == "allow"            # a rule written for exactly that command


def test_deny_rules_match_any_part_and_win():
    with workdir() as cwd:
        write_settings({"permissions": {"allow": ["Bash"], "deny": ["Bash(rm *)"]}})
        decide = lambda c: settings.decide(loaded(), "bash", {"command": c}, cwd)
        assert decide("echo hi")[0] == "allow"
        assert decide("echo hi && rm -rf x") == ("deny", "Bash(rm *)")


def test_path_rules():
    with workdir() as cwd:
        write_settings({"permissions": {"allow": ["Edit(src/**)"], "deny": ["Write(//etc/**)"], "ask": ["Edit(src/secret.py)"]}})
        decide = lambda tool, path: settings.decide(loaded(), tool, {"path": path}, cwd)[0]
        assert decide("edit_file", "src/a/b.py") == "allow"
        assert decide("write_file", "./src/x.txt") == "allow"     # Edit rules cover write_file
        assert decide("edit_file", "lib/x.py") is None
        assert decide("write_file", "/etc/passwd") == "deny"
        assert decide("edit_file", "src/secret.py") == "ask"
        assert decide("read_file", "src/a.py") is None             # Edit rules don't cover reads


def test_suggest_and_add_save_an_allow_rule_locally():
    with workdir() as cwd:
        assert settings.suggest("bash", {"command": " make test "}, cwd) == "Bash(make test)"
        assert settings.suggest("edit_file", {"path": "src/a.py"}, cwd) == "Edit(src/a.py)"
        settings.add("Bash(make test)", cwd)
        settings.add("Bash(make test)", cwd)                        # no duplicate
        with open(".claude/settings.local.json", encoding="utf-8") as f:
            assert json.load(f) == {"permissions": {"allow": ["Bash(make test)"]}}
        assert settings.decide(loaded(), "bash", {"command": "make test"}, cwd)[0] == "allow"


def test_bad_settings_file_is_reported_not_fatal():
    with workdir():
        write(".claude/settings.json", "{not json")
        found, problems = settings.load(settings.files(""))
        assert found == [] and problems and "ignoring" in problems[0], problems


# ---- the gate (hooks.py pre) ----

def test_gate_defaults():
    with workdir():
        out = gate(tool_use("bash", command="ls"), tool_use("read_file", path="x"))
        assert [c["decision"] for c in out["calls"]] == ["ask", "allow"], out
        assert out["calls"][0]["suggest"] == "Bash(ls)"
        assert gate(tool_use("bash", command="ls"), approve="auto")["calls"][0]["decision"] == "allow"


def test_gate_memory_files_need_no_approval():
    with workdir() as cwd:
        os.makedirs("mem")
        out = gate(tool_use("write_file", path="mem/a.md", content="x"), tool_use("write_file", path="a.md", content="x"),
                   memory_dir=os.path.join(cwd, "mem"))
        assert [c["decision"] for c in out["calls"]] == ["allow", "ask"], out


def test_gate_rules_and_ask_beats_auto():
    with workdir():
        write_settings({"permissions": {"allow": ["Bash(echo ok)"], "deny": ["Bash(touch *)"], "ask": ["Bash(git push *)"]}})
        out = gate(tool_use("bash", command="echo ok"), tool_use("bash", command="touch x"),
                   tool_use("bash", command="git push origin"), approve="auto")
        assert [c["decision"] for c in out["calls"]] == ["allow", "deny", "ask"], out
        assert out["calls"][0]["reason"] == "allowed by Bash(echo ok)"
        assert "denied by the rule Bash(touch *)" in out["calls"][1]["reason"]


HOOK = """import json, sys
data = json.load(sys.stdin)
open("hook-input.json", "a").write(json.dumps(data) + "\\n")
{body}
"""


def hook(body, name="hook.py"):
    write(name, HOOK.format(body=body))
    return f"python3 {name}"


def test_pre_hook_exit_2_denies_and_sees_claude_code_names():
    with workdir():
        cmd = hook("""if "forbidden" in data["tool_input"].get("command", ""):
    print("no forbidden things", file=sys.stderr); sys.exit(2)""")
        write_settings({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]}]}})
        out = gate(tool_use("bash", command="echo forbidden"), tool_use("bash", command="echo fine"), tool_use("read_file", path="a"))
        assert [c["decision"] for c in out["calls"]] == ["deny", "ask", "allow"], out
        assert "no forbidden things" in out["calls"][0]["reason"]
        with open("hook-input.json", encoding="utf-8") as f:
            seen = [json.loads(line) for line in f]
        assert len(seen) == 2 and seen[0]["tool_name"] == "Bash" and seen[0]["hook_event_name"] == "PreToolUse", seen


def test_pre_hook_json_allow_and_updated_input():
    with workdir():
        cmd = hook("""print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
    "updatedInput": {"file_path": "other.txt"}}}))""")
        write_settings({"hooks": {"PreToolUse": [{"matcher": "Write|Edit", "hooks": [{"type": "command", "command": cmd}]}]}})
        out = gate(tool_use("write_file", path="a.txt", content="hi"))
        call = out["calls"][0]
        assert call["decision"] == "allow" and call["block"]["input"] == {"path": "other.txt", "content": "hi"}, call
        with open("hook-input.json", encoding="utf-8") as f:
            assert json.loads(f.readline())["tool_input"]["file_path"] == "a.txt"


def test_pre_hook_failure_is_a_note_not_a_block():
    with workdir():
        write_settings({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "exit 3"}]}]}})
        out = gate(tool_use("bash", command="ls"))
        assert out["calls"][0]["decision"] == "ask" and "failed (exit 3)" in out["notes"][0], out


def test_post_hook_feedback_reaches_the_model():
    with workdir():
        cmd = hook("""print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
    "additionalContext": "saw " + data["tool_response"]["content"]}}))""")
        write_settings({"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]}]}})
        block = tool_use("bash", command="echo hi")
        out = gate(block)
        assert out["post"]
        write("session.jsonl", json.dumps({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": block["id"], "content": "hi\n[exit code 0]"}]}) + "\n")
        posted = hooks.post("session.jsonl", "", {"calls": out["calls"]})
        text = posted["message"]["content"][0]["text"]
        assert "PostToolUse:Bash hook additional context: saw hi" in text and text.startswith("<system-reminder>"), posted


def test_prompt_hook_adds_context_or_blocks():
    with workdir():
        cmd = hook("""if "secret" in data["prompt"]:
    print("don't paste secrets", file=sys.stderr); sys.exit(2)
print("the magic word is xyzzy")""")
        write_settings({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd}]}]}})
        out = hooks.prompt("s.jsonl", "", {"message": {"role": "user", "content": "hello"}, "prompt": "hello"})
        assert not out["blocked"] and out["message"]["content"][0] == {"type": "text", "text": "hello"}, out
        assert "the magic word is xyzzy" in out["message"]["content"][1]["text"]
        out = hooks.prompt("s.jsonl", "", {"message": {"role": "user", "content": "my secret"}, "prompt": "my secret"})
        assert out["blocked"] and out["reason"] == "don't paste secrets" and out["message"] is None, out


def test_stop_hook_blocks_once():
    with workdir():
        cmd = hook("""if not data["stop_hook_active"]:
    print("run the tests first", file=sys.stderr); sys.exit(2)""")
        write_settings({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}})
        lines = [{"role": "user", "content": "do it"}, {"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
        write("s.jsonl", "".join(json.dumps(m) + "\n" for m in lines))
        out = hooks.stop("s.jsonl", "")
        assert out["block"] and out["message"]["content"][0]["text"] == "Stop hook feedback:\nrun the tests first", out
        lines += [out["message"], {"role": "assistant", "content": [{"type": "text", "text": "tests pass"}]}]
        write("s.jsonl", "".join(json.dumps(m) + "\n" for m in lines))
        assert not hooks.stop("s.jsonl", "")["block"]
        with open("hook-input.json", encoding="utf-8") as f:
            seen = [json.loads(line) for line in f]
        assert [s["stop_hook_active"] for s in seen] == [False, True] and seen[1]["last_assistant_message"] == "tests pass"
        # neither the feedback nor a prompt with hook context confuses the extraction pass's turn finding
        assert memory.user_text(out["message"]) is None and memory.user_text(lines[0]) == "do it"


# ---- CLAUDE.md ----

def test_instructions_context_order_and_agents_fallback():
    with workdir() as cwd:
        assert instructions.context("") == ""
        write("user.md", "user rule")
        write("AGENTS.md", "agents rule")
        write("CLAUDE.local.md", "local rule")
        text = instructions.context(os.path.join(cwd, "user.md"))
        assert text.index("user rule") < text.index("agents rule") < text.index("local rule"), text
        assert "(user's private global instructions for all projects)" in text and text.startswith("As you answer")
        write("CLAUDE.md", "claude rule")                              # CLAUDE.md replaces AGENTS.md
        text = instructions.context("")
        assert "claude rule" in text and "agents rule" not in text, text


def test_nested_instructions_sent_once():
    with workdir():
        write("CLAUDE.md", "root rule")
        write("src/CLAUDE.md", "src rule")
        write("src/billing/CLAUDE.md", "billing rule")
        write("src/billing/x.py", "")
        write("other/y.py", "")
        assert instructions.nested("s.jsonl", "x.py") is None              # the work dir's own: loaded at start
        msg = instructions.nested("s.jsonl", "src/billing/x.py")
        text = msg["content"][0]["text"]
        assert "src rule" in text and "billing rule" in text and "root rule" not in text, text
        assert text.index("src rule") < text.index("billing rule")
        assert instructions.nested("s.jsonl", "src/billing/x.py") is None  # once per session
        assert instructions.nested("s.jsonl", "other/y.py") is None
        assert instructions.nested("s.jsonl", "/etc/passwd") is None


# ---- compaction ----

def test_compaction_check_request_and_apply():
    with workdir():
        lines = [{"role": "user", "content": "remember 42"},
                 {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "next"}]}]   # mid-turn
        write("s.jsonl", "".join(json.dumps(m) + "\n" for m in lines))
        assert not compact.check("s.jsonl", 100)
        write("s.jsonl.usage", "150")
        assert compact.check("s.jsonl", 100) and not compact.check("s.jsonl", 0)
        req = compact.request("s.jsonl", [], "keep numbers")
        assert req["ok"] and req["messages"][-1]["content"][0]["type"] == "tool_result"
        assert "keep numbers" in req["messages"][-1]["content"][-1]["text"]
        write("s.jsonl.nested", "/work/src/CLAUDE.md\n")
        reply = {"content": [{"type": "text", "text": "<analysis>x</analysis>\n<summary>The number is 42.</summary>"}],
                 "stop_reason": "end_turn"}
        out = compact.apply("s.jsonl", False, reply)
        assert out["ok"] and out["archive"] == "s.jsonl.compacted-1.jsonl", out
        with open("s.jsonl", encoding="utf-8") as f:
            now = [json.loads(line) for line in f]
        text = now[0]["content"][0]["text"]
        assert len(now) == 1 and "The number is 42." in text and "<analysis>" not in text and "Continue the conversation" in text
        with open(out["archive"], encoding="utf-8") as f:
            assert len(f.readlines()) == 3
        assert not compact.check("s.jsonl", 100)
        assert os.path.getsize("s.jsonl.nested") == 0
        assert compact.apply("s.jsonl", True, reply)["archive"] == "s.jsonl.compacted-2.jsonl"
        assert not compact.apply("s.jsonl", True, {"content": [], "stop_reason": "max_tokens"})["ok"]


def test_compaction_keeps_the_message_just_typed():
    with workdir():
        lines = [{"role": "user", "content": "the code is 7"}, {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                 {"role": "user", "content": "what is the code?"}]
        write("s.jsonl", "".join(json.dumps(m) + "\n" for m in lines))
        req = compact.request("s.jsonl", [], "")
        assert "what is the code?" not in json.dumps(req["messages"]), req     # not summarized
        reply = {"content": [{"type": "text", "text": "<summary>The code is 7.</summary>"}], "stop_reason": "end_turn"}
        assert compact.apply("s.jsonl", False, reply)["ok"]
        with open("s.jsonl", encoding="utf-8") as f:
            now = [json.loads(line) for line in f]
        assert len(now) == 2 and now[1] == lines[2], now
        assert "Continue the conversation" not in now[0]["content"][0]["text"]   # the user's question follows


def test_compaction_needs_a_conversation():
    with workdir():
        write("s.jsonl", json.dumps({"role": "user", "content": "hi"}) + "\n")
        assert compact.request("s.jsonl", [], "") == {"ok": False, "error": "nothing to compact yet", "messages": []}


def main(argv):
    pattern = argv[1] if len(argv) > 1 else ""
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and pattern in n]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:  # noqa: BLE001 - report every failure and keep going
            failed += 1
            print(f"FAIL {name}\n{traceback.format_exc()}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
