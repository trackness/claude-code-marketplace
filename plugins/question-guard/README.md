# question-guard

A `UserPromptSubmit` hook that detects question sentences in each submitted prompt and injects a reminder that a question must be answered and NEVER authorizes action by itself — so a question phrased like a request ("can you clean this up?") is answered, not silently acted on.

## What it does

On every prompt submission the hook scans the prompt text (first 20,000 characters), strips pasted/quoted material (fenced code blocks, inline code spans, URLs, blockquote lines), splits the rest into sentences, and classifies each. If any sentence is a question it injects a short reminder into Claude's context. Prompts with no question produce no output.

## What it detects

A sentence is treated as a **question** when any of these hold:

- it ends with `?`;
- its first word is a WH-word (`who, what, when, where, why, how, which, whose, whom`) and it has at least two words;
- its first word is an auxiliary (`should, could, would, can, will, shall, may, might, do, does, did, is, are, am, was, were, have, has, had`) **and** its second word is a pronoun (`i, we, you, it, they, ...`) — the pronoun gate stops imperatives like "do the refactor" from matching (`the` is deliberately excluded);
- a `?`-less sentence ends with a tag: `, right`, `, no`, `, correct`, `, yeah`.

A sentence is treated as a **directive** (used only to pick the reminder wording) when its first word is a bare imperative verb (`add, fix, write, ...`) or it starts with `please ` / `let's `.

## Reminder variants

- **PURE** (only questions detected): quotes the questions and reminds that a question never authorizes action by itself, and that a question phrased as a request should be answered by naming the action a directive would trigger and stopping there.
- **MIXED** (questions AND directives detected): reminds to answer every question individually AND carry out the directives, while the questions themselves add no scope.

Up to the first 5 detected questions are quoted (each hard-sliced to 200 characters, no ellipsis); the reported count is the true total even when more than 5 are present.

## Delivery + visibility

By default the reminder is delivered **quietly** as `hookSpecificOutput.additionalContext` JSON — added to Claude's context but invisible in the transcript. Set the env var `QUESTION_GUARD_VISIBLE` to a truthy value (`1`, `true`, `yes`, or `on`, case-insensitive, surrounding whitespace ignored) to instead print the reminder as plain stdout, so you can watch it fire while tuning. Any other value — including empty or unset — keeps the quiet mode.

## Install

```bash
claude plugin install question-guard@trackness
```

Then run `/reload-plugins` (or start a fresh session) so the hook is picked up.

## Configuration

Both levers are constants at the top of `scripts/_remind.py` (`scripts/remind_questions.py` is only the interpreter-version guard that hands off to it):

- **Detection (lever 1):** `WH_LEADS`, `AUX_LEADS`, `PRONOUN_NEXT`, `TAG_ENDINGS`, `IMPERATIVE_LEADS`, and `INPUT_SCAN_CAP`.
- **Reminder (lever 2):** `PURE_TEMPLATE`, `MIXED_TEMPLATE`, `MAX_QUOTED_QUESTIONS`, `MAX_QUOTE_CHARS`, `MAX_REMINDER_CHARS`.

The `QUESTION_GUARD_VISIBLE` env var toggles delivery without editing code.

## Requirements

- Python 3.14+ (standard library only — no third-party dependencies). On an older `python3` the hook silently does nothing and exits 0 — **fail-open by design**, because a missing reminder is harmless while a blocked or erased prompt is not.

## Limitations

- Detection is heuristic: it will miss some questions (false negatives) and flag some non-questions (false positives). It never blocks or edits the prompt, so a misfire only adds or omits a reminder.
- The default delivery is invisible in the transcript (`additionalContext`); use `QUESTION_GUARD_VISIBLE` to see it.
- Only the first 20,000 characters of a prompt are scanned; a question that appears only after that is not detected. The reminder itself is clamped to 3,500 characters, well under the platform's 10,000-character context-injection cap.
- The hook is fail-open by design: malformed input, a missing prompt, or any internal error produces no reminder rather than an error.
