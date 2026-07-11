# question-guard design spec

Date: 2026-07-10

## Problem & goal

Users' prompts often contain questions that Claude wrongly treats as directives (e.g. "can you clean this up?" triggers a cleanup). The repo's CLAUDE.md Questions rule states: a question must ALWAYS be answered and NEVER authorizes action by itself; mixed messages require answering every question AND carrying out every directive. question-guard enforces this by reminder: a UserPromptSubmit hook detects question sentences in each submitted prompt and injects a reminder into Claude's context. Two tuning levers, per the user's request: (1) detection, (2) the reminder — its wording and delivery.

## Verified platform facts (basis of the design)

Verified 2026-07-10 against https://code.claude.com/docs/en/hooks.md, hooks-guide.md, plugins.md, plugins-reference.md, plugin-marketplaces.md:

- UserPromptSubmit supports NO matcher (silently ignored); fires on every prompt submission. stdin JSON includes: session_id, prompt_id, transcript_path, cwd, permission_mode, hook_event_name, prompt. The prompt text field is `prompt`.
- On exit 0, plain stdout is added to Claude's context AND shown in the transcript. JSON `hookSpecificOutput.additionalContext` (with required `"hookEventName": "UserPromptSubmit"`) is instead wrapped in a system reminder — added to context, invisible in the transcript. Both capped at 10,000 characters.
- Exit code 2 AND `decision: "block"` both block processing and ERASE the user's prompt. question-guard must never use either.
- Event default timeout 30s (critical path); per-hook `timeout` field in seconds.
- `${CLAUDE_PLUGIN_ROOT}` is substituted in hooks.json command/args (exec form needs no quoting) and exported as an env var.
- Plugin layout: .claude-plugin/plugin.json manifest; hooks at hooks/hooks.json at plugin root. Marketplace: .claude-plugin/marketplace.json at repo root; local plugin entries use a relative-path `source` and carry NO version field (version lives in plugin.json).
- Local testing: `claude plugin validate`, `claude --plugin-dir <path>`, `/reload-plugins`, `/hooks`, `claude --debug-file`.

## Plugin shape

```
plugins/question-guard/
├── .claude-plugin/plugin.json
├── hooks/hooks.json
├── scripts/remind_questions.py   (entry: version guard, every-Python-3 syntax)
├── scripts/_remind.py            (core: Python 3.14 code)
├── tests/test_detector.py
├── tests/test_output.py
├── pyproject.toml
└── README.md
```

plugin.json exactly:

```json
{
  "name": "question-guard",
  "displayName": "Question Guard",
  "version": "1.0.0",
  "description": "UserPromptSubmit hook injecting a reminder that a question requires an answer, never action (questions are not directives)",
  "author": { "name": "trackness", "email": "trackness@users.noreply.github.com" }
}
```

hooks/hooks.json exactly (note: no matcher key — the event supports none):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3",
            "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/remind_questions.py"],
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Timeout 10s is self-imposed, under the event's 30s default, because the hook sits on every prompt's critical path.

## Interpreter targeting (user directive: target Python 3.14)

Core logic (`_remind.py`) is written for Python 3.14, matching model-guard and this repo's pyproject convention (`target-version = "py314"`, same ruff select list as plugins/model-guard/pyproject.toml: ["E", "W", "F", "I", "N", "UP", "B", "C4", "SIM", "RET", "ARG", "PL", "RUF"]). The entry script `remind_questions.py` uses only syntax every Python 3 parses: it checks `sys.version_info < (3, 14)` and on an older interpreter exits 0 SILENTLY (no output) — fail-open, deliberately the opposite of model-guard's fail-closed guard, because a missing reminder is harmless while noise or a blocked prompt is not. On 3.14+ it lazily imports `_remind` by name via importlib and calls its main(). Scripts are non-executable (rw-r--r--) with `#!/usr/bin/env python3` shebang, invoked via explicit `python3` in hooks.json — house convention. Stdlib only, no network.

