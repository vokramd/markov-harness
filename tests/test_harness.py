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
import subprocess
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
APPROVAL = r"(run|write|apply) it\? \(a: always\) \[y/n/a\]"


class Session:
    def __init__(self, *args, workdir=WORK, extract=False, project=None, first=()):
        # Extraction is off except in the memory tests: it adds a model call per turn, and what it saves
        # in the shared work dir's memory would leak into later tests.
        args = [*first, "--var", f"model={MODEL}", "--var", f"extract_memories={str(extract).lower()}", *args]
        os.makedirs(workdir, exist_ok=True)
        env = {**os.environ, "HARNESS_WORKDIR": workdir}
        env.pop("HARNESS_PROJECT_ID", None)
        if project:
            env["HARNESS_PROJECT_ID"] = project
        self.child = pexpect.spawn(HARNESS, args, cwd=HERE, env=env, encoding="utf-8", timeout=TIMEOUT)
        if os.environ.get("VERBOSE"):
            self.child.logfile_read = sys.stdout
        self.transcript = ""
        banner = self.wait(PROMPT)
        self.path = re.search(r"session=(\S+)", banner).group(1)

    def wait(self, pattern):
        """Wait for pattern; fail fast if the harness reports an API error, or asks for an approval
        the test didn't expect (it would otherwise wait out the timeout)."""
        i = self.child.expect([pattern, r"API error[^\r\n]*", APPROVAL])
        seen = ANSI.sub("", self.child.before + self.child.after)
        self.transcript += seen
        if i == 1:
            raise AssertionError(f"harness reported: {ANSI.sub('', self.child.after)}")
        if i == 2:
            raise AssertionError(f"unexpected approval prompt: {seen[-400:]}")
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
    s.say_until("Use the bash tool to run: echo markov-$((6*7)) . Then tell me the output.", APPROVAL)
    out = s.answer("y")
    assert "markov-42" in out, out
    s.exit()
    results = tool_results(s.messages())
    assert results and "markov-42" in results[0]["content"] and "[exit code 0]" in results[0]["content"], results


def test_bash_tool_denied():
    s = Session()
    s.say_until("Use the bash tool to run: touch should-not-exist . Do not retry if it is declined.", APPROVAL)
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
    assert "harness: agents meta.yaml rules.yaml scripts skills step_types.yaml vars vars.yaml workflows" in out, out


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
                      "the line is exactly:     return 'hello'", APPROVAL)
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
    run_dir = "test-memory"   # this test's own memory dir, emptied first so earlier runs can't interfere
    memory = os.path.join(STATE, run_dir)
    os.makedirs(memory, exist_ok=True)
    for name in glob.glob(os.path.join(memory, "*.md")):
        os.remove(name)
    host, shell = f"tangerine-{uuid.uuid4().hex[:6]}", f"vermilion-{uuid.uuid4().hex[:6]}"
    with open(os.path.join(memory, "MEMORY.md"), "w", encoding="utf-8") as f:
        f.write(f"- [Deploy host](deploy.md) — the deploy host is {host}\n")
    s = Session("--var", f"memory_dir=/state/{run_dir}", workdir=work, extract=True)   # approve=ask: no prompts
    out = s.say("According to your memory index, what is the deploy host? Reply with just the host name.")
    assert host in out, out
    s.child.sendline(f"Remember for future sessions: my preferred shell is {shell}. Save it to memory, "
                     "then reply with only: saved")
    i = s.child.expect([PROMPT, APPROVAL])
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


