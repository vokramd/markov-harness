"""End-to-end tests: drive bin/harness in a pty with pexpect, talking to the real relay.

    python3 tests/test_harness.py            # all tests
    python3 tests/test_harness.py bash_tool  # tests whose name contains "bash_tool"
    VERBOSE=1 python3 tests/test_harness.py  # echo the terminal
    MODEL=claude-sonnet-5-5 python3 tests/test_harness.py  # another model (default claude-haiku-5-5)
"""
import glob
import json
import os
import re
import sys
import time
import uuid

import pexpect

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(HERE, "bin", "harness")
STATE = os.path.join(HERE, ".state", "container")   # mounted at /state in the container
WORK = os.path.join(HERE, ".state", "work")          # mounted at /work, the tools' cwd
PROMPT = ">: "
TIMEOUT = 120
MODEL = os.environ.get("MODEL", "claude-haiku-5-5")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Session:
    def __init__(self, *args, workdir=WORK, extract=False):
        # Extraction is off except in the memory tests: it adds a model call per turn, and what it saves
        # in the shared work dir's memory would leak into later tests.
        args = ["--var", f"model={MODEL}", "--var", f"extract_memories={str(extract).lower()}", *args]
        os.makedirs(workdir, exist_ok=True)
        env = {**os.environ, "HARNESS_WORKDIR": workdir}
        self.child = pexpect.spawn(HARNESS, args, cwd=HERE, env=env, encoding="utf-8", timeout=TIMEOUT)
        if os.environ.get("VERBOSE"):
            self.child.logfile_read = sys.stdout
        self.transcript = ""
        banner = self.wait(PROMPT)
        self.path = re.search(r"session=(\S+)", banner).group(1)

    def wait(self, pattern):
        """Wait for pattern; fail fast if the harness reports an API error first."""
        i = self.child.expect([pattern, r"API error[^\r\n]*"])
        seen = ANSI.sub("", self.child.before + self.child.after)
        self.transcript += seen
        if i == 1:
            raise AssertionError(f"harness reported: {ANSI.sub('', self.child.after)}")
        return seen

    def say(self, text):
        """Send one user turn; return everything printed until the next prompt."""
        self.child.sendline(text)
        return self.wait(PROMPT)

    def say_until(self, text, pattern):
        self.child.sendline(text)
        return self.wait(pattern)

    def answer(self, text, pattern=PROMPT):
        self.child.sendline(text)
        return self.wait(pattern)

    def exit(self):
        self.child.sendline("/exit")
        self.child.expect(pexpect.EOF)
        self.transcript += ANSI.sub("", self.child.before)
        self.child.close()
        assert "completed successfully" in self.transcript, self.transcript[-500:]
        assert self.child.exitstatus == 0, self.child.exitstatus

    def messages(self):
        with open(host_path(self.path), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


def tool_results(messages):
    return [b for m in messages if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_result"]


def test_two_turns_share_context():
    word = f"heliotrope-{uuid.uuid4().hex[:6]}"   # unique, so no saved memory can supply it
    s = Session()
    s.say(f"For this conversation only, the codeword is '{word}'. Reply only with: ok")
    out = s.say("What was the codeword? Reply with just the word.")
    assert word in out.lower(), out
    s.exit()
    roles = [m["role"] for m in s.messages()]
    assert roles == ["user", "assistant", "user", "assistant"], roles


def test_bash_tool_approved():
    s = Session()
    s.say_until("Use the bash tool to run: echo markov-$((6*7)) . Then tell me the output.", r"run it\? \[y/n\]")
    out = s.answer("y")
    assert "markov-42" in out, out
    s.exit()
    results = tool_results(s.messages())
    assert results and "markov-42" in results[0]["content"] and "[exit code 0]" in results[0]["content"], results


def test_bash_tool_denied():
    s = Session()
    s.say_until("Use the bash tool to run: touch should-not-exist . Do not retry if it is declined.", r"run it\? \[y/n\]")
    s.answer("n")
    s.exit()
    results = tool_results(s.messages())
    assert results and results[0].get("is_error") and "declined" in results[0]["content"], results
    assert not os.path.exists(os.path.join(WORK, "should-not-exist"))


def test_write_then_read_file():
    name = f"test-{uuid.uuid4().hex[:8]}.txt"
    s = Session("--var", "approve=auto")
    s.say(f"Use write_file to write the text 'blue-giraffe' to {name}, then use read_file to read it back, "
          f"then tell me what it contains.")
    s.exit()
    with open(os.path.join(WORK, name), encoding="utf-8") as f:
        assert "blue-giraffe" in f.read()
    contents = [r["content"] for r in tool_results(s.messages())]
    assert any("wrote" in c for c in contents) and any("blue-giraffe" in c for c in contents), contents


def test_read_of_missing_file_is_an_error_result():
    name = f"missing-{uuid.uuid4().hex[:8]}.txt"
    s = Session()
    out = s.say(f"Use read_file to read {name} (it may not exist), then reply with only: done")
    assert "done" in out.lower(), out          # the run carried on past the failed read
    s.exit()
    results = tool_results(s.messages())
    assert results and results[0].get("is_error") and "No such file" in results[0]["content"], results


def test_bash_is_containerized():
    s = Session("--var", "approve=auto")
    s.say("Use the bash tool to run exactly this command, then reply with only: done. Command: "
          f"pwd; id -u; test -e {HERE}/README.md && echo HOST-VISIBLE || echo host-hidden; "
          "touch /usr/x 2>/dev/null && echo ROOTFS-WRITABLE || echo rootfs-readonly; "
          "echo harness: $(ls -A /harness)")
    s.exit()
    out = tool_results(s.messages())[0]["content"]
    assert out.splitlines()[0] == "/work", out
    assert str(os.getuid()) in out, out          # --userns=keep-id: files in /work stay the user's
    assert "host-hidden" in out and "rootfs-readonly" in out, out
    # Only the workflow files are mounted: markov fingerprints /harness, and .state/ changes every run.
    assert "harness: meta.yaml rules.yaml scripts step_types.yaml vars vars.yaml workflows" in out, out


def test_reply_streams():
    """The reply reaches the terminal in pieces while it is being written, not all at once."""
    s = Session()
    s.child.sendline("Count from 1 to 80 in words (one, two, ...), one per line, nothing else.")
    s.child.expect(r"\r\n")                     # the echoed input line
    arrivals, text = [], ""
    while "end_turn" not in text:
        chunk = s.child.read_nonblocking(4096, timeout=TIMEOUT)
        arrivals.append((time.monotonic(), chunk))
        text += chunk
    s.wait(PROMPT)
    s.exit()
    # Counted in reads, not seconds: haiku writes the whole reply in a few tenths of a second, so a
    # time span flakes. Printed at once, the reply would come in a read or two; streamed, it comes in ~40.
    reply = [c for _, c in arrivals if re.search(r"[a-z]", ANSI.sub("", c))]
    assert "eighty" in text.lower(), text[-300:]
    assert len(reply) >= 10, f"reply arrived in {len(reply)} piece(s)"
    events = [e for e in s.messages()[-1]["content"] if e.get("type") == "text"]
    assert events and "eighty" in events[0]["text"].lower(), events  # assembled from the stream


def test_resume_session():
    s = Session()
    s.say("My favourite number is 314. Reply only with: noted")
    s.exit()
    r = Session("--var", f"session={s.path}")
    assert r.path == s.path
    out = r.say("What is my favourite number? Reply with just the number.")
    assert "314" in out, out
    r.exit()
    assert len(r.messages()) == 4


def test_edit_file_shows_diff_and_applies():
    name = f"edit-{uuid.uuid4().hex[:8]}.py"
    path = os.path.join(WORK, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("def greet():\n    return 'hello'\n\n\nprint(greet())\n")
    s = Session()
    out = s.say_until(f"Use edit_file on {name} to change the string 'hello' to 'goodbye'. Do not read it first; "
                      "the line is exactly:     return 'hello'", r"apply it\? \[y/n\]")
    assert "-    return 'hello'" in out and "+    return 'goodbye'" in out, out   # the diff, before approving
    s.answer("y")
    s.exit()
    with open(path, encoding="utf-8") as f:
        assert f.read() == "def greet():\n    return 'goodbye'\n\n\nprint(greet())\n"
    results = tool_results(s.messages())
    assert any("edited" in r["content"] and not r.get("is_error") for r in results), results


def test_glob_and_grep_run_together():
    d = f"search-{uuid.uuid4().hex[:8]}"
    os.makedirs(os.path.join(WORK, d, "sub"))
    for rel, text in [("a.txt", "nothing here\n"), ("sub/b.txt", "the needle-7731 is here\n"), ("c.md", "x\n")]:
        with open(os.path.join(WORK, d, rel), "w", encoding="utf-8") as f:
            f.write(text)
    s = Session()   # approve=ask: read-only tools must not prompt
    s.say(f"In a single reply, call glob with pattern '**/*.txt' and path '{d}', and at the same time call grep "
          f"with pattern 'needle-[0-9]+' and path '{d}'. Then list the .txt files and the needle you found.")
    s.exit()
    msgs = s.messages()
    calls = [b["name"] for m in msgs if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]
    assert {"glob", "grep"} <= set(calls), calls
    contents = "\n".join(r["content"] for r in tool_results(msgs))
    assert f"{d}/a.txt" in contents and f"{d}/sub/b.txt" in contents and "c.md" not in contents, contents
    assert f"{d}/sub/b.txt:1: the needle-7731 is here" in contents, contents
    assert "Glob(" in s.transcript and "Grep(" in s.transcript, s.transcript[-800:]


def test_bash_output_streams_live():
    s = Session("--var", "approve=auto")
    s.child.sendline("Use the bash tool to run exactly: for i in 1 2 3; do echo tick-$i; sleep 1; done . "
                     "Then reply with only: done")
    seen = {}
    while "tick-3" not in seen:
        i = s.child.expect([r"tick-(\d)(?:\x1b\[0m)?\r\n", r"API error[^\r\n]*"])
        assert i == 0, s.child.after
        seen.setdefault(f"tick-{s.child.match.group(1)}", time.monotonic())
    s.wait(r"exit 0")
    s.wait(PROMPT)
    s.exit()
    assert seen["tick-3"] - seen["tick-1"] > 1.5, seen   # printed as they ran, not all at the end


def test_tool_input_streams_as_a_count():
    name = f"long-{uuid.uuid4().hex[:8]}.txt"
    s = Session("--var", "approve=auto")
    s.say(f"Use write_file to write {name} containing the numbers 1 to 300, one per line. Then reply with only: done")
    s.exit()
    assert re.search(r"tool input: [\d,]+ chars", s.transcript), s.transcript[-800:]
    with open(os.path.join(WORK, name), encoding="utf-8") as f:
        assert f.read().split() == [str(n) for n in range(1, 301)]


def test_prompt_cache_is_read_on_the_next_call():
    s = Session()
    filler = " ".join(f"item-{n}" for n in range(3000))   # past the minimum cacheable prefix
    s.say(f"Here is a list to keep in mind: {filler}. Reply only with: ok")
    out = s.say("Reply only with: ok")
    s.exit()
    m = re.search(r"cache read (\d+)", out)
    assert m and int(m.group(1)) > 3000, out


def test_tool_rounds_are_capped():
    s = Session("--var", "approve=auto", "--var", "max_tool_rounds=1")
    out = s.say("Use the bash tool to run: echo first-call . After you see its output, use the bash tool "
                "again to run: echo second-call . Then reply with: done")
    assert "Bash(echo first-call)" in out and "Bash(echo second-call)" not in out, out   # one model call only
    assert "stopped after 1 model calls" in out, out
    out = s.say("Reply with only: still-here")                   # the session carries on
    assert "still-here" in out, out
    s.exit()


def test_memory_index_loaded_and_writes_need_no_approval():
    work = os.path.join(HERE, ".state", "work-memory")
    run_dir = f"test-memory/{uuid.uuid4().hex[:8]}"         # a fresh memory dir, so earlier runs can't interfere
    memory = os.path.join(STATE, run_dir)
    os.makedirs(memory)
    host, shell = f"tangerine-{uuid.uuid4().hex[:6]}", f"vermilion-{uuid.uuid4().hex[:6]}"
    with open(os.path.join(memory, "MEMORY.md"), "w", encoding="utf-8") as f:
        f.write(f"- [Deploy host](deploy.md) — the deploy host is {host}\n")
    s = Session("--var", f"memory_dir=/state/{run_dir}", workdir=work, extract=True)   # approve=ask: no prompts
    out = s.say("According to your memory index, what is the deploy host? Reply with just the host name.")
    assert host in out, out
    s.child.sendline(f"Remember for future sessions: my preferred shell is {shell}. Save it to memory, "
                     "then reply with only: saved")
    i = s.child.expect([PROMPT, r"(run|write|apply) it\? \[y/n\]"])
    s.transcript += ANSI.sub("", s.child.before + s.child.after)
    assert i == 0, s.transcript[-800:]
    s.exit()
    saved = {}
    for name in os.listdir(memory):
        with open(os.path.join(memory, name), encoding="utf-8") as f:
            saved[name] = f.read()
    facts = [n for n, text in saved.items() if n != "MEMORY.md" and shell in text]
    assert facts and "metadata:" in saved[facts[0]], saved
    assert any(f in saved["MEMORY.md"] for f in facts) and host in saved["MEMORY.md"], saved["MEMORY.md"]
    first = s.messages()[0]["content"]
    assert isinstance(first, str) and "system-reminder" not in first, first   # injected per request, not stored
    fork = host_path(s.path) + ".extract.jsonl"   # the main agent saved it, so no extraction pass repeated it
    assert not os.path.exists(fork) or shell not in json.dumps(fork_tool_calls(fork)), fork


def host_path(path):
    return os.path.join(STATE, os.path.relpath(path, "/state"))


def fork_tool_calls(fork):
    with open(fork, encoding="utf-8") as f:
        msgs = [json.loads(line) for line in f if line.strip()]
    return [b for m in msgs if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]


def test_memory_extracted_after_turn():
    work = os.path.join(HERE, ".state", "work-extract")
    key = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(work))
    memory = os.path.join(STATE, "projects", key, "memory")
    editor = f"kakoune-{uuid.uuid4().hex[:6]}"
    s = Session(workdir=work, extract=True)
    # A lasting preference, stated in passing; the main agent answers without tools, then the pass saves it.
    out = s.say(f"I always use the {editor} editor, on every project. Without using any tools, tell me: "
                "what is 6 times 7? Reply with just the number.")
    assert "42" in out, out
    s.exit()
    assert "memory updated" in s.transcript, s.transcript[-800:]
    calls = fork_tool_calls(host_path(s.path) + ".extract.jsonl")
    assert any(c["name"] in ("write_file", "edit_file") and editor in json.dumps(c["input"]) for c in calls), calls
    texts = []
    for name in os.listdir(memory):
        with open(os.path.join(memory, name), encoding="utf-8") as f:
            texts.append(f.read())
    assert any(editor in t and "metadata:" in t for t in texts), texts
    roles = [m["role"] for m in s.messages()]
    assert roles == ["user", "assistant"], roles   # the pass writes to its own fork file, not the session


def main(argv):
    pattern = argv[1] if len(argv) > 1 else ""
    print(f"using model {MODEL}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and pattern in n]
    failed = 0
    for name, fn in tests:
        start = time.time()
        try:
            fn()
            print(f"PASS {name} ({time.time() - start:.1f}s)")
        except Exception as e:  # noqa: BLE001 - report every failure and keep going
            failed += 1
            print(f"FAIL {name} ({time.time() - start:.1f}s): {type(e).__name__}: {str(e)[:800]}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
