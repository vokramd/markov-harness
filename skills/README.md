# Bundled skills

Skills that ship with the harness, one [Agent Skills](https://agentskills.io) folder each:
`skills/<name>/SKILL.md`, with `name` (matching the folder) and `description` frontmatter, then the
instructions. `bin/harness` mounts this folder at `/harness/skills`. It is searched first, before the
personal skills in `/state/skills` and the work dir's `.claude/skills`, so a skill here wins over one of
the same name there. This README isn't a skill: only folders with a `SKILL.md` are.
