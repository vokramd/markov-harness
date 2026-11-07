# Built-in agent types

Subagent types for the agent tool, one markdown file each: `agents/<name>.md`, with `name`, `description`,
optionally `tools` (a list, Claude Code or harness names; default every tool but `agent`) and `model`
(`haiku`, `sonnet`, `opus`, `inherit` or a model id; default `inherit`) in the frontmatter, then the system
prompt. `bin/harness` mounts this folder at `/harness/agents`. It is searched first, before the personal agents
in `/state/agents` and the work dir's `.claude/agents`, so a type here wins over one of the same name there.
This README isn't an agent type.
