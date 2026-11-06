# markov-harness

A minimal coding-agent harness (a tiny Claude Code) built entirely as a markov workflow: it loops,
prompts for input, shapes the conversation into a Messages API request, calls the model through
model-relay, runs the tools the model asks for, and prints the reply.

![markov-harness demo: reading, editing and running code with approvals, then a memory saved in one session and applied in the next](demo/demo.gif)

```bash
make -C ../markov build-static                         # once: CGO_ENABLED=0 markov at ../markov/bin/static/markov
bin/harness                                            # new session; tools act on $PWD (mounted at /work)
HARNESS_WORKDIR=~/src/project bin/harness              # tools act on another dir
bin/harness --var session=/state/sessions/<id>.jsonl   # resume one
bin/harness --var approve=auto                         # run tools without asking
bin/harness --var model=claude-sonnet-5-5
bin/harness --var thinking_display=omitted            # hide the model's thinking (it still thinks)
python3 tests/test_harness.py                          # end-to-end tests (pexpect, real relay, container)
../markov/bin/static/markov validate workflow          # check the workflow without running it
```

## Layout

The harness is a markov [directory workflow](../markov/docs/reference/workflow-file.md#directory-workflow-layout) in
`workflow/`, one file per workflow:

| Path | |
|---|---|
| `workflow/meta.yaml` | `entrypoint: main`, and the shape of the whole thing in a comment |
| `workflow/vars.yaml` | Settings, each overridable with `--var` (model, approval, limits, paths) |
| `workflow/vars/prompts.yaml` | The identity line and the system prompt |
| `workflow/vars/tools.yaml` | The tool definitions offered to the model |
| `workflow/workflows/main.yaml` | Session setup: key, session file, memory snapshot, the shared request blocks; repeats `turn` |
| `workflow/workflows/turn.yaml` | One user turn: prompt, record, repeat `agent_step`, memory extraction |
| `workflow/workflows/agent_step.yaml` | One model call and the tools it asks for |
| `workflow/workflows/extract_memories.yaml`, `extract_step.yaml` | The memory extraction pass |
| `workflow/workflows/tool_*.yaml` | One workflow per tool (`tool_bash`, `tool_read_file`, …, `tool_unknown`) |
| `workflow/scripts/` | The Python the steps run (`history.py`, `show.py`, `edit.py`, `search.py`, `memory.py`) |
| `workflow/rules.yaml`, `step_types.yaml` | Empty; markov requires them |

The workflow is its own directory, not the repo root, because markov fingerprints the whole workflow directory to detect
edits on resume, and `.state/` changes on every run. `main`'s `request` step builds the system blocks and thinking settings
once (`request_base`) and passes them to `agent_step` and `extract_step`, so both send the same bytes and the extraction
pass reads the main loop's prompt cache. When the workflow was one file, a YAML anchor did this, but anchors don't reach
across files.

## Demo

`demo/demo.gif` (above) is rendered from `demo/demo.cast`, an [asciinema](https://asciinema.org) recording of
`demo/drive.py`: a scripted run that types the prompts and answers every approval with `y`. The model's replies are real
(claude-haiku-5-5). In the first session the extraction pass saves that `python` is missing, and the user states a preference
that the main agent saves. The second session then applies it. To record it again (asciinema 3 and
[agg](https://github.com/asciinema/agg)):

```bash
asciinema rec --headless -c "python3 demo/drive.py .state/demo-3" --window-size 100x30 --idle-time-limit 2 demo/demo.cast
agg --speed 1.5 --font-size 14 --last-frame-duration 5 --font-family "Noto Sans Mono,Adwaita Mono,Symbola" demo/demo.cast demo/demo.gif
```

`asciinema play demo/demo.cast` replays it in a terminal. The work dir must be new, so its memory starts empty.

## Container

`bin/harness` runs markov in a rootless podman container (image `localhost/markov-harness:1`, built from
`container/Containerfile` on first use). Nothing of the harness is baked into the image; it is all bind-mounted,
each mount with `:z` for SELinux:

| Host | Container | |
|---|---|---|
| `../markov/bin/static/markov` | `/usr/local/bin/markov` | ro |
| `workflow/` | `/harness` (the directory workflow) | ro |
| `.state/relay-api-key` | `/run/secrets/relay-api-key` | ro |
| `.state/container/` | `/state` (sessions, memory, `markov.log`, `markov-state.db`) | rw |
| `$HARNESS_WORKDIR` (default `$PWD`) | `/work`, the tools' cwd | rw |

The container runs with `--userns=keep-id`, `--cap-drop=all`, `no-new-privileges`, a read-only root with a tmpfs `/tmp`,
and 2 GB / 512 pid limits. The launcher won't mount `/`, `$HOME`, `/usr`, `/etc` and the like as the work dir. When the relay
resolves to one of this host's own addresses it is mapped to `host-gateway`, because with pasta networking the container
gets a copy of the host's IP.

Tools run in the same container as markov, the way Claude Code runs its tools in its own environment: the container is the
sandbox boundary. So the network is open, and the bash tool can read the relay key. The key belongs to a dedicated relay
project (`markov-harness`) and can be rotated or revoked from the relay admin API.

## Tools

| Tool | Approval | |
|---|---|---|
| `bash` | asks | Runs in `/work`; output streams to the terminal line by line (`live_output`), then `⎿ exit N` |
| `write_file` | asks, except `.md` files in the memory dir | Create or replace a file |
| `edit_file` | asks, after showing the diff, except `.md` files in the memory dir | Replace `old_string` with `new_string`; it must occur exactly once unless `replace_all`. `workflow/scripts/edit.py` plans the change, the diff is shown, and `apply` checks again before writing |
| `read_file`, `glob`, `grep` | never | Read-only; several in one reply run in parallel (`concurrency: 4`), each printing its call and result together so they don't interleave. `workflow/scripts/search.py` skips `.git`, `node_modules` and the like and stops at 200 results |

Read-only tools in a reply run before the ones that change things; tool calls in one reply are meant to be
independent. Tools stream their input (`eager_input_streaming`), shown as a `… tool input: N chars` line while
the model writes it; `history.py assemble` checks the input is a JSON object, and one that isn't (cut off by
`max_tokens`) gets an error result instead of running.

## Memory

A copy of Claude Code's auto-memory (2.1.319, lean prompt variant), kept in the runtime folder instead of `~/.claude`:

- **Where:** `/state/projects/<key>/memory/` (host: `.state/container/projects/<key>/memory/`), where `<key>` is the
  host work dir with every non-alphanumeric character replaced by `-`. So each `HARNESS_WORKDIR` has its own memory.
  `bin/harness` passes it as `memory_dir`; `--var memory_dir=...` overrides it.
- **Prompt:** a third system block with Claude Code's `# Memory` section: one fact per file with
  `name`/`description`/`metadata.type` frontmatter, plus a one-line pointer in `MEMORY.md`.
- **Index:** at session start, `workflow/scripts/memory.py context` creates the dir and snapshots `MEMORY.md` (cut at 200 lines,
  then 25,000 characters, with a warning) to `<session>.context`. `history.py messages` puts it at the start of the first
  user message as a `<system-reminder>`, at request time only, so the session file stays clean. The snapshot is fixed for the
  session, so memories saved mid-session don't break the prompt cache; a resumed session takes a new one.
- **Writes:** the model uses `write_file`/`edit_file`; `memory.py owns` lets `.md` files inside the memory dir skip
  the approval prompt.
- **Extraction:** after each turn, `extract_memories` decides with `memory.py fork` whether the turn gets a pass. It
  does when the turn finished, its user message has at least 3 words, and the main agent didn't already write memory
  in it. The pass then runs `extract_step` on a fork, `<session>.jsonl.extract.jsonl`: the parent's messages plus Claude
  Code's extraction prompt, with the existing memory files listed. That file is rewritten on each pass.
  - **Request:** the same system blocks (YAML anchors), tools, thinking and model as the main loop, so it reads the
    parent's prompt cache. It isn't streamed, so only a dim `✻ memory updated: …` line shows.
  - **Tools:** `memory.py tools` runs the fork's tool calls without asking. `read_file`/`glob`/`grep` work anywhere;
    `write_file`/`edit_file` only on `.md` files in the memory dir; `bash` only as `rm [-f]` of such files (for
    "forget X"). Anything else is denied.
  - **Limits:** at most `extract_max_turns` (5) calls per pass; `--var extract_memories=false` turns it off. It runs
    in line, before the next prompt (markov has no background steps), so it adds a model call to each qualifying turn.

The tests turn extraction off except in the two memory tests, which use their own work dir or a fresh `--var memory_dir`.

Not copied (yet): relevance-selected recall and periodic consolidation ("dream").

## How it works

| Piece | Where |
|---|---|
| Loop | markov `repeat` (flat do-while loops): `main` repeats `turn` until `/exit`; `turn` repeats `agent_step` while the model asks for tools, up to `max_tool_rounds` (50) calls; the extraction pass repeats `extract_step` up to `extract_max_turns` (5) |
| User input / tool approval | markov `prompt` step (needs a TTY) |
| Conversation state | `/state/sessions/<run>.jsonl` (host: `.state/container/sessions/`), one message per line, appended with `write_file` |
| Request shaping | `workflow/scripts/history.py` turns the JSONL into `messages`, merging same-role lines |
| Model call | `http_request` to `relay-api.local/v1/messages` with `stream: {format: sse, text: delta.text, thinking: delta.thinking}`; `--callback tty://` prints each text delta as it arrives, and the thinking summary dimmed before it, and `history.py assemble` rebuilds the message from the events |
| Tools | one workflow per tool, `tool_<name>`, dispatched with `for_each` over `tool_use` blocks: read-only ones with `concurrency: 4`, then the rest one at a time |
| Live display | `--callback "tty://?show=text,thinking:dim,output:lines,tool_input:count"`: reply text, thinking dimmed, bash output lines, and a running count of tool input |
| Caching | top-level `cache_control: {type: ephemeral}` caches the conversation so far; the usage line shows `cache read N write N` |
| Output | `workflow/scripts/show.py` writes to `/dev/tty`, because markov captures step stdout |

markov's own log goes to `.state/container/markov.log`. The relay key is in `.state/relay-api-key` (project
`markov-harness`, upstream `personal-anthropic`); markov masks it in events as a secret step output.

## Markov findings

- `prompt` requires a TTY, so this can't run inside markov-ide (it gives runs `stdin: null`).
- Whitespace after a single `{{ expr }}` used to be trimmed (`"{{ x | to_json }}\n"` lost the `\n`),
  because `exactExpression` trimmed before matching. Fixed in markov; the `{{ nl }}` workaround is gone.
- The template docs list `truncate`; pongo2's filter is `truncatechars`. `markov validate` doesn't
  catch unknown filters.
- No map literals in templates; build records with `set_fact` maps instead.
- The newer models need the Claude Code identity line as the first system block (see `client_identity`).
