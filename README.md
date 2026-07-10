# trackness marketplace

Claude Code plugin marketplace.

## Install

```bash
claude plugin install gh-pm@trackness
```

## Plugins

| Plugin                                        | Version | Description                                                                                  | Source                                                 |
|-----------------------------------------------|---------|-----------------------------------------------------------------------------------------------|---------------------------------------------------------|
| [`gh-pm`](https://github.com/trackness/gh-pm) | 3.0.0   | GitHub project management workflows, enforcement hooks, and PR reviewer agent                 | [trackness/gh-pm](https://github.com/trackness/gh-pm) |
| [`model-guard`](plugins/model-guard)          | 1.0.0   | PreToolUse hook denying subagent/workflow spawns that omit an explicit model (fable banned)   | [plugins/model-guard](plugins/model-guard)            |

### gh-pm

Manages the full lifecycle of GitHub Issues through Claude Code: find gaps (`/gh-pm:audit`), spec work (`/gh-pm:promote`), implement (`/gh-pm:task`), and ship (`/gh-pm:ship`).

**Includes:**
- **1 agent** — `gh-pm:pr-reviewer` for comprehensive PR review
- **5 skills** — `/gh-pm:setup-project`, `/gh-pm:task`, `/gh-pm:ship`, `/gh-pm:audit`, `/gh-pm:promote`
- **6 enforcement hooks** — branch protection, hook bypass prevention, doc staleness checks, agent model enforcement, PR reviewer enforcement, memory write approval

**Requirements:**
- `gh` CLI and `jq` installed
- [`superpowers-extended-cc`](https://github.com/obra/superpowers) plugin enabled
- `.claude/project.json` in each consumer repo (created by `/gh-pm:setup-project`)

### model-guard

Denies `Agent`/`Task`/`Workflow` spawns that omit an explicit `model`, and bans `fable` outright, so subagents never silently inherit the session model.

**Includes:**
- **1 enforcement hook** — `PreToolUse` hook on `Agent|Task|Workflow` that denies spawns without an explicit, non-banned model

**Requirements:**
- Python 3.14+ (standard library only). On an older `python3` the hook fails closed — it denies every `Agent`/`Task`/`Workflow` spawn until a 3.14+ interpreter is used.
