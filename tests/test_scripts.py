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

import attachments  # noqa: E402
import compact  # noqa: E402
import history  # noqa: E402
import hooks  # noqa: E402
import input as user_input  # noqa: E402
import instructions  # noqa: E402
import memory  # noqa: E402
import session  # noqa: E402
import settings  # noqa: E402
import todos  # noqa: E402
import window  # noqa: E402


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


def test_subagent_stop_hook_matches_the_agent_type():
    with workdir():
        cmd = hook("""if not data["stop_hook_active"]:
    print("cite the files", file=sys.stderr); sys.exit(2)""")
        write_settings({"hooks": {"SubagentStop": [{"matcher": "Explore", "hooks": [{"type": "command", "command": cmd}]}]}})
        sub = "s.jsonl.agent-toolu_1.jsonl"
        write(sub, json.dumps({"role": "user", "content": "find it"}) + "\n"
              + json.dumps({"role": "assistant", "content": [{"type": "text", "text": "found"}]}) + "\n")
        assert not hooks.stop(sub, "", "general-purpose")["block"]          # another type: no hook
        out = hooks.stop(sub, "", "Explore")
        assert out["block"] and out["message"]["content"][0]["text"] == "SubagentStop hook feedback:\ncite the files", out
        assert "SubagentStop hook sent the Explore agent back" in out["notes"][0]
        with open("hook-input.json", encoding="utf-8") as f:
            seen = json.loads(f.readlines()[-1])
        assert seen["agent_type"] == "Explore" and seen["agent_id"] == "toolu_1" and seen["transcript_path"] == "s.jsonl", seen
        assert seen["agent_transcript_path"] == sub and seen["last_assistant_message"] == "found"


def test_session_start_context_and_session_end():
    with workdir():
        cmd = hook("""if data["hook_event_name"] == "SessionStart":
    print("branch is main" if data["source"] == "startup" else "resumed")""")
        each = [{"hooks": [{"type": "command", "command": cmd}]}]
        write_settings({"hooks": {"SessionStart": [{"matcher": "startup|resume", "hooks": each[0]["hooks"]}], "SessionEnd": each}})
        hooks.session_start("s.jsonl", "", "startup")
        with open("s.jsonl.session-start", encoding="utf-8") as f:
            assert f.read() == "SessionStart:startup hook additional context: branch is main"
        hooks.session_start("s.jsonl", "", "compact")                     # the matcher leaves it out
        assert os.path.getsize("s.jsonl.session-start") == 0
        hooks.session_end("s.jsonl", "", "prompt_input_exit")
        with open("hook-input.json", encoding="utf-8") as f:
            seen = json.loads(f.readlines()[-1])
        assert seen["hook_event_name"] == "SessionEnd" and seen["reason"] == "prompt_input_exit", seen


def test_pre_compact_hook_adds_instructions_or_blocks():
    with workdir():
        cmd = hook("""if data["trigger"] == "auto":
    print("not now", file=sys.stderr); sys.exit(2)
print("keep every number")""")
        write_settings({"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": cmd}]}]}})
        out = hooks.pre_compact("s.jsonl", "", "manual", {"instructions": "focus on tests"})
        assert not out["blocked"] and out["instructions"] == "focus on tests\n\nkeep every number", out
        out = hooks.pre_compact("s.jsonl", "", "auto", {"instructions": ""})
        assert out["blocked"] and "PreCompact hook blocked the compaction: not now" in out["notes"], out


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


