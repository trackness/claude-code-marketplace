# trackness marketplace

Claude Code plugin marketplace.

## Install

```bash
claude plugin install gh-pm@trackness
```

## Plugins

| Plugin | Version | Description | Source |
|--------|---------|-------------|--------|
| [`gh-pm`](https://github.com/trackness/gh-pm) | 3.0.0 | GitHub project management workflows, enforcement hooks, and PR reviewer agent | [trackness/gh-pm](https://github.com/trackness/gh-pm) |

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
