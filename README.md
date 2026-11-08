# markov-harness

A minimal coding-agent harness (a tiny Claude Code) built entirely as a markov workflow: it loops,
prompts for input, shapes the conversation into a Messages API request, calls the model through
model-relay, runs the tools the model asks for, and prints the reply.

![markov-harness demo: reading, editing and running code with approvals, then a memory saved in one session and applied in the next](demo/demo.gif)

```bash
make -C ../markov build-static                         # once: CGO_ENABLED=0 markov at ../markov/bin/static/markov
bin/harness                                            # new session; tools act on $PWD (mounted at /work)
HARNESS_WORKDIR=~/src/project bin/harness              # tools act on another dir
HARNESS_PROJECT_ID=myproject bin/harness               # share one memory across work dirs
bin/harness --var session=/state/sessions/<id>.jsonl   # resume one
bin/harness --var approve=auto                         # run tools without asking
bin/harness --var model=claude-sonnet-5-5
bin/harness --var thinking_display=omitted            # hide the model's thinking (it still thinks)
bin/harness --var compact_at=100000                    # compact the conversation sooner (default 160000 tokens)
python3 tests/test_harness.py                          # end-to-end tests (pexpect, real relay, container)
python3 tests/test_scripts.py                          # unit tests of the scripts (no model, no container)
../markov/bin/static/markov validate .                 # check the workflow without running it
```

## Layout