def skill_workdir():
    """A work dir with two project skills, each holding a fresh codeword only the skill can supply."""
    work = os.path.join(HERE, ".state", "work-skills")
    words = {name: f"{name}-{uuid.uuid4().hex[:6]}" for name in ("quokka", "pelican")}
    skills = {
        "release-codeword": ("Gives this project's release codeword. Use whenever the user asks for the release codeword.",
                             f"The release codeword is {words['quokka']}. Reply with just the codeword."),
        "stamp": ("Stamps a word with the project's stamp.",
                  f"Reply with exactly one line: $ARGUMENTS-{words['pelican']}"),
    }
    for name, (description, body) in skills.items():
        os.makedirs(os.path.join(work, ".claude", "skills", name), exist_ok=True)
        with open(os.path.join(work, ".claude", "skills", name, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
    return work, words


def test_model_loads_a_skill():
    work, words = skill_workdir()
    s = Session(workdir=work)
    out = s.say("What is this project's release codeword? Reply with just the codeword.")
    assert "Skill(release-codeword)" in out and "Successfully loaded skill" in out, out
    assert words["quokka"] in out.split("Successfully loaded skill")[-1], out
    s.exit()
    uses = [b for m in s.messages() if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]
    assert [u["input"].get("skill") for u in uses if u["name"] == "skill"] == ["release-codeword"], uses
    loaded = [m["content"] for m in s.messages() if isinstance(m["content"], list) and
              any(b.get("type") == "tool_result" and b.get("content") == "Launching skill: release-codeword" for b in m["content"])]
    assert loaded and "Base directory for this skill: /work/.claude/skills/release-codeword" in loaded[0][1]["text"], loaded
    with open(host_path(s.path) + ".skills", encoding="utf-8") as f:
        listing = f.read()
    assert "- release-codeword: Gives this project's release codeword." in listing and "- stamp:" in listing, listing


def test_slash_command_runs_a_skill():
    work, words = skill_workdir()
    s = Session(workdir=work)
    out = s.say("/stamp kiwi")
    assert "loaded skill stamp" in out and f"kiwi-{words['pelican']}" in out, out
    out = s.say("/no-such-skill")
    assert "Unknown skill: no-such-skill" in out, out
    s.exit()
    msgs = s.messages()
    assert len(msgs) == 2, msgs   # the unknown one reached neither the session nor the model
    assert "<command-name>/stamp</command-name>\n<command-args>kiwi</command-args>" in msgs[0]["content"][0]["text"], msgs[0]
    assert not any(b.get("type") == "tool_use" for b in msgs[1]["content"]), msgs[1]   # already loaded: no skill call


def agent_workdir():
    """A work dir with a file to find, nested a few levels down, and a custom agent type holding a number."""
    work = os.path.join(HERE, ".state", "work-agents")
    word, number = f"marmalade-{uuid.uuid4().hex[:6]}", f"{uuid.uuid4().int % 90000 + 10000}"
    nested = os.path.join(work, "src", "billing", "legacy")
    os.makedirs(nested, exist_ok=True)
    for name in glob.glob(os.path.join(work, "src", "**", "*.txt"), recursive=True):
        os.remove(name)
    for i in range(5):
        with open(os.path.join(work, "src", f"notes-{i}.txt"), "w", encoding="utf-8") as f:
            f.write("nothing to see here\n")
    with open(os.path.join(nested, "rates.txt"), "w", encoding="utf-8") as f:
        f.write(f"the code name is {word}\n")
    os.makedirs(os.path.join(work, ".claude", "agents"), exist_ok=True)
    with open(os.path.join(work, ".claude", "agents", "oracle.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: oracle\ndescription: Knows the oracle number. Ask it for the oracle number.\ntools: Read\n---\n"
                f"You know one thing: the oracle number is {number}. When asked for it, answer with just the number.\n")
    return work, word, number


def agent_calls(messages):
    return [b for m in messages if m["role"] == "assistant" for b in m["content"]
            if b.get("type") == "tool_use" and b["name"] == "agent"]


def test_explore_agent_searches_in_its_own_context():
    work, word, _ = agent_workdir()
    s = Session(workdir=work)
    out = s.say(f"Use the Explore agent to find which file under src/ contains the text '{word}'. "
                "Don't check its answer yourself; reply with just the file's path.")
    s.exit()
    assert "Explore(" in out and "Done (" in out, out
    assert "rates.txt" in out.split("Done (")[-1], out
    calls = agent_calls(s.messages())
    assert calls and calls[0]["input"].get("subagent_type") == "Explore", calls
    sub = host_path(s.path) + f".agent-{calls[0]['id']}.jsonl"
    with open(sub, encoding="utf-8") as f:
        sub_msgs = [json.loads(line) for line in f if line.strip()]
    used = {b["name"] for m in sub_msgs if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"}
    assert used and used <= {"read_file", "glob", "grep"}, used            # Explore is read-only
    main_tools = {b["name"] for m in s.messages() if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"}
    assert main_tools == {"agent"}, main_tools                          # the search stayed in the subagent


def test_custom_agent_type_from_the_work_dir():
    work, _, number = agent_workdir()
    s = Session(workdir=work)
    out = s.say("Ask the oracle agent for the oracle number, then reply with just the number.")
    s.exit()
    assert "oracle(" in out and number in out.split("Done (")[-1], out
    assert [c["input"].get("subagent_type") for c in agent_calls(s.messages())] == ["oracle"], s.messages()
    with open(host_path(s.path) + ".agents", encoding="utf-8") as f:
        listing = f.read()
    assert "- oracle: Knows the oracle number." in listing and "(Tools: read_file)" in listing, listing
    assert "- Explore:" in listing and "- general-purpose:" in listing, listing


def test_agent_mention_asks_for_that_agent():
    work, _, number = agent_workdir()
    s = Session(workdir=work)
    out = s.say("@agent-oracle what is the oracle number? Reply with just the number.")
    s.exit()
    assert "asked for agent oracle" in out and number in out.split("Done (")[-1], out
    first = s.messages()[0]["content"]
    assert 'invoke the agent "oracle"' in first[1]["text"], first     # the reminder, after the typed text
    assert [c["input"].get("subagent_type") for c in agent_calls(s.messages())] == ["oracle"], s.messages()


def test_agent_command_runs_without_the_model():
    work, _, number = agent_workdir()
    s = Session(workdir=work)
    out = s.say("/agent oracle What is the oracle number?")
    assert "oracle(/agent oracle What is the oracle number?)" in out and number in out.split("Done (")[-1], out
    assert len(s.messages()) == 1, s.messages()                     # the command and its output; no model reply
    recorded = s.messages()[0]["content"]
    assert "<command-name>/agent</command-name>" in recorded[0]["text"], recorded
    assert recorded[1]["text"].startswith("<local-command-stdout>") and number in recorded[1]["text"], recorded
    out = s.say("/agent no-such-type do it")
    assert "Unknown agent type: no-such-type" in out, out
    out = s.say("What number did the oracle just give? Reply with just the number.")
    assert number in out, out                                        # the model sees the command's output
    s.exit()


def memory_dir(workdir, project=None):
    """Where bin/harness keeps this work dir's (or project's) memory on the host."""
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_PROJECT_ID"}
    env["HARNESS_WORKDIR"] = workdir
    if project:
        env["HARNESS_PROJECT_ID"] = project
    os.makedirs(workdir, exist_ok=True)
    return subprocess.run([HARNESS, "--print-memory-dir"], env=env, capture_output=True, text=True, check=True).stdout.strip()


def test_memory_keys_dont_collide():
    # The slug alone maps both to ...-work-key-b-c; the path hash keeps them apart.
    a, b = (os.path.join(HERE, ".state", "work-key", *p) for p in (["b-c"], ["b", "c"]))
    assert memory_dir(a) != memory_dir(b), memory_dir(a)
    assert memory_dir(a) == memory_dir(a)
    assert memory_dir(a, project="shared") == memory_dir(b, project="shared") == os.path.join(STATE, "projects", "shared", "memory")
    bad = subprocess.run([HARNESS, "--print-memory-dir"], env={**os.environ, "HARNESS_PROJECT_ID": "../up"},
                         capture_output=True, text=True)
    assert bad.returncode == 2 and "HARNESS_PROJECT_ID" in bad.stderr, bad


def test_project_id_shares_memory_across_work_dirs():
    project = "test-project-id"
    memory = memory_dir(WORK, project=project)
    os.makedirs(memory, exist_ok=True)
    for name in glob.glob(os.path.join(memory, "*.md")):
        os.remove(name)
    port = f"{uuid.uuid4().int % 50000 + 10000}"
    with open(os.path.join(memory, "MEMORY.md"), "w", encoding="utf-8") as f:
        f.write(f"- [Dev port](dev-port.md) — the dev server port is {port}\n")
    # A work dir that has never been used: only the project ID can lead it to that memory.
    s = Session(workdir=os.path.join(HERE, ".state", "work-key", "b", "c"), project=project)
    out = s.say("According to your memory index, what is the dev server port? Reply with just the number.")
    assert port in out, out
    s.exit()


def host_path(path):
    return os.path.join(STATE, os.path.relpath(path, "/state"))


def fork_tool_calls(fork):
    with open(fork, encoding="utf-8") as f:
        msgs = [json.loads(line) for line in f if line.strip()]
    return [b for m in msgs if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]


def test_memory_extracted_after_turn():
    work = os.path.join(HERE, ".state", "work-extract")
    memory = memory_dir(work)
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


def fresh_workdir(name):
    """A work dir of this test's own, with its .claude settings and instruction files reset."""
    work = os.path.join(HERE, ".state", name)
    os.makedirs(os.path.join(work, ".claude"), exist_ok=True)
    for rel in (".claude/settings.json", ".claude/settings.local.json"):
        with open(os.path.join(work, rel), "w", encoding="utf-8") as f:
            f.write("{}\n")
    return work


def reminders(messages):
    return [b["text"] for m in messages if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "text" and b["text"].startswith("<system-reminder>")]


def test_claude_md_and_nested_instructions():
    work = fresh_workdir("work-instructions")
    codename, team, colour = (f"{w}-{uuid.uuid4().hex[:6]}" for w in ("nebula", "otter", "cerulean"))
    files = {"CLAUDE.md": f"The project codename is {codename}.\n",
             "sub/deep/CLAUDE.md": f"Files in this folder belong to team {team}.\n",
             "sub/deep/notes.txt": "hello\n"}
    for rel, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(work, rel)), exist_ok=True)
        with open(os.path.join(work, rel), "w", encoding="utf-8") as f:
            f.write(text)
    with open(os.path.join(STATE, "test-user-claude.md"), "w", encoding="utf-8") as f:
        f.write(f"The user's favourite colour is {colour}.\n")
    s = Session("--var", "user_instructions=/state/test-user-claude.md", workdir=work)
    out = s.say("According to your instructions, what is the project codename and my favourite colour? "
                "Reply with just the two words.")
    assert codename in out and colour in out, out
    assert team not in out, out                                      # the nested file isn't loaded yet
    out = s.say("Use read_file on sub/deep/notes.txt, then tell me which team that folder belongs to, "
                "per any instructions you got. Reply with just the team.")
    assert team in out.split("Read(")[-1], out
    s.say("Use read_file on sub/deep/notes.txt once more, then reply with only: ok")
    s.exit()
    nested = [r for r in reminders(s.messages()) if team in r]
    assert len(nested) == 1 and "/work/sub/deep/CLAUDE.md" in nested[0], nested   # once per session
    with open(host_path(s.path) + ".instructions", encoding="utf-8") as f:
        snapshot = f.read()
    assert snapshot.index(colour) < snapshot.index(codename), snapshot


def test_always_allow_saves_a_rule():
    work = fresh_workdir("work-perms")
    command = f"echo perm-{uuid.uuid4().hex[:6]}"
    s = Session(workdir=work)
    s.say_until(f"Use the bash tool to run exactly: {command} . Then reply with only: done", APPROVAL)
    out = s.answer("a")
    assert f"saved Bash({command})" in out, out
    with open(os.path.join(work, ".claude", "settings.local.json"), encoding="utf-8") as f:
        assert json.load(f)["permissions"]["allow"] == [f"Bash({command})"]
    out = s.say(f"Run exactly the same command with the bash tool again, then reply with only: done")   # no prompt
    assert f"allowed by Bash({command})" in out, out
    s.exit()
    runs = [r for r in tool_results(s.messages()) if command.split()[1] in r["content"]]
    assert len(runs) == 2 and not any(r.get("is_error") for r in runs), runs


def test_permission_rules_and_hooks():
    work = fresh_workdir("work-hooks")
    magic, post, stop = (f"{w}-{uuid.uuid4().hex[:6]}" for w in ("xyzzy", "postmark", "finale"))
    hook = f"""import json, sys
data = json.load(sys.stdin)
event = data["hook_event_name"]
if event == "UserPromptSubmit":
    print("The magic word is {magic}.")
elif event == "PreToolUse" and "forbidden" in data["tool_input"].get("command", ""):
    print("commands with forbidden in them are not allowed", file=sys.stderr)
    sys.exit(2)
elif event == "PostToolUse":
    print(json.dumps({{"hookSpecificOutput": {{"hookEventName": "PostToolUse", "additionalContext": "{post}"}}}}))
elif event == "Stop" and not data["stop_hook_active"]:
    print("Before you finish, say the word {stop}.", file=sys.stderr)
    sys.exit(2)
"""
    with open(os.path.join(work, ".claude", "hook.py"), "w", encoding="utf-8") as f:
        f.write(hook)
    each = [{"hooks": [{"type": "command", "command": "python3 .claude/hook.py"}]}]
    with open(os.path.join(work, ".claude", "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"permissions": {"deny": ["Bash(touch *)"]},
                   "hooks": {e: each for e in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")}}, f)
    if os.path.exists(os.path.join(work, "made-by-test")):
        os.remove(os.path.join(work, "made-by-test"))
    s = Session("--var", "approve=auto", workdir=work)
    out = s.say("What is the magic word? Reply with just the word.")
    assert magic in out, out                                          # UserPromptSubmit context
    assert "Stop hook sent the agent back" in out and stop in out.split("Stop hook sent the agent back")[-1], out
    s.say("Use the bash tool to run exactly: echo forbidden-thing . Do not retry; reply with only: done")
    s.say("Use the bash tool to run exactly: touch made-by-test . Do not retry; reply with only: done")
    s.say("Use the bash tool to run exactly: echo hello-post . Then reply with only: done")
    s.exit()
    results = tool_results(s.messages())
    assert any(r.get("is_error") and "PreToolUse:Bash hook denied" in r["content"] and "forbidden in them" in r["content"]
               for r in results), results
    assert any(r.get("is_error") and "denied by the rule Bash(touch *)" in r["content"] for r in results), results
    assert not os.path.exists(os.path.join(work, "made-by-test"))
    assert any(f"PostToolUse:Bash hook additional context: {post}" in r for r in reminders(s.messages()))


def test_compact_command():
    code, colour = f"vault-{uuid.uuid4().hex[:6]}", f"mauve-{uuid.uuid4().hex[:6]}"
    s = Session()
    s.say(f"The vault code is {code}. Reply only with: ok")
    s.say(f"The colour is {colour}. Reply only with: ok")
    out = s.say("/compact")
    assert "Conversation compacted" in out and "kept the last 2 messages verbatim" in out, out
    msgs = s.messages()
    assert len(msgs) == 3 and code in msgs[0]["content"][0]["text"], msgs   # the summary, then the latest exchange
    assert msgs[1]["content"] == f"The colour is {colour}. Reply only with: ok", msgs[1]
    assert os.path.exists(host_path(s.path) + ".compacted-1.jsonl")
    out = s.say("What are the vault code and the colour? Reply with just the two.")
    assert code in out and colour in out, out
    s.exit()


def test_auto_compaction():
    code = f"vault-{uuid.uuid4().hex[:6]}"
    s = Session("--var", "compact_at=1")   # every call after the first compacts before it runs
    s.say(f"The vault code is {code}. Reply only with: ok")
    out = s.say("What is the vault code? Reply with just the code.")
    assert "Compacting conversation" in out and "Conversation compacted" in out, out
    assert code in out.split("Conversation compacted")[-1], out
    s.exit()
    first = s.messages()[0]["content"][0]["text"]
    assert first.startswith("This session is being continued"), first
    assert s.messages()[1]["content"] == "What is the vault code? Reply with just the code.", s.messages()[1]   # kept verbatim



def test_context_command():
    s = Session()
    s.say("Reply only with: ok")
    out = s.say("/context")
    for part in ("Context usage", "System prompt", "Tools", "Messages", "(2 messages)", "Free space", "auto-compacts at"):
        assert part in out, out
    assert "estimated" not in out, out                     # counted with count_tokens
    assert len(s.messages()) == 2                          # no turn, nothing recorded
    s.exit()


def test_session_and_subagent_hooks():
    work = fresh_workdir("work-session-hooks")
    target, word, extra = (f"{w}-{uuid.uuid4().hex[:6]}" for w in ("orbit", "tangerine", "precompact"))
    hook = f"""import json, sys
data = json.load(sys.stdin)
event = data["hook_event_name"]
with open(".claude/events.log", "a") as f:
    f.write(event + " " + str(data.get("source") or data.get("trigger") or data.get("reason") or data.get("agent_type") or "") + "\\n")
if event == "SessionStart":
    print("The deploy target is {target}.")
elif event == "PreCompact":
    print("Mention the code word {extra} in the summary.")
elif event == "SubagentStop" and not data["stop_hook_active"]:
    print("End your answer with the word {word}.", file=sys.stderr)
    sys.exit(2)
"""
    with open(os.path.join(work, ".claude", "hook.py"), "w", encoding="utf-8") as f:
        f.write(hook)
    log = os.path.join(work, ".claude", "events.log")
    if os.path.exists(log):
        os.remove(log)
    each = [{"hooks": [{"type": "command", "command": "python3 .claude/hook.py"}]}]
    with open(os.path.join(work, ".claude", "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"hooks": {e: each for e in ("SessionStart", "SessionEnd", "PreCompact", "SubagentStop")}}, f)
    s = Session(workdir=work)
    out = s.say("What is the deploy target? Reply with just the target.")
    assert target in out, out                                         # SessionStart context
    out = s.say("/agent general-purpose Reply with only the word: hello")
    assert "SubagentStop hook sent the general-purpose agent back" in out and word in out.split("sent the")[-1], out
    out = s.say("/compact")
    assert "Conversation compacted" in out, out
    assert extra in s.messages()[0]["content"][0]["text"]             # PreCompact added to the instructions
    out = s.say("What is the deploy target? Reply with just the target.")
    assert target in out, out                                         # SessionStart:compact context again
    s.exit()
    with open(log, encoding="utf-8") as f:
        events = f.read().split("\n")
    assert events[:2] == ["SessionStart startup", "SubagentStop general-purpose"], events
    assert "PreCompact manual" in events and "SessionStart compact" in events, events
    assert events[-2] == "SessionEnd prompt_input_exit", events


PLAN_PROMPT = r"Would you like to proceed\? \(y: yes, a: yes and accept edits, n: no, keep planning\) \[y/a/n\]"


def test_todo_list():
    s = Session()
    out = s.say("Use the todo_write tool to make a todo list with exactly these three items, the first in_progress: "
                "Draft outline, Write chapter, Proofread. Then reply with only: done")
    assert "Update Todos" in out and "Draft outline" in out and "Proofread" in out, out
    s.exit()
    with open(host_path(s.path) + ".todos", encoding="utf-8") as f:
        items = json.load(f)
    assert [t["content"] for t in items] == ["Draft outline", "Write chapter", "Proofread"], items
    assert items[0]["status"] == "in_progress"


def test_plan_mode():
    work = fresh_workdir("work-plan")
    name = f"plan-{uuid.uuid4().hex[:6]}.txt"
    s = Session(workdir=work)
    out = s.say("/plan")
    assert "plan mode on" in out, out
    out = s.say_until(f"Create a file named {name} containing the single word hello. It's a one-step task and "
                      "there is nothing to research: don't use bash or other tools, present the plan right away.", PLAN_PROMPT)
    assert "Here is the plan" in out and name in out.split("Here is the plan")[-1], out
    assert not os.path.exists(os.path.join(work, name))
    out = s.answer("a")                                     # approve, and accept edits: no write approval
    assert "plan approved; permission mode: acceptEdits" in out and "allowed in accept-edits mode" in out, out
    assert "accept edits on" in out.split("allowed in accept-edits mode")[-1], out   # the prompt shows the mode
    s.exit()
    with open(os.path.join(work, name), encoding="utf-8") as f:
        assert f.read().strip() == "hello"
    assert any("Plan mode is active" in r for r in reminders(s.messages()))


def test_at_mention_and_changed_file():
    work = fresh_workdir("work-mention")
    first, second = (f"{w}-{uuid.uuid4().hex[:6]}" for w in ("lemon", "lime"))
    note = os.path.join(work, "note.txt")
    with open(note, "w", encoding="utf-8") as f:
        f.write(f"intro\nThe secret is {first}.\nend\n")
    s = Session(workdir=work)
    out = s.say("What is the secret in @note.txt? Reply with just the secret, without using any tools.")
    assert "attached /work/note.txt" in out and first in out.split("attached")[-1] and "Read(" not in out, out
    with open(note, "w", encoding="utf-8") as f:
        f.write(f"intro\nThe secret is {second}.\nend\n")
    out = s.say("What is the secret in that file now? Don't use any tools; reply with just the secret.")
    assert second in out and "Read(" not in out, out
    s.exit()
    changed = [r for r in reminders(s.messages()) if "was modified" in r]
    assert len(changed) == 1 and f"     2\tThe secret is {second}." in changed[0], changed


def test_session_commands():
    work = fresh_workdir("work-commands")
    s = Session(workdir=work)
    out = s.say("/help")
    assert "/clear" in out and "/model" in out and "@path" in out, out
    out = s.say("/mode")
    assert "permission mode: acceptEdits" in out and "accept edits on" in out, out
    s.say("/mode default")
    out = s.say("/model sonnet")
    assert "model set to claude-sonnet-5-5" in out, out
    s.say("Reply only with: ok")
    out = s.say("/cost")
    assert "Total cost:" in out and "claude-sonnet-5-5:" in out and "$" in out, out
    out = s.say("/clear")
    assert "conversation cleared" in out, out
    assert s.messages() == [] and os.path.exists(host_path(s.path) + ".cleared-1.jsonl")
    word = f"walnut-{uuid.uuid4().hex[:6]}"
    s.say(f"The password is {word}. Reply only with: ok")
    s.exit()
    t = Session(workdir=work, first=["--continue"])           # this work dir's latest session
    assert t.path == s.path, (t.path, s.path)
    out = t.say("/resume")
    assert "no other sessions" in out or "msgs" in out, out
    out = t.say("What is the password? Reply with just it.")
    assert word in out and "claude-sonnet-5-5" in t.transcript.split("session=")[0], t.transcript[:300]   # model kept
    t.exit()


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