def test_compaction_keeps_the_latest_messages_verbatim():
    with workdir():
        big = "x" * 4000                                                     # about 1000 tokens a message
        lines = [{"role": "user", "content": "start " + big},
                 {"role": "assistant", "content": [{"type": "text", "text": "a1 " + big}]},
                 {"role": "user", "content": "second " + big},
                 {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": big}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "out " + big}]},
                 {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "bash", "input": {"command": "ls"}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "done"}]}]
        cut = lambda keep: len(lines) - len(compact.split(lines, keep)[1])
        assert cut(1500) == 5                              # starts at the assistant whose tool_result follows
        assert cut(2500) == 3                              # never at a tool_result (4)
        assert cut(3500) == 2                              # or at a message the user typed
        head, tail = compact.split(lines, 100000)
        assert head == lines[:2] and tail == lines[2:]     # one assistant message is left to summarize
        assert compact.split(lines, 0) == (lines, [])
        write("s.jsonl", "".join(json.dumps(m) + "\n" for m in lines))
        req = compact.request("s.jsonl", [], "", 1500)
        assert "t2" not in json.dumps(req["messages"]) and req["messages"][-1]["content"][0]["type"] == "tool_result"
        reply = {"content": [{"type": "text", "text": "<summary>Working on it.</summary>"}], "stop_reason": "end_turn"}
        out = compact.apply("s.jsonl", False, reply, 1500)
        assert out["ok"] and out["kept"] == 2, out
        now = history.read_lines("s.jsonl")
        assert now[1:] == lines[5:] and "The most recent messages follow verbatim." in now[0]["content"][0]["text"]
        assert [m["role"] for m in history.merge(now)] == ["user", "assistant", "user"]


def test_compaction_needs_a_conversation():
    with workdir():
        write("s.jsonl", json.dumps({"role": "user", "content": "hi"}) + "\n")
        assert compact.request("s.jsonl", [], "") == {"ok": False, "error": "nothing to compact yet", "messages": []}


# ---- /context ----