The repo root is a markov [directory workflow](../markov/docs/reference/workflow-file.md#directory-workflow-layout),
one file per workflow:

| Path | |
|---|---|
| `meta.yaml` | `entrypoint: main`, and the shape of the whole thing in a comment |
| `vars.yaml` | Settings, each overridable with `--var` (model, approval, limits, paths) |
| `vars/prompts.yaml` | The identity line and the system prompt |
| `vars/tools.yaml` | The tool definitions offered to the model |
| `workflows/main.yaml` | Session setup: key, session file, the instructions, memory, skill and agent snapshots, the shared request blocks; repeats `turn` |
| `workflows/turn.yaml` | One user turn: prompt, UserPromptSubmit hooks, record, repeat `agent_step`, memory extraction; `/compact` |
| `workflows/agent_step.yaml` | One model call (compacting first when the context is full), the gate, the tools it asks for, PostToolUse and Stop hooks |
| `workflows/compact.yaml` | Compaction: summarize the conversation and replace it with the summary |
| `workflows/extract/memories.yaml`, `extract/step.yaml` | The memory extraction pass: `extract.memories`, `extract.step` |
| `workflows/tools/*.yaml` | One workflow per tool, named by its path: `tools.bash`, `tools.read_file`, …, `tools.denied`, `tools.unknown` |
| `scripts/` | The Python the steps run (`history.py`, `show.py`, `edit.py`, `search.py`, `memory.py`, `skills.py`, `agents.py`, `input.py`, `instructions.py`, `compact.py`, `settings.py`, `hooks.py`) |
| `skills/` | Skills that ship with the harness, one `<name>/SKILL.md` folder each (see [Skills](#skills)) |
| `agents/` | Subagent types that ship with the harness: `general-purpose` and `Explore` (see [Subagents](#subagents)) |
| `workflows/subagent_step.yaml`, `agent/run.yaml` | A subagent: one model call and its tools, and `agent.run`, which starts one, repeats that and collects its answer (for the tool and for `/agent`) |
| `rules.yaml`, `step_types.yaml` | Empty; markov requires them |

markov fingerprints the whole workflow directory to detect edits on resume, and `.state/` changes on every run, so
`bin/harness` mounts only the workflow files into the container's `/harness`, where markov runs them: `.state/`, `tests/`,
`demo/` and the rest stay out. `main`'s `request` step builds the system blocks and thinking settings
once (`request_base`) and passes them to `agent_step` and `extract.step`, so both send the same bytes and the extraction
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
| `meta.yaml`, `vars.yaml`, `vars/`, `rules.yaml`, `step_types.yaml`, `workflows/`, `scripts/`, `skills/`, `agents/` | `/harness` (the directory workflow) | ro |
| `.state/relay-api-key` | `/run/secrets/relay-api-key` | ro |
| `.state/container/` | `/state` (sessions, memory, personal skills and agents, `CLAUDE.md`, `settings.json`, `markov.log`, `markov-state.db`) | rw |
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
| `bash` | asks, unless a rule allows it (see [Permissions](#permissions)) | Runs in `/work`; output streams to the terminal line by line (`live_output`), then `⎿ exit N` |
| `write_file` | asks, except `.md` files in the memory dir and paths a rule allows | Create or replace a file |
| `edit_file` | asks, after showing the diff, except `.md` files in the memory dir and paths a rule allows | Replace `old_string` with `new_string`; it must occur exactly once unless `replace_all`. `scripts/edit.py` plans the change, the diff is shown, and `apply` checks again before writing |
| `skill` | never | Load a skill's instructions (see [Skills](#skills)) |
| `agent` | never itself; the subagent's own `bash`/`write_file`/`edit_file` ask | Hand a task to a subagent (see [Subagents](#subagents)) |
| `read_file`, `glob`, `grep` | never | Read-only; several in one reply run in parallel (`concurrency: 4`), each printing its call and result together so they don't interleave. `scripts/search.py` skips `.git`, `node_modules` and the like and stops at 200 results |

Every call first goes through the gate (`hooks.py pre`): PreToolUse hooks and permission rules decide whether it
runs, asks, or is denied (`tools.denied` answers it with the reason). Read-only tools in a reply run before the ones that change things; tool calls in one reply are meant to be
independent. Tools stream their input (`eager_input_streaming`), shown as a `… tool input: N chars` line while
the model writes it; `history.py assemble` checks the input is a JSON object, and one that isn't (cut off by
`max_tokens`) gets an error result instead of running.

## Instructions (CLAUDE.md)

A copy of Claude Code's CLAUDE.md loading (`scripts/instructions.py`):

- **At session start**, `instructions.py context` snapshots, in order: the user's file (`/state/CLAUDE.md`, host
  `.state/container/CLAUDE.md`; `--var user_instructions=...` moves it), the work dir's `CLAUDE.md` and
  `.claude/CLAUDE.md` (or `AGENTS.md` when it has neither), and its `CLAUDE.local.md`. `history.py messages` puts
  them first in the first user message, as a `<system-reminder>` with Claude Code's `# claudeMd` preamble, each file
  as `Contents of <path> (<kind>):`. Fixed for the session, like the memory index.
- **Nested:** when `read_file` reads a file in a subdirectory, the `CLAUDE.md` (or `AGENTS.md`) and `CLAUDE.local.md`
  of each directory between the work dir and the file are added after the tool result as a reminder, once per
  session (listed in `<session>.nested`, under a lock, since reads run in parallel). This is Claude Code's
  `nested_memory` attachment.
- Subagents get the session's snapshot too, and their own nested files. Not (yet): `@path` imports, and the
  instructions of the work dir's parents (only the work dir is mounted).

## Compaction

A copy of Claude Code's compaction (`workflows/compact.yaml`, `scripts/compact.py`):

- **When:** before each main-loop model call, `compact.py check` compares the last call's context size (input, cache
  and output tokens, saved to `<session>.usage`) with `compact_at` (160,000; `0` turns it off), so a long turn is
  compacted between its tool rounds too. `/compact [instructions]` compacts at once.
- **How:** one model call with the conversation and a summary prompt modelled on Claude Code's (an `<analysis>`,
  then a `<summary>` with the requests, files and code, errors, every user message, pending tasks and the current
  work). It sends the main loop's system blocks and tools, so it reads their prompt cache, and is told not to call tools.
- **After:** the old session is copied to `<session>.compacted-<n>.jsonl`, and the session becomes one user
  message: the summary, that file's path (the model can `read_file` it for details) and, when automatic, Claude
  Code's instruction to carry on without recapping. The instruction, memory, skill and agent reminders are added
  again as usual, and nested instruction files are sent again when next read.
- Not (yet): keeping the most recent messages verbatim after the summary, and re-attaching recently read files.

## Permissions

Claude Code's permission rules, from settings files (`scripts/settings.py`), merged in order: the user's
`/state/settings.json` (host `.state/container/settings.json`; `--var user_settings=...` moves it), then the work
dir's `.claude/settings.json` and `.claude/settings.local.json`:

```json
{"permissions": {"allow": ["Bash(npm test)", "Bash(git diff *)", "Edit(src/**)"],
                 "ask":   ["Bash(git push *)"],
                 "deny":  ["Bash(rm *)", "Edit(//etc/**)"]}}
```

- **Rules:** a tool name with an optional specifier. `Bash(cmd)` is that exact command; `Bash(cmd *)` or
  `Bash(cmd:*)` that command with or without more words. `Edit(glob)` covers `write_file` and `edit_file`
  (`Write(...)` only `write_file`), with the glob relative to the work dir (`//` for an absolute path; `*` stays in a
  directory, `**` crosses them). The harness's names work too (`bash(...)`, `edit_file(...)`).
- **Order:** deny, then ask, then allow; ask rules prompt even with `approve=auto`. Read-only tools need no rule.
- **Compound commands:** a command with `;`, `&`, `|`, `<`, `>`, a backtick, `$(` or a newline only matches an allow
  rule written for exactly that command, so `Bash(git diff *)` can't pass `git diff; rm -rf x`. Deny and ask rules
  also match each part. This is a convenience, not a sandbox: the container is the sandbox.
- **`a` (always)** at an approval prompt (`[y/n/a]`) runs the call and saves an allow rule for exactly that command
  or file to `.claude/settings.local.json`; later calls show `✻ allowed by <rule>` instead of asking.
- Subagents' calls go through the same rules.

## Hooks

Claude Code's command hooks (`scripts/hooks.py`), configured in the same settings files:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 .claude/check.py"}]}]}}
```

| Event | When | What it can do |
|---|---|---|
| `PreToolUse` | before each tool call (the gate) | exit 2, or `permissionDecision: deny`: deny it, the reason going to the model; `allow` or `ask`; `updatedInput` |
| `PostToolUse` | after a reply's tool calls ran | exit 2, `decision: block` or `additionalContext`: a reminder to the model after the results |
| `UserPromptSubmit` | before a prompt is sent | stdout or `additionalContext`: context after the prompt; exit 2 or `decision: block`: drop the prompt |
| `Stop` | when the model ends its turn | exit 2 or `decision: block`: send it back to work with the reason (`stop_hook_active` is true on the next stop) |

- A hook runs with `sh -c` in the work dir, in the container, with the event as JSON on stdin (`session_id`,
  `transcript_path`, `cwd`, `hook_event_name`, `permission_mode` and the event's fields). Tool names and the
  `file_path` input field are Claude Code's, so hooks written for Claude Code work. The matcher is a regular
  expression over the tool name.
- Other exit codes and timeouts (`timeout`, default 60 s) are shown as a dim note and don't block; `systemMessage`
  is shown too. A denial shows the call and the reason.
- Order of the gate: a hook's deny, a deny rule, a hook's ask, an ask rule, a hook's allow, an allow rule, then the
  defaults (mutating tools ask unless `approve=auto` or a memory file).
- Not (yet): `SessionStart`, `SubagentStop`, `PreCompact`, `Notification` and the other events, `continue: false`,
  and hooks in the extraction pass.

## Memory

A copy of Claude Code's auto-memory (2.1.319, lean prompt variant), kept in the runtime folder instead of `~/.claude`:

- **Where:** `/state/projects/<key>/memory/` (host: `.state/container/projects/<key>/memory/`), one per project, shared by
  every session in it. By default the project is the work dir: `<key>` is the host path with every non-alphanumeric
  character replaced by `-`, as in Claude Code, plus 8 hex of the path's SHA-256, because the slug alone collides
  (`/a/b-c` and `/a/b/c`). `HARNESS_PROJECT_ID=<id>` makes `<key>` the ID instead, so several work dirs (checkouts,
  worktrees, a moved directory) share one memory; a path key starts with `-` and an ID can't, so they never collide.
  `bin/harness --print-memory-dir` prints the host path. `bin/harness` passes it as `memory_dir`;
  `--var memory_dir=...` overrides it.
- **Why per project, not one global memory:** most memories are about the code (`python` is missing here, the deploy
  host), and one shared store would put them in unrelated projects' prompts, share one 200-line index between all of
  them, and have concurrent sessions write one `MEMORY.md` with no locking. A user-level layer for facts that hold
  everywhere is a possible addition, routed by scope rather than by memory type, since feedback is often about one
  project.
- **Prompt:** a third system block with Claude Code's `# Memory` section: one fact per file with
  `name`/`description`/`metadata.type` frontmatter, plus a one-line pointer in `MEMORY.md`.
- **Index:** at session start, `scripts/memory.py context` creates the dir and snapshots `MEMORY.md` (cut at 200 lines,
  then 25,000 characters, with a warning) to `<session>.context`. `history.py messages` puts it at the start of the first
  user message as a `<system-reminder>`, at request time only, so the session file stays clean. The snapshot is fixed for the
  session, so memories saved mid-session don't break the prompt cache; a resumed session takes a new one.
- **Writes:** the model uses `write_file`/`edit_file`; `memory.py owns` lets `.md` files inside the memory dir skip
  the approval prompt.
- **Extraction:** after each turn, `extract.memories` decides with `memory.py fork` whether the turn gets a pass. It
  does when the turn finished, its user message has at least 3 words, and the main agent didn't already write memory
  in it. The pass then runs `extract.step` on a fork, `<session>.jsonl.extract.jsonl`: the parent's messages plus Claude
  Code's extraction prompt, with the existing memory files listed. That file is rewritten on each pass.
  - **Request:** the same system blocks and thinking (`request_base`, from `main`), tools and model as the main loop, so
    it reads the parent's prompt cache. It isn't streamed, so only a dim `✻ memory updated: …` line shows.
  - **Tools:** `memory.py tools` runs the fork's tool calls without asking. `read_file`/`glob`/`grep` work anywhere;
    `write_file`/`edit_file` only on `.md` files in the memory dir; `bash` only as `rm [-f]` of such files (for
    "forget X"). Anything else is denied.
  - **Limits:** at most `extract_max_turns` (5) calls per pass; `--var extract_memories=false` turns it off. It runs
    in line, before the next prompt (markov has no background steps), so it adds a model call to each qualifying turn.

The tests turn extraction off except in the two memory tests, which use their own work dir or their own `--var memory_dir`
(emptied first), and find memory dirs with `--print-memory-dir`.

Not copied (yet): relevance-selected recall and periodic consolidation ("dream").

## Skills

A copy of Claude Code's skills. A skill is a directory with a `SKILL.md`: frontmatter with `name` and
`description`, then the instructions.

- **Where:** searched in this order, and the first skill of a name wins:
  1. `skills/<name>/SKILL.md` at the root of this repo, the skills that ship with the harness (mounted at
     `/harness/skills`, found as `{{ markov_source_dir }}/skills`). A `skills/` folder of
     [Agent Skills](https://agentskills.io) folders is the layout of the anthropics/skills repo and of
     Claude Code plugins.
  2. Personal skills in `/state/skills/` (host: `.state/container/skills/`); `--var skills_dir=...` moves them.
  3. The work dir's `.claude/skills/`, as in Claude Code; `--var project_skills_dir=...` moves them.
- **Listing:** at session start, `scripts/skills.py context` snapshots the skills to `<session>.skills`, and
  `history.py messages` puts it in the first user message as its own `<system-reminder>`, after the memory
  index: `The following skills are available for use with the skill tool:` then `- name: description` lines.
  Like the memory index it is fixed for the session, so the prompt cache holds.
- **The model loads one** with the `skill` tool (`{skill, args}`), with no approval. The tool result is
  `Launching skill: <name>`, and a text block after it in the same user message holds
  `Base directory for this skill: <dir>` and the body, where `$ARGUMENTS` is replaced by `args` (or
  `ARGUMENTS: <args>` is appended). The terminal shows `⏺ Skill(<name>)  ⎿ Successfully loaded skill`.
- **The user loads one** by typing `/<name> [args]`. The user message is Claude Code's
  `<command-message>`/`<command-name>`/`<command-args>` tags, then the same text block; the tool's
  description tells the model a `<command-name>` tag means the skill is already loaded. A `/<word>` that
  names no skill prints `Unknown skill: <word>` and reaches neither the session nor the model; a path
  such as `/usr/bin/x` is plain text.
- **Frontmatter honoured:** `disable-model-invocation: true` leaves a skill out of the listing and the tool
  refuses it; `user-invocable: false` refuses `/<name>`. Not (yet): `allowed-tools`, `model`,
  `argument-hint`, plugin and nested skill dirs, and a listing that changes mid-session.

## Subagents

A copy of Claude Code's Agent tool. The main agent hands a task to a subagent, which works in its own
context (its own session file, system prompt and tools); only its final message comes back, as the tool result.

- **Agent types** are markdown files, `<name>.md`: frontmatter with `name`, `description`, optionally `tools`
  (a list; Claude Code's names, `Read`, `Grep` and so on, map to the harness's) and `model` (`haiku`, `sonnet`,
  `opus`, `inherit` or a model id), then the subagent's system prompt. Searched in order, the first of a name
  winning: `agents/` in this repo (the built-in `general-purpose`, with every tool but `agent`, and `Explore`,
  read-only), then `/state/agents/` (host: `.state/container/agents/`), then the work dir's `.claude/agents/`.
- **Listing:** `scripts/agents.py context` snapshots the types to `<session>.agents` at session start, and
  `history.py messages` adds it as a third `<system-reminder>`: `Available agent types for the agent tool:`, then
  `- name: description (Tools: ...)` lines.
- **The tool:** `agent {description, prompt, subagent_type}`. `tools.agent` runs `agents.py start`, which
  writes the subagent's session, `<session>.agent-<tool_use_id>.jsonl`, with the prompt as its first message,
  then repeats `subagent_step` while it asks for tools (up to `max_tool_rounds`), and answers with its last
  text (`agents.py result`). The terminal shows `⏺ Explore(<description>)`, the subagent's tool calls, and
  `⎿ Done (N tool uses · M model calls)`; its replies aren't streamed.
- **What a subagent gets:** the identity block, a short preamble (it can't ask questions, its last message is
  all that comes back) and its type's prompt as the system prompt; the thinking settings and, by default, the
  main model. Not the main system prompt, the memory or skill listings, or the `agent` tool, so it can't start
  subagents of its own; a call to a tool it may not use gets an error result.
- **The user can start one** two ways (`scripts/input.py`):
  - **`@agent-<name>`** anywhere in a message, naming an agent type: the message goes to the model with Claude Code's
    reminder after it (`The user has expressed a desire to invoke the agent "<name>"...`), so the model calls it,
    writing the prompt itself. A name that is no agent type stays plain text.
  - **`/agent <type> <task>`**: the workflow runs the subagent itself, with no main-model call (`agent.run`, the same
    loop the tool uses), prints its answer, and records the command and its output the way Claude Code records a
    local command (`<command-name>`/`<command-args>`, then `<local-command-stdout>`), so the model sees it next turn.
- **One at a time:** several `agent` calls in one reply run in turn, after the other tools, because a
  subagent's `bash`, `write_file` and `edit_file` still ask for approval. Not (yet): running read-only types in
  parallel, background subagents, and resuming a subagent.

## How it works

| Piece | Where |
|---|---|
| Loop | markov `repeat` (flat do-while loops): `main` repeats `turn` until `/exit`; `turn` repeats `agent_step` while the model asks for tools (or a Stop hook sends it back), up to `max_tool_rounds` (50) calls; the extraction pass repeats `extract.step` up to `extract_max_turns` (5) |
| User input / tool approval | markov `prompt` step (needs a TTY) |
| Conversation state | `/state/sessions/<run>.jsonl` (host: `.state/container/sessions/`), one message per line, appended with `write_file` |
| Request shaping | `scripts/history.py` turns the JSONL into `messages`, merging same-role lines |
| Model call | `http_request` to `relay-api.local/v1/messages` with `stream: {format: sse, text: delta.text, thinking: delta.thinking}`; `--callback tty://` prints each text delta as it arrives, and the thinking summary dimmed before it, and `history.py assemble` rebuilds the message from the events |
| Tools | one workflow per tool, `tools.<name>` (`workflows/tools/<name>.yaml`), dispatched with `for_each` over `tool_use` blocks: read-only ones with `concurrency: 4`, then the rest one at a time |
| Live display | `--callback "tty://?show=text,thinking:dim,output:lines,tool_input:count"`: reply text, thinking dimmed, bash output lines, and a running count of tool input |
| Caching | top-level `cache_control: {type: ephemeral}` caches the conversation so far; the usage line shows `cache read N write N` |
| Output | `scripts/show.py` writes to `/dev/tty`, because markov captures step stdout |

markov's own log goes to `.state/container/markov.log`. The relay key is in `.state/relay-api-key` (project
`markov-harness`, upstream `personal-anthropic`); markov masks it in events as a secret step output.

## Markov findings

- `prompt` requires a TTY, so this can't run inside markov-ide (it gives runs `stdin: null`).
- Whitespace after a single `{{ expr }}` used to be trimmed (`"{{ x | to_json }}\n"` lost the `\n`),
  because `exactExpression` trimmed before matching. Fixed in markov; the `{{ nl }}` workaround is gone.
- The template docs listed `truncate`; pongo2's filter is `truncatechars`, and `markov validate` didn't catch
  unknown filters. Both fixed in markov: `validate` now parses every template and expression.
- `load_artifact` ignored `ignore_errors`, so a missing file crashed the session. Fixed in markov; `read_file` stays
  on `scripts/search.py read`, which returns the error as the tool result text without extra steps.
- No map literals in templates; build records with `set_fact` maps instead.
- The newer models need the Claude Code identity line as the first system block (see `client_identity`).

## License

[Apache License 2.0](LICENSE)