## Detection (lever 1)

Pipeline in `_remind.py`, all pattern lists as named constants at the top of the file (the tuning surface):

1. Read stdin JSON; take `prompt`. Scan at most the first 20,000 characters (INPUT_SCAN_CAP = 20_000) — latency guard.
2. Sanitize, applying these steps in this fixed order: (1) remove fenced code blocks (``` and ~~~), (2) remove inline code spans (backticks), (3) remove URLs (regex \bhttps?://\S+), (4) remove blockquote lines (lines starting with optional whitespace then >). The order is load-bearing: fenced blocks are stripped before inline-code spans so a triple-backtick fence is not mangled by single-backtick removal. Pasted/quoted material is not the user's own asking.
3. Sentence split: on newlines, then within lines on runs of .?! followed by whitespace or end; terminator stays with its sentence.
4. A sentence is a QUESTION if any of the following hold (the tests are OR-combined; each sentence is classified at most once, so a sentence satisfying several tests — e.g. both `?`-terminated and WH-led — still counts as a single question). Detected question sentences are collected into an order-preserving list holding exactly one entry per detected sentence (multiple test matches on the same sentence never create multiple entries); the reminder's `{n}` is the length of that list.
   a. it ends with `?`;
   b. its first word (case-insensitive) is in WH_LEADS = {who, what, when, where, why, how, which, whose, whom} and the sentence has ≥ 2 words;
   c. its first word is in AUX_LEADS = {should, could, would, can, will, shall, may, might, do, does, did, is, are, am, was, were, have, has, had} AND its second word is in PRONOUN_NEXT = {i, we, you, it, they, he, she, this, that, these, those, there, anyone, anybody, someone, somebody, one} — the pronoun requirement stops imperatives like "do the refactor" from matching. The article "the" is deliberately NOT a member of PRONOUN_NEXT, precisely so an AUX-led imperative such as "do the refactor" is never misread as a question;
   d. a `?`-less sentence ends with a tag in TAG_ENDINGS = {", right", ", no", ", correct", ", yeah"}.
5. DIRECTIVE detection (used ONLY to pick the reminder variant, so false positives are low-stakes): a sentence whose first word is in IMPERATIVE_LEADS (a constant listing bare directive verbs: add, fix, write, implement, update, remove, make, run, create, change, refactor, delete, rename, deploy, install, build, move, use, stop, start, revert, merge, push, commit, rebase, split, extract, convert, migrate, rewrite, document, test, ensure), or starts with "please " or "let's ".
6. No questions found → print nothing, exit 0.

Slash-command prompts (prompt whose first non-whitespace character is `/`) are NOT special-cased: they flow through the same pipeline unchanged. A bare command line ("/reload-plugins") matches no question test and yields silence; a command line whose arguments carry a natural-language question ("/review is the login flow safe?") is reminded like any other prompt. No command-specific branching is added — this keeps the hook fail-open with the smallest surface.

## Reminder (lever 2)

Composition: the numbered quote block shows the first MAX_QUOTED_QUESTIONS = 5 distinct detected question sentences in prompt order, each hard-truncated to at most MAX_QUOTE_CHARS = 200 characters (plain character slice, no ellipsis). `{n}` in the templates is the total number of distinct detected question sentences — which may exceed 5; when it does, `{n}` still reports the true total while only the first 5 are quoted, so the "answer EVERY question" instruction is never under-counted. The assembled reminder is finally clamped to MAX_REMINDER_CHARS = 3500 (headroom under the platform's 10k cap): if it would exceed that, trailing quoted questions are dropped and, if it is still over, the quote block is hard-sliced — the template instruction text is NEVER trimmed, so the guidance always survives intact. Given the per-quote (200) and count (5) caps this clamp is a defensive backstop that is not reached in normal operation. Two template constants:

- PURE variant (no directives detected): "The user's latest message contains {n} question(s):\n{numbered quotes}\nQuestions are questions. Answer each one directly. A question NEVER authorizes action by itself. If a question reads like a request to act (e.g. 'can you clean this up?'), name the action a directive would trigger and stop there — do not perform it."
- MIXED variant (directives also detected): "The user's latest message mixes questions with directives. The question(s):\n{numbered quotes}\nAnswer EVERY question individually AND carry out the directives. The questions themselves add no scope: act only on what is explicitly directed."

Delivery: default is quiet — emit a single JSON object {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": <reminder>}} serialized with `json.dumps` (never hand-assembled), so quotes, newlines, backslashes, and control characters in the quoted user text are safely escaped. If env var QUESTION_GUARD_VISIBLE, once `.strip().lower()`-ed, equals one of {1, true, yes, on}, print the reminder as plain stdout instead, so the user can watch it fire while tuning; any other value — including empty or unset — selects the quiet JSON mode. Always exit 0.

## Failure policy

Fail-open everywhere: whole main wrapped so malformed stdin, missing prompt field, or any internal error prints nothing and exits 0. Never exit 2, never emit decision:"block" — both erase the user's prompt. No stderr chatter on the happy path or on handled errors.

## Testing

unittest.TestCase style (runs under python3 -m unittest discovery AND pytest, matching the repo's tests/ convention). test_detector.py: ?-sentences; no-? interrogatives ("why is the cache cold"); aux+pronoun ("can you clean this up?", "do we have tests"); imperative non-match ("do the refactor", "fix the bug then run tests"); tag questions; ? inside fenced code / inline code / URLs / ternary expressions not firing; blockquote stripping; mixed prompts choosing MIXED variant; empty prompt; >20k-char prompt scan cap; quote truncation at 200 chars; reminder cap at 3500. test_output.py: exact additionalContext JSON shape incl. hookEventName; visible-mode plain stdout; env-var truthiness parsing; silence + exit 0 on no questions, malformed JSON stdin, missing prompt key. Manual pre-PR verification: claude plugin validate, a live claude --plugin-dir plugins/question-guard session, /hooks listing, claude --debug-file observation of the injection.

## Marketplace registration

.claude-plugin/marketplace.json gains (local-plugin convention — relative source, NO version field):

```json
{
  "name": "question-guard",
  "description": "UserPromptSubmit hook injecting a reminder that a question requires an answer, never action (questions are not directives)",
  "source": "./plugins/question-guard",
  "author": { "name": "trackness", "email": "trackness@users.noreply.github.com" }
}
```

Root README.md gains a table row (version 1.0.0, links to plugins/question-guard) and a short section following the model-guard section's format: what it enforces, Includes (1 enforcement hook — UserPromptSubmit reminder injection), Requirements (Python 3.14+; on older interpreters the hook silently does nothing — fail-open by design), Install (claude plugin install question-guard@trackness), and the two levers under Configuration (constants at top of scripts/_remind.py; QUESTION_GUARD_VISIBLE env var).

## Out of scope

LLM-based question classification (possible future lever), plugin.json userConfig surface, fixing the stale gh-pm version row in the root README.

## Branch & lifecycle

Branch feat/question-guard-plugin carries: this spec (committed first, for review); the ten changed files that ship the plugin — the eight new files under plugins/question-guard/ (.claude-plugin/plugin.json, hooks/hooks.json, scripts/remind_questions.py, scripts/_remind.py, tests/test_detector.py, tests/test_output.py, pyproject.toml, README.md) plus two registration edits (.claude-plugin/marketplace.json and the root README.md); and — per the user's directive that superpowers docs must not survive the merge — a final `chore: remove design spec before merge` commit deleting docs/superpowers/ before the branch is squash-merged, so the spec never reaches main. Squash happens only after the user approves the PR; merge only on explicit say-so.
