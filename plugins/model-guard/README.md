# model-guard

A `PreToolUse` hook that denies subagent (`Agent`/`Task`) and `Workflow` spawns which omit an explicit model, so nothing silently inherits the session model or runs on `fable`.

## What it enforces

- **Agent / Task spawns:** `tool_input.model` must be present and non-blank, and must not be a banned value. A spawn missing `model`, or setting it to a banned value, is denied.
- **Valid models:** `haiku`, `sonnet`, `opus`.
- **Banned:** `fable`, `inherit`. (Configurable — see below.)
- **Workflow spawns:** the submitted script is statically linted. Every `agent(...)` call in the script must set `model:` as a top-level key of its options object, **and** — when the value is a plain string literal — that value must not be blank or a banned model (`fable`/`inherit`), exactly as on the Agent path. So a workflow cannot launder a `fable`/`inherit`/blank subagent spawn. `model` set only inside a `schema:` object, inside prompt text, inside a comment, or inside a regex literal does **not** satisfy the check — those are exactly the holes the lint is designed to close. A dynamically-computed model value (an identifier or expression rather than a literal) cannot be resolved statically and is accepted as satisfying the requirement.

## Frontmatter-pin exception

If `subagent_type` resolves to a project `.claude/agents/<type>.md` (found by walking up from `cwd`) or a user-level `~/.claude/agents/<type>.md`, and that file's YAML frontmatter pins a non-banned `model:`, then omitting `model` on the call is allowed — the frontmatter pin counts as the explicit choice.

This lookup only covers filesystem agent definitions reachable from `cwd` upward and `~/.claude/agents`. **Plugin-provided agent types are not resolvable** by this hook and are always denied if `model` is omitted, even if the plugin's own agent definition pins a model. This is a deliberate over-deny, not a bug: correct it by naming the model explicitly on the call.

## Workflow static-lint semantics and limits

The lint works on the script text submitted with the `Workflow` call (or read from `scriptPath`). It is fail-closed: anything the lint cannot positively verify — unterminated strings, unbalanced parentheses, nested `workflow()` calls, an unreadable `scriptPath`, or a payload carrying none of `script`/`scriptPath`/`name`/`resumeFromRunId` — is denied rather than allowed through.

**Saved / bundled workflow invocations** (name-only or `resumeFromRunId`-only calls, e.g. resuming `/deep-research`, where the caller has no script text to rewrite) are not linted. These get `permissionDecision: "ask"` instead of an automatic deny, so the user gates them at runtime. Set `STRICT_SAVED_WORKFLOWS = True` in the script to flip this to a hard deny instead.

**Known limitation, confirmed empirically:** `PreToolUse` does **not** fire for each `agent(...)` call *inside* a workflow at runtime — only the initial `Workflow` tool call is gated. Confirmed on Claude Code v2.1.205 (2026-07-09) via `claude --debug-file`: internal `agent()` spawns dispatch straight to the API (`source=agent:builtin:workflow-subagent`) inside an execution context with zero hooks registered (`Hooks: Found 0 total hooks in registry`), producing no `tool_dispatch` or `PreToolUse` entry for tool name `Agent`. The static lint performed at `Workflow` submission time is therefore the sole gate for workflow-internal model requirements; it is deliberately biased strict to compensate. If a later Claude Code version adds per-spawn runtime hooking of internal `agent()` calls, this hook will additionally catch those at the point they fire.

## The permissions.allow bypass caveat

Community-reported issue #18312: if `Agent`, `Task`, or `Workflow` appear in a `permissions.allow` list, hook denies for that tool can be silently bypassed. **Consumers of this plugin must not allowlist `Agent`, `Task`, or `Workflow`** in their settings, or this hook's denials will not take effect.

## Requested vs. effective model

This hook enforces the model **requested** at the call site — it cannot see or control the **effective** model actually used. `CLAUDE_CODE_SUBAGENT_MODEL`, if set, outranks the call-site `model` parameter, and an `availableModels` restriction can silently downgrade the effective model. Neither is visible to a `PreToolUse` hook, so both are out of this hook's reach. An explicit, non-banned `model` on the call is the strongest guarantee this hook can provide.

## Install

```bash
claude plugin install model-guard@trackness
```

Then run `/reload-plugins` (or start a fresh session) so the hook is picked up.

## Configuration

Edit the constants at the top of `scripts/enforce_explicit_model.py`:

- `BANNED_MODELS` — set of model names that are rejected even when explicitly named (default: `{"fable", "inherit"}`).
- `ALLOW_FRONTMATTER_PIN` — set to `False` to disable the frontmatter-pin exception entirely and require every spawn to name a model explicitly, even when the subagent type has a pinned model.
- `STRICT_SAVED_WORKFLOWS` — set to `True` to make name-only / `resumeFromRunId`-only `Workflow` calls deny outright instead of prompting the user with `ask`.

## Requirements

- Python 3.14+ (standard library only — no third-party dependencies)
