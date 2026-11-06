"""Drive a short scripted harness demo, for recording with asciinema (see "Demo" in README.md).

    asciinema rec --headless -c "python3 demo/drive.py .state/demo-1" --window-size 100x30 --idle-time-limit 2 demo/demo.cast
    agg --speed 1.5 --font-size 14 --last-frame-duration 5 --font-family "Noto Sans Mono,Adwaita Mono,Symbola" demo/demo.cast demo/demo.gif

Sets up a tiny project in the given work dir (which must not exist yet, so its memory starts
empty), then runs two sessions: the first edits and runs code and mentions a preference, which the
extraction pass saves; the second shows that preference applied from memory. Input is typed a
character at a time and every approval is answered "y".
"""
import os
import re
import sys
import time

import pexpect

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT = ">: "
APPROVAL = r"(run|write|apply) it\? \[y/n\]"
HELLO = '''def greet(name):
    return f"Hello, {name}!"


if __name__ == "__main__":
    print(greet("world"))
'''

SESSIONS = [
    [
        "read hello.py and sum it up in one sentence",
        "add a --name option, defaulting to world, then run it with --name markov",
        "looks good. fyi, I always want type hints in any Python I write. no changes needed now",
    ],
    [
        "add a farewell(name) function to hello.py",
    ],
]


def type_out(child, text, delay=0.045):
    for ch in text:
        child.send(ch)
        time.sleep(delay)
    time.sleep(0.4)
    child.send("\r")


def shell_line(text):
    sys.stdout.write("\033[1;32m$\033[0m ")
    sys.stdout.flush()
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(0.03)
    sys.stdout.write("\r\n")
    sys.stdout.flush()
    time.sleep(0.5)


def run_session(workdir, turns):
    rel = os.path.relpath(workdir, HERE)
    shell_line(f"HARNESS_WORKDIR={rel} bin/harness")
    child = pexpect.spawn(os.path.join(HERE, "bin", "harness"), cwd=HERE, encoding="utf-8", timeout=180,
                          env={**os.environ, "HARNESS_WORKDIR": workdir}, dimensions=(30, 100))
    child.logfile_read = sys.stdout
    child.expect(PROMPT)
    for text in turns:
        time.sleep(1.0)
        type_out(child, text)
        while child.expect([PROMPT, APPROVAL, r"API error"]) == 1:
            time.sleep(1.2)
            type_out(child, "y", delay=0)
        if "API error" in child.after:
            sys.exit("harness reported an API error")
    time.sleep(1.5)
    type_out(child, "/exit")
    child.expect(pexpect.EOF)
    child.close()
    time.sleep(1.0)


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: drive.py <new work dir>")
    workdir = os.path.realpath(argv[1])
    os.makedirs(workdir)   # fails if it exists: the demo needs an empty memory
    with open(os.path.join(workdir, "hello.py"), "w", encoding="utf-8") as f:
        f.write(HELLO)
    shell_line(f"cat {os.path.relpath(workdir, HERE)}/hello.py")
    sys.stdout.write(HELLO.replace("\n", "\r\n"))
    time.sleep(1.5)
    for turns in SESSIONS:
        run_session(workdir, turns)
    key = re.sub(r"[^A-Za-z0-9]", "-", workdir)
    shell_line(f"cat .state/container/projects/*{re.sub(r'[^A-Za-z0-9]', '-', os.path.basename(workdir))}/memory/MEMORY.md")
    with open(os.path.join(HERE, ".state", "container", "projects", key, "memory", "MEMORY.md"), encoding="utf-8") as f:
        sys.stdout.write(f.read().replace("\n", "\r\n"))
    time.sleep(3)


if __name__ == "__main__":
    main(sys.argv)