def test_context_command_and_breakdown():
    assert user_input.expand("/context", [], [])["context"] is True
    with workdir():
        lines = [{"role": "user", "content": "hello " * 50}, {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
        write("s.jsonl", "".join(json.dumps(m) + "\n" for m in lines))
        write("s.jsonl.instructions", "rule " * 50)
        data = {"model": "m", "system": [{"type": "text", "text": "sys"}], "tools": [{"name": "t"}], "thinking": {"type": "adaptive", "display": "x"}}
        b = window.bodies("s.jsonl", ["s.jsonl.instructions"], data)
        assert b["full"]["thinking"] == {"type": "adaptive"} and "tools" not in b["system"] and len(b["full"]["messages"]) == 2
        ok = lambda n: {"status_code": 200, "body": {"input_tokens": n}}
        rows, exact, n = window.parts("s.jsonl", ["s.jsonl.instructions"], {"counts": [ok(8), ok(108), ok(408), ok(708)]})
        rows = dict(rows)
        assert exact and n == 2 and rows["System prompt"] == 100 and rows["Tools"] == 300, rows
        assert rows["CLAUDE.md"] + rows["Messages"] == 308 and rows["CLAUDE.md"] > 0, rows
        rows, exact, _ = window.parts("s.jsonl", [], {"counts": [ok(8), {"status_code": 404, "body": {}}], **data})
        assert not exact and dict(rows)["Messages"] > 0


# ---- modes, todos, attachments, session commands ----

def lines_of(path):
    return history.read_lines(path)


def test_permission_modes_in_the_gate():
    with workdir():
        session.init("s.jsonl", "ask", "plan", "/host")
        g = lambda *b: hooks.pre("s.jsonl", "", {"reply": {"content": list(b), "invalid": []}, "approve": "ask",
                                                 "memory_dir": "/x", "mutating_tools": ["bash", "write_file", "edit_file"]})
        out = g(tool_use("write_file", path="a.txt", content="x"), tool_use("bash", command="ls"), tool_use("read_file", path="a"))
        assert [c["decision"] for c in out["calls"]] == ["deny", "ask", "allow"] and "exit_plan_mode" in out["calls"][0]["reason"]
        session.set_mode("s.jsonl", "acceptEdits")
        out = g(tool_use("edit_file", path="src/a.py"), tool_use("write_file", path="/etc/x"), tool_use("bash", command="ls"))
        assert [c["decision"] for c in out["calls"]] == ["allow", "ask", "ask"], out
        assert out["calls"][0]["reason"] == "allowed in accept-edits mode"
        session.set_mode("s.jsonl", "bypassPermissions")
        assert [c["decision"] for c in g(tool_use("bash", command="ls"))["calls"]] == ["allow"]
        # a subagent's session follows the main session's mode
        assert session.mode(hooks.main_session("s.jsonl.agent-t1.jsonl")) == "bypassPermissions"
        assert session.status("s.jsonl", "m")["label"].startswith("⏵⏵ bypass")


def test_todo_write_and_stale_reminder():
    with workdir():
        block = {"id": "t1", "name": "todo_write", "input": {"todos": [
            {"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"},
            {"content": "Fix bug", "status": "pending", "activeForm": "Fixing bug"}]}}
        msg, error = todos.write("s.jsonl", block)
        assert not error and msg["content"][0]["content"].startswith("Todos have been modified successfully")
        assert [t["status"] for t in todos.load("s.jsonl")] == ["in_progress", "pending"]
        _, error = todos.write("s.jsonl", {"id": "t2", "input": {"todos": [{"content": "x", "status": "done"}]}})
        assert "status must be one of" in error and len(todos.load("s.jsonl")) == 2      # unchanged
        say = {"role": "assistant", "content": [{"type": "text", "text": "working"}]}
        user = {"role": "user", "content": "go on"}
        lines = [user] + [say, user] * 9
        write("s.jsonl", "".join(json.dumps(m) + "\n" for m in lines))
        assert attachments.before("s.jsonl")["added"] == []                              # 9 replies: not yet
        with open("s.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(say) + "\n" + json.dumps(user) + "\n")
        assert attachments.before("s.jsonl")["added"] == ["todos"]
        text = lines_of("s.jsonl")[-1]["content"][0]["text"]
        assert "hasn't been used recently" in text and "Running tests" in text, text
        assert attachments.before("s.jsonl")["added"] == []                              # not again so soon


def test_plan_mode_reminders_and_exit_plan():
    with workdir():
        session.init("s.jsonl", "ask", "default", "/host")
        session.set_mode("s.jsonl", "plan")
        write("s.jsonl", json.dumps({"role": "user", "content": "add a flag"}) + "\n")
        assert attachments.before("s.jsonl")["added"] == ["plan"]
        assert attachments.before("s.jsonl")["added"] == []                    # once per turn: not mid-turn
        block = {"id": "t1", "name": "exit_plan_mode", "input": {"plan": "1. edit x"}}
        rejected = session.exit_plan("s.jsonl", "n", block)["content"][0]
        assert rejected["is_error"] and "still in plan mode" in rejected["content"] and session.mode("s.jsonl") == "plan"
        approved = session.exit_plan("s.jsonl", "a", block)["content"][0]
        assert "approved your plan" in approved["content"] and "1. edit x" in approved["content"]
        assert session.mode("s.jsonl") == "acceptEdits" and not session.load("s.jsonl")["plan_exited"]
        assert session.exit_plan("s.jsonl", "", block)["content"][0]["content"] == session.NOT_PLANNING
        session.set_mode("s.jsonl", "plan")
        session.set_mode("s.jsonl", "default")                                 # /mode out of plan mode
        with open("s.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "content": "go"}) + "\n")
        assert attachments.before("s.jsonl")["added"] == ["plan_exited"]
        assert attachments.before("s.jsonl")["added"] == []


def test_changed_files_are_reported_once():
    with workdir():
        write("a.txt", "one\ntwo\nthree\n")
        write("s.jsonl", json.dumps({"role": "user", "content": "hi"}) + "\n")
        attachments.track("s.jsonl", "a.txt")
        assert attachments.before("s.jsonl")["added"] == []
        time.sleep(0.01)
        write("a.txt", "one\nTWO\nthree\nfour\n")
        assert attachments.before("s.jsonl")["added"] == ["changed_file"]
        text = lines_of("s.jsonl")[-1]["content"][0]["text"]
        assert "a.txt was modified" in text and "     2\tTWO" in text and "     4\tfour" in text, text
        assert attachments.before("s.jsonl")["added"] == []
        os.utime("a.txt")                                                       # touched, not changed
        assert attachments.before("s.jsonl")["added"] == []


def test_at_mentions_attach_files_and_directories():
    with workdir() as cwd:
        write("src/app.py", "print('hi')\n")
        out = user_input.expand("look at @src/app.py, and @src please; not @nope or @agent-x", [], [], "s.jsonl", 1000)
        assert out["files"] == [os.path.join(cwd, "src/app.py"), os.path.join(cwd, "src")], out["files"]
        texts = [b["text"] for b in out["message"]["content"]]
        assert texts[0].startswith("look at") and "Called the Read tool" in texts[1] and "     1\tprint('hi')" in texts[2]
        assert "Called the Bash tool" in texts[3] and "app.py" in texts[4]
        assert os.path.join(cwd, "src/app.py") in attachments.load_reads("s.jsonl")   # tracked for changes
        assert user_input.expand("email me @ noon", [], [], "s.jsonl")["message"]["content"] == "email me @ noon"


def test_session_commands():
    with workdir():
        assert user_input.expand("/model sonnet", [], [])["session_command"] == {"name": "model", "args": "sonnet"}
        assert user_input.expand("/clear", [], [])["session_command"]["name"] == "clear"
        assert user_input.expand("/clear now", [], [])["session_command"] is None      # not a command: a skill lookup
        session.init("s.jsonl", "ask", "default", "/host")
        session.command("s.jsonl", {"name": "mode"})
        assert session.mode("s.jsonl") == "acceptEdits"
        session.command("s.jsonl", {"name": "plan"})
        assert session.mode("s.jsonl") == "plan"
        session.command("s.jsonl", {"name": "plan"})
        assert session.mode("s.jsonl") == "default"
        session.command("s.jsonl", {"name": "model", "args": "opus", "default_model": "claude-haiku-5-5"})
        assert session.status("s.jsonl", "claude-haiku-5-5")["model"] == "claude-opus-5-5"
        lines = [{"role": "user", "content": "first prompt here"}, {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]
        write("s.jsonl", "".join(json.dumps(m) + "\n" for m in lines))
        write("s.jsonl.todos", "[]")
        session.command("s.jsonl", {"name": "clear"})
        assert lines_of("s.jsonl") == [] and len(lines_of("s.jsonl.cleared-1.jsonl")) == 2
        assert session.mode("s.jsonl") == "default"


def test_cost_prices_each_call():
    with workdir():
        prices = {"claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125,
                                       "long_above": 100000, "long": {"input": 0.50, "output": 2.50}}}
        calls = [{"model": "claude-haiku-5-5", "kind": "main", "usage": {"input_tokens": 1000000, "output_tokens": 1000000}},
                 {"model": "claude-haiku-5-5", "kind": "compact", "usage": {"input_tokens": 200000, "output_tokens": 0}},
                 {"model": "other", "kind": "subagent", "usage": {"input_tokens": 5}}]
        write("s.jsonl.costs", "".join(json.dumps(c) + "\n" for c in calls))
        c = session.cost("s.jsonl", prices)
        # the first call's prompt is over 100k: long rates (0.50 + 2.50); the second too (0.10)
        assert abs(c["usd"] - (0.5 + 2.5 + 0.1)) < 1e-9 and c["unpriced"] == ["other"] and c["calls"] == 3, c
        assert c["kinds"] == {"main": 1, "compact": 1, "subagent": 1}


def test_sessions_list_for_a_work_dir():
    with workdir():
        os.makedirs("sessions")
        for name, wd in (("a.jsonl", "/w1"), ("b.jsonl", "/w2"), ("c.jsonl", "/w1")):
            write(f"sessions/{name}", json.dumps({"role": "user", "content": f"prompt {name}"}) + "\n")
            session.init(f"sessions/{name}", "ask", "default", wd)
            time.sleep(0.01)
        write("sessions/c.jsonl.compacted-1.jsonl", "{}\n")
        found = session.sessions("sessions", "/w1")
        assert [s["name"] for s in found] == ["c.jsonl", "a.jsonl"] and found[0]["prompt"] == "prompt c.jsonl", found


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
