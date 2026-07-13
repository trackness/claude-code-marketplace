# question-guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a question-guard Claude Code plugin whose `UserPromptSubmit` hook detects question sentences in each submitted prompt and injects a reminder that a question must be answered and NEVER authorizes action by itself.

**Architecture:** A single `UserPromptSubmit` command hook runs `scripts/remind_questions.py`, an interpreter-version guard written in every-Python-3 syntax that silently no-ops below 3.14 and otherwise lazily imports the Python 3.14 core `scripts/_remind.py`. The core sanitizes the prompt, detects question (and, for variant selection, directive) sentences via named constant pattern-sets, composes an adaptive reminder quoting the detected questions, and delivers it either as quiet `additionalContext` JSON (default) or, under `QUESTION_GUARD_VISIBLE`, as plain stdout. Every tuning knob is a constant at the top of `_remind.py` (constants-as-levers); the hook is fail-open throughout.

**Tech Stack:** Python 3.14 (stdlib only), unittest, ruff (py314), Claude Code plugin hooks

**User decisions (already made):**
- Sentence-heuristic detection (not bare-?, not LLM)
- Adaptive reminder quoting detected questions; PURE vs MIXED variants
- Quiet additionalContext default; QUESTION_GUARD_VISIBLE env toggle for visible stdout (approved as-is after explicit re-ruling)
- Target Python 3.14; entry script silently no-ops (exit 0) below 3.14 — fail-open
- Python over bash+jq (jq was permitted; Python chosen for detector complexity, lever testability, stdout-hazard control)
- Superpowers docs (docs/superpowers/) must NOT survive the merge — removed by a final chore commit before squash-merge
- Branch feat/question-guard-plugin; Conventional Commits; one PR; merge only on explicit user say-so

---

## Conventions used by every task

- All paths are relative to the repo root `/Users/james/workspace/claude-code-marketplace` unless a `cd` is shown.
- Scripts are created with default permissions (`rw-r--r--`, non-executable) and invoked via an explicit `python3` in `hooks.json` — house convention. Do NOT `chmod +x` them.
- Every commit uses the two trailers verbatim (subject in the first `-m`, both trailer lines in a second `-m`):

```bash
git commit -m "<conventional-commits subject>" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GsMUs3eWHPku9ss86owvuH"
```

- The ruff invocation used throughout (mirrors `plugins/question-guard/pyproject.toml`):

```bash
ruff check --target-version py314 --select E,W,F,I,N,UP,B,C4,SIM,RET,ARG,PL,RUF plugins/question-guard/scripts plugins/question-guard/tests
```

---

### Task 1: Plugin skeleton

**Goal:** Scaffold the question-guard plugin with its manifest, `UserPromptSubmit` hook config, and ruff pyproject so the plugin is structurally valid and discoverable.

**Files:**
- Create `plugins/question-guard/.claude-plugin/plugin.json`
- Create `plugins/question-guard/hooks/hooks.json`
- Create `plugins/question-guard/pyproject.toml`

**Acceptance Criteria:**
- [ ] `plugins/question-guard/.claude-plugin/plugin.json` is valid JSON with `name` `"question-guard"`, `version` `"1.0.0"`, `displayName` `"Question Guard"`.
- [ ] `plugins/question-guard/hooks/hooks.json` is valid JSON registering a `UserPromptSubmit` command hook that runs `python3` on `${CLAUDE_PLUGIN_ROOT}/scripts/remind_questions.py` with `timeout` 10 and NO `matcher` key.
- [ ] `plugins/question-guard/pyproject.toml` sets ruff `target-version = "py314"` and the model-guard select list.
- [ ] `claude plugin validate ./plugins/question-guard` reports validation passed (when the CLI is available).

**Verify:**
```bash
python3 -c "import json; json.load(open('plugins/question-guard/hooks/hooks.json')); json.load(open('plugins/question-guard/.claude-plugin/plugin.json')); print('ok')"
```
Expected: `ok`. Then:
```bash
claude plugin validate ./plugins/question-guard
```
Expected: `✔ Validation passed` (skip this second command if the `claude` CLI is not on PATH).

**Steps:**
- [ ] Create `plugins/question-guard/.claude-plugin/plugin.json` with exactly:

```json
{
  "name": "question-guard",
  "displayName": "Question Guard",
  "version": "1.0.0",
  "description": "UserPromptSubmit hook injecting a reminder that a question requires an answer, never action (questions are not directives)",
  "author": { "name": "trackness", "email": "trackness@users.noreply.github.com" }
}
```

- [ ] Create `plugins/question-guard/hooks/hooks.json` with exactly:

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

- [ ] Create `plugins/question-guard/pyproject.toml` with exactly:

```toml
[tool.ruff]
target-version = "py314"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "N", "UP", "B", "C4", "SIM", "RET", "ARG", "PL", "RUF"]
```

- [ ] Run the JSON-parse verify command above; confirm it prints `ok`.
- [ ] Run `claude plugin validate ./plugins/question-guard`; confirm `✔ Validation passed` (skip if the CLI is absent).
- [ ] Commit:

```bash
git add plugins/question-guard/.claude-plugin/plugin.json plugins/question-guard/hooks/hooks.json plugins/question-guard/pyproject.toml
git commit -m "feat(question-guard): add plugin manifest, hook config, and pyproject" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GsMUs3eWHPku9ss86owvuH"
```

```json:metadata
{"files": ["plugins/question-guard/.claude-plugin/plugin.json", "plugins/question-guard/hooks/hooks.json", "plugins/question-guard/pyproject.toml"], "verifyCommand": "python3 -c \"import json; json.load(open('plugins/question-guard/hooks/hooks.json')); json.load(open('plugins/question-guard/.claude-plugin/plugin.json')); print('ok')\"", "acceptanceCriteria": ["plugin.json is valid JSON with name question-guard, version 1.0.0, displayName Question Guard", "hooks.json registers a UserPromptSubmit command hook running python3 on ${CLAUDE_PLUGIN_ROOT}/scripts/remind_questions.py with timeout 10 and no matcher key", "pyproject.toml sets ruff target-version py314 and the model-guard select list", "claude plugin validate ./plugins/question-guard passes when the CLI is available"], "modelTier": "mechanical"}
```

---

### Task 2: Detector core + tests

**Goal:** Implement the question-detection core of `_remind.py` (constants, sanitize, sentence split, the four question tests, per-sentence de-duplication) proven by `test_detector.py`.

**Files:**
- Create `plugins/question-guard/scripts/_remind.py` (detector portion — constants + sanitize + split + tokens + `_is_question` + `detect_questions`)
- Create (Test) `plugins/question-guard/tests/test_detector.py` (`DetectQuestionsTests`)

**Acceptance Criteria:**
- [ ] `detect_questions("Is the build green?")` returns `["Is the build green?"]` (rule a: `?`-terminated).
- [ ] `detect_questions("why is the cache cold")` returns 1 question (WH lead, no `?`).
- [ ] `detect_questions("can you clean this up?")` and `detect_questions("do we have tests")` each return 1 (aux lead + pronoun).
- [ ] `detect_questions("do the refactor")` and `detect_questions("fix the bug then run tests")` return `[]` (imperative non-match; `"the"` is not in `PRONOUN_NEXT`).
- [ ] `detect_questions("we ship today, right")` returns 1 (tag question, no `?`).
- [ ] Questions inside fenced code blocks, inline code spans, URLs, and blockquote lines do not fire.
- [ ] A sentence matching multiple tests yields exactly one entry; two distinct questions yield two entries; `""` yields `[]`.
- [ ] All 15 tests in `test_detector.py` pass and the module is ruff-clean.

**Verify:**
```bash
cd plugins/question-guard && python3 -m unittest discover -s tests -v
```
Expected: `Ran 15 tests` … `OK`. Then (from repo root) run the shared ruff command → `All checks passed!`.

**Steps:**
- [ ] Create `plugins/question-guard/tests/test_detector.py` with exactly (this is the FULL failing test):

````python
#!/usr/bin/env python3
"""Detector tests for question-guard's core module.

These exercise ``_remind`` in-process (no subprocess): sentence detection under
the four question tests and sanitisation of pasted/quoted material.

stdlib only; python3 >= 3.14.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import _remind  # noqa: E402


class DetectQuestionsTests(unittest.TestCase):
    """The four OR-combined question tests, sanitisation, and de-duplication."""

    def test_question_mark_terminated(self):
        self.assertEqual(_remind.detect_questions("Is the build green?"),
                         ["Is the build green?"])

    def test_wh_lead_without_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("why is the cache cold")), 1)

    def test_aux_pronoun_with_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("can you clean this up?")), 1)

    def test_aux_pronoun_without_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("do we have tests")), 1)

    def test_imperative_aux_lead_is_not_a_question(self):
        self.assertEqual(_remind.detect_questions("do the refactor"), [])

    def test_plain_imperative_is_not_a_question(self):
        self.assertEqual(_remind.detect_questions("fix the bug then run tests"), [])

    def test_tag_question_without_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("we ship today, right")), 1)

    def test_wh_single_word_is_not_a_question(self):
        self.assertEqual(_remind.detect_questions("Why."), [])

    def test_question_mark_in_fenced_code_does_not_fire(self):
        self.assertEqual(_remind.detect_questions("```\nis this real?\n```"), [])

    def test_question_in_inline_code_ternary_does_not_fire(self):
        self.assertEqual(_remind.detect_questions("use `cond ? a : b` here."), [])

    def test_question_mark_in_url_does_not_fire(self):
        self.assertEqual(
            _remind.detect_questions("see https://example.com/a?b=1 now."), []
        )

    def test_blockquote_line_is_stripped(self):
        self.assertEqual(_remind.detect_questions("> is this quoted?"), [])

    def test_overlapping_tests_yield_one_entry(self):
        # '?'-terminated AND WH-led -> still exactly one entry for the sentence.
        self.assertEqual(_remind.detect_questions("why is it slow?"),
                         ["why is it slow?"])

    def test_two_distinct_questions_counted_separately(self):
        got = _remind.detect_questions("Is it done? Should we merge this?")
        self.assertEqual(got, ["Is it done?", "Should we merge this?"])

    def test_empty_prompt_yields_nothing(self):
        self.assertEqual(_remind.detect_questions(""), [])


if __name__ == "__main__":
    unittest.main()
````

- [ ] Run `cd plugins/question-guard && python3 -m unittest discover -s tests -v`. Expected FAILURE: `ModuleNotFoundError: No module named '_remind'` → `FAILED (errors=1)`.
- [ ] Create `plugins/question-guard/scripts/_remind.py` with exactly (this is the FULL detector implementation):

````python
#!/usr/bin/env python3
"""Question-guard core (detection): find question sentences in a prompt.

Written for Python 3.14+. The sibling entry point ``remind_questions.py`` guards
the interpreter version and imports this module lazily, so this file may use any
3.14 syntax. Standard library only, no network.

Detection is lever 1: every pattern set and cap is a named constant in the block
below -- the tuning surface. Reminder composition and IO are added in later tasks.
"""

import re
import string

# ---- Detection levers (lever 1) -----------------------------------------
# Only the first this-many characters of the prompt are scanned (latency guard).
INPUT_SCAN_CAP = 20_000

# A sentence whose first word is one of these AND that has >= 2 words is a
# question (interrogative lead), even without a trailing '?'.
WH_LEADS = frozenset(
    {"who", "what", "when", "where", "why", "how", "which", "whose", "whom"}
)

# A sentence whose first word is one of these is a question ONLY when its second
# word is a pronoun (see PRONOUN_NEXT) -- this blocks imperatives like
# "do the refactor" from matching.
AUX_LEADS = frozenset(
    {
        "should", "could", "would", "can", "will", "shall", "may", "might",
        "do", "does", "did", "is", "are", "am", "was", "were", "have", "has",
        "had",
    }
)

# Second-word gate for AUX_LEADS. "the" is deliberately NOT here, so an
# aux-led imperative such as "do the refactor" is never misread as a question.
PRONOUN_NEXT = frozenset(
    {
        "i", "we", "you", "it", "they", "he", "she", "this", "that", "these",
        "those", "there", "anyone", "anybody", "someone", "somebody", "one",
    }
)

# A '?'-less sentence ending with one of these tags is a (tag) question.
TAG_ENDINGS = (", right", ", no", ", correct", ", yeah")

_STRIP = string.punctuation
# A WH-led or aux-led sentence must have at least this many words to be a
# question (blocks bare "Why." from matching the interrogative-lead tests).
_MIN_LEAD_WORDS = 2


# ---- Sanitize + split ---------------------------------------------------
def _sanitize(text):
    """Strip material that is not the user's own asking, in a fixed order.

    Order is load-bearing: fenced blocks are removed before inline-code spans so
    a triple-backtick fence is not mangled by single-backtick removal.
    """
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)  # 1. fenced blocks
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.DOTALL)  # 1. fenced blocks
    text = re.sub(r"`[^`]*`", " ", text)  # 2. inline code spans
    text = re.sub(r"\bhttps?://\S+", " ", text)  # 3. URLs
    return re.sub(r"(?m)^[ \t]*>.*$", " ", text)  # 4. blockquote lines


def _split_sentences(text):
    """Split on newlines, then within a line on a run of .?! + whitespace/end.

    The terminator run stays attached to the sentence it ends.
    """
    out = []
    for line in text.split("\n"):
        for part in re.split(r"(?<=[.?!])\s+", line):
            stripped = part.strip()
            if stripped:
                out.append(stripped)
    return out


def _tokens(sentence):
    """Lowercased word tokens with surrounding punctuation stripped."""
    out = []
    for raw in sentence.lower().split():
        word = raw.strip(_STRIP)
        if word:
            out.append(word)
    return out


# ---- Classification -----------------------------------------------------
def _is_question(sentence):
    """True if the sentence matches any of the four question tests (OR-combined)."""
    s = sentence.strip()
    if not s:
        return False
    if s.endswith("?"):  # (a) '?'-terminated
        return True
    toks = _tokens(s)
    if toks:
        first = toks[0]
        enough = len(toks) >= _MIN_LEAD_WORDS
        if first in WH_LEADS and enough:  # (b) WH lead, >= 2 words
            return True
        if first in AUX_LEADS and enough and toks[1] in PRONOUN_NEXT:
            return True  # (c) aux lead + pronoun second word
    tail = s.lower().rstrip(" \t.!")
    return any(tail.endswith(t) for t in TAG_ENDINGS)  # (d) '?'-less tag question


def detect_questions(text):
    """Order-preserving list of detected question sentences (one entry each).

    A sentence is appended at most once even when several tests match it, so the
    list length is the count of distinct detected question sentences.
    """
    return [s for s in _split_sentences(_sanitize(text)) if _is_question(s)]
````

- [ ] Re-run `cd plugins/question-guard && python3 -m unittest discover -s tests -v`. Expected: `Ran 15 tests` … `OK`.
- [ ] Run the shared ruff command from repo root. Expected: `All checks passed!`.
- [ ] Commit:

```bash
git add plugins/question-guard/scripts/_remind.py plugins/question-guard/tests/test_detector.py
git commit -m "feat(question-guard): add question detector core with tests" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GsMUs3eWHPku9ss86owvuH"
```

```json:metadata
{"files": ["plugins/question-guard/scripts/_remind.py", "plugins/question-guard/tests/test_detector.py"], "verifyCommand": "cd plugins/question-guard && python3 -m unittest discover -s tests -v", "acceptanceCriteria": ["detect_questions returns the '?'-terminated sentence for 'Is the build green?'", "WH lead without '?' ('why is the cache cold') is detected", "aux+pronoun ('can you clean this up?', 'do we have tests') detected; aux imperative ('do the refactor', 'fix the bug then run tests') not detected", "tag question without '?' ('we ship today, right') detected", "'?' inside fenced code / inline code / URL / blockquote does not fire", "overlapping tests yield one entry; two distinct questions yield two; empty prompt yields none", "all 15 tests pass and the module is ruff-clean"], "modelTier": "complex"}
```

---

### Task 3: Reminder composition + directive detection + tests

**Goal:** Add directive detection and reminder composition (PURE/MIXED variant selection, ≤5 quotes each hard-sliced to 200 chars, true-total count reporting, the 3500-char backstop that never trims template text) to `_remind.py`, proven by an extended `test_detector.py`.

**Files:**
- Modify `plugins/question-guard/scripts/_remind.py` (add `IMPERATIVE_LEADS`, `MAX_QUOTED_QUESTIONS`, `MAX_QUOTE_CHARS`, `MAX_REMINDER_CHARS`, `PURE_TEMPLATE`, `MIXED_TEMPLATE`, `_is_directive`, `detect_directives`, `_assemble`, `compose_reminder`)
- Modify (Test) `plugins/question-guard/tests/test_detector.py` (add `DetectDirectivesTests` and `ComposeReminderTests`)

**Acceptance Criteria:**
- [ ] `detect_directives` is `True` for `"add a test"`, `"please add a test"`, `"let's ship it"`; `False` for `"is it done?"`.
- [ ] `compose_reminder(["Is it done?"], False)` uses the PURE template (`"Questions are questions."`), reports `"contains 1 question(s)"`, and numbers the quote `"1. Is it done?"`.
- [ ] `compose_reminder([...], True)` uses the MIXED template (`"mixes questions with directives"`, `"act only on what is explicitly directed."`).
- [ ] With 7 questions the reminder quotes only the first 5 (`"5. "` present, `"6. "` absent) but reports `"contains 7 question(s)"`.
- [ ] A 300-char question is hard-sliced to `MAX_QUOTE_CHARS` (200) with no ellipsis character.
- [ ] With 20 long questions the reminder length is ≤ `MAX_REMINDER_CHARS` and the template instruction text survives.
- [ ] All 24 tests pass and the module is ruff-clean.

**Verify:**
```bash
cd plugins/question-guard && python3 -m unittest discover -s tests -v
```
Expected: `Ran 24 tests` … `OK`. Then the shared ruff command → `All checks passed!`.

**Steps:**
- [ ] Overwrite `plugins/question-guard/tests/test_detector.py` with exactly (adds the two new classes; FULL file):

````python
#!/usr/bin/env python3
"""Detector + composition tests for question-guard's core module.

These exercise ``_remind`` in-process (no subprocess): sentence detection under
the four question tests, sanitisation of pasted/quoted material, directive
detection for variant selection, and reminder composition (quoting, truncation,
count reporting, the length clamp).

stdlib only; python3 >= 3.14.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import _remind  # noqa: E402


class DetectQuestionsTests(unittest.TestCase):
    """The four OR-combined question tests, sanitisation, and de-duplication."""

    def test_question_mark_terminated(self):
        self.assertEqual(_remind.detect_questions("Is the build green?"),
                         ["Is the build green?"])

    def test_wh_lead_without_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("why is the cache cold")), 1)

    def test_aux_pronoun_with_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("can you clean this up?")), 1)

    def test_aux_pronoun_without_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("do we have tests")), 1)

    def test_imperative_aux_lead_is_not_a_question(self):
        self.assertEqual(_remind.detect_questions("do the refactor"), [])

    def test_plain_imperative_is_not_a_question(self):
        self.assertEqual(_remind.detect_questions("fix the bug then run tests"), [])

    def test_tag_question_without_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("we ship today, right")), 1)

    def test_wh_single_word_is_not_a_question(self):
        self.assertEqual(_remind.detect_questions("Why."), [])

    def test_question_mark_in_fenced_code_does_not_fire(self):
        self.assertEqual(_remind.detect_questions("```\nis this real?\n```"), [])

    def test_question_in_inline_code_ternary_does_not_fire(self):
        self.assertEqual(_remind.detect_questions("use `cond ? a : b` here."), [])

    def test_question_mark_in_url_does_not_fire(self):
        self.assertEqual(
            _remind.detect_questions("see https://example.com/a?b=1 now."), []
        )

    def test_blockquote_line_is_stripped(self):
        self.assertEqual(_remind.detect_questions("> is this quoted?"), [])

    def test_overlapping_tests_yield_one_entry(self):
        # '?'-terminated AND WH-led -> still exactly one entry for the sentence.
        self.assertEqual(_remind.detect_questions("why is it slow?"),
                         ["why is it slow?"])

    def test_two_distinct_questions_counted_separately(self):
        got = _remind.detect_questions("Is it done? Should we merge this?")
        self.assertEqual(got, ["Is it done?", "Should we merge this?"])

    def test_empty_prompt_yields_nothing(self):
        self.assertEqual(_remind.detect_questions(""), [])


class DetectDirectivesTests(unittest.TestCase):
    """Directive detection -- used only to select the reminder variant."""

    def test_imperative_lead_is_a_directive(self):
        self.assertTrue(_remind.detect_directives("add a test"))

    def test_please_prefix_is_a_directive(self):
        self.assertTrue(_remind.detect_directives("please add a test"))

    def test_lets_prefix_is_a_directive(self):
        self.assertTrue(_remind.detect_directives("let's ship it"))

    def test_pure_question_is_not_a_directive(self):
        self.assertFalse(_remind.detect_directives("is it done?"))


class ComposeReminderTests(unittest.TestCase):
    """Reminder composition: variant, quoting, truncation, count, clamp."""

    def test_pure_variant_when_no_directives(self):
        out = _remind.compose_reminder(["Is it done?"], False)
        self.assertIn("Questions are questions.", out)
        self.assertIn("contains 1 question(s)", out)
        self.assertIn("1. Is it done?", out)

    def test_mixed_variant_when_directives(self):
        out = _remind.compose_reminder(["Should we merge this?"], True)
        self.assertIn("mixes questions with directives", out)
        self.assertIn("act only on what is explicitly directed.", out)

    def test_only_first_five_quoted_but_count_is_total(self):
        qs = [f"Question number {i}?" for i in range(1, 8)]  # 7 questions
        out = _remind.compose_reminder(qs, False)
        self.assertIn("contains 7 question(s)", out)
        self.assertIn("5. ", out)
        self.assertNotIn("6. ", out)

    def test_quote_hard_sliced_to_200_chars_no_ellipsis(self):
        long_q = "why " + ("x" * 300) + "?"
        out = _remind.compose_reminder([long_q], False)
        self.assertNotIn("…", out)  # no ellipsis char
        self.assertIn("1. " + long_q[:_remind.MAX_QUOTE_CHARS] + "\n", out)
        self.assertNotIn(long_q[:_remind.MAX_QUOTE_CHARS] + "x", out)

    def test_reminder_stays_within_hard_cap(self):
        qs = ["why " + ("x" * 300) + "?" for _ in range(20)]
        out = _remind.compose_reminder(qs, False)
        self.assertLessEqual(len(out), _remind.MAX_REMINDER_CHARS)
        self.assertIn("Questions are questions.", out)  # template text survives


if __name__ == "__main__":
    unittest.main()
````

- [ ] Run `cd plugins/question-guard && python3 -m unittest discover -s tests -v`. Expected FAILURE: `AttributeError: module '_remind' has no attribute 'detect_directives'` → `FAILED (errors=9)`.
- [ ] Overwrite `plugins/question-guard/scripts/_remind.py` with exactly (detector + directive detection + composition; FULL file):

````python
#!/usr/bin/env python3
"""Question-guard core: detect question sentences and compose the reminder.

Written for Python 3.14+. The sibling entry point ``remind_questions.py`` guards
the interpreter version and imports this module lazily, so this file may use any
3.14 syntax. Standard library only, no network.

All tuning knobs live in the constants block below -- the two levers the design
exposes: (1) detection (the pattern sets and caps) and (2) the reminder (its
templates and quote caps). Edit those constants to tune behaviour; nothing else
needs changing. Stdin IO and the entry hand-off are added in the next task.
"""

import re
import string

# ---- Detection levers (lever 1) -----------------------------------------
# Only the first this-many characters of the prompt are scanned (latency guard).
INPUT_SCAN_CAP = 20_000

# A sentence whose first word is one of these AND that has >= 2 words is a
# question (interrogative lead), even without a trailing '?'.
WH_LEADS = frozenset(
    {"who", "what", "when", "where", "why", "how", "which", "whose", "whom"}
)

# A sentence whose first word is one of these is a question ONLY when its second
# word is a pronoun (see PRONOUN_NEXT) -- this blocks imperatives like
# "do the refactor" from matching.
AUX_LEADS = frozenset(
    {
        "should", "could", "would", "can", "will", "shall", "may", "might",
        "do", "does", "did", "is", "are", "am", "was", "were", "have", "has",
        "had",
    }
)

# Second-word gate for AUX_LEADS. "the" is deliberately NOT here, so an
# aux-led imperative such as "do the refactor" is never misread as a question.
PRONOUN_NEXT = frozenset(
    {
        "i", "we", "you", "it", "they", "he", "she", "this", "that", "these",
        "those", "there", "anyone", "anybody", "someone", "somebody", "one",
    }
)

# A '?'-less sentence ending with one of these tags is a (tag) question.
TAG_ENDINGS = (", right", ", no", ", correct", ", yeah")

# ---- Reminder + directive levers (lever 2) ------------------------------
# A sentence whose first word is one of these (or that starts with "please " /
# "let's ") is a directive. Directive detection ONLY selects the reminder
# variant, so false positives are low-stakes.
IMPERATIVE_LEADS = frozenset(
    {
        "add", "fix", "write", "implement", "update", "remove", "make", "run",
        "create", "change", "refactor", "delete", "rename", "deploy", "install",
        "build", "move", "use", "stop", "start", "revert", "merge", "push",
        "commit", "rebase", "split", "extract", "convert", "migrate", "rewrite",
        "document", "test", "ensure",
    }
)

# At most this many detected questions are quoted in the reminder ...
MAX_QUOTED_QUESTIONS = 5
# ... each hard-sliced to at most this many characters (plain slice, no ellipsis).
MAX_QUOTE_CHARS = 200
# The whole reminder is clamped to this (headroom under the platform's 10k cap).
MAX_REMINDER_CHARS = 3500

# {n} is the TOTAL number of distinct detected questions (may exceed the 5 quoted);
# {quotes} is the numbered quote block. Instruction text is never trimmed.
PURE_TEMPLATE = (
    "The user's latest message contains {n} question(s):\n"
    "{quotes}\n"
    "Questions are questions. Answer each one directly. A question NEVER "
    "authorizes action by itself. If a question reads like a request to act "
    "(e.g. 'can you clean this up?'), name the action a directive would trigger "
    "and stop there — do not perform it."
)
MIXED_TEMPLATE = (
    "The user's latest message mixes questions with directives. The question(s):\n"
    "{quotes}\n"
    "Answer EVERY question individually AND carry out the directives. The "
    "questions themselves add no scope: act only on what is explicitly directed."
)

_STRIP = string.punctuation
# A WH-led or aux-led sentence must have at least this many words to be a
# question (blocks bare "Why." from matching the interrogative-lead tests).
_MIN_LEAD_WORDS = 2


# ---- Sanitize + split ---------------------------------------------------
def _sanitize(text):
    """Strip material that is not the user's own asking, in a fixed order.

    Order is load-bearing: fenced blocks are removed before inline-code spans so
    a triple-backtick fence is not mangled by single-backtick removal.
    """
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)  # 1. fenced blocks
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.DOTALL)  # 1. fenced blocks
    text = re.sub(r"`[^`]*`", " ", text)  # 2. inline code spans
    text = re.sub(r"\bhttps?://\S+", " ", text)  # 3. URLs
    return re.sub(r"(?m)^[ \t]*>.*$", " ", text)  # 4. blockquote lines


def _split_sentences(text):
    """Split on newlines, then within a line on a run of .?! + whitespace/end.

    The terminator run stays attached to the sentence it ends.
    """
    out = []
    for line in text.split("\n"):
        for part in re.split(r"(?<=[.?!])\s+", line):
            stripped = part.strip()
            if stripped:
                out.append(stripped)
    return out


def _tokens(sentence):
    """Lowercased word tokens with surrounding punctuation stripped."""
    out = []
    for raw in sentence.lower().split():
        word = raw.strip(_STRIP)
        if word:
            out.append(word)
    return out


# ---- Classification -----------------------------------------------------
def _is_question(sentence):
    """True if the sentence matches any of the four question tests (OR-combined)."""
    s = sentence.strip()
    if not s:
        return False
    if s.endswith("?"):  # (a) '?'-terminated
        return True
    toks = _tokens(s)
    if toks:
        first = toks[0]
        enough = len(toks) >= _MIN_LEAD_WORDS
        if first in WH_LEADS and enough:  # (b) WH lead, >= 2 words
            return True
        if first in AUX_LEADS and enough and toks[1] in PRONOUN_NEXT:
            return True  # (c) aux lead + pronoun second word
    tail = s.lower().rstrip(" \t.!")
    return any(tail.endswith(t) for t in TAG_ENDINGS)  # (d) '?'-less tag question


def _is_directive(sentence):
    """True if the sentence is an imperative / "please " / "let's " directive."""
    s = sentence.strip()
    if not s:
        return False
    low = s.lower()
    if low.startswith("please ") or low.startswith("let's "):
        return True
    toks = _tokens(s)
    return bool(toks) and toks[0] in IMPERATIVE_LEADS


def detect_questions(text):
    """Order-preserving list of detected question sentences (one entry each).

    A sentence is appended at most once even when several tests match it, so the
    list length is the count of distinct detected question sentences.
    """
    return [s for s in _split_sentences(_sanitize(text)) if _is_question(s)]


def detect_directives(text):
    """True if any sanitized sentence looks like a directive."""
    return any(_is_directive(s) for s in _split_sentences(_sanitize(text)))


# ---- Reminder composition -----------------------------------------------
def _assemble(template, n, quotes):
    """Fill a template with the count and a numbered quote block."""
    block = "\n".join(f"{i}. {q}" for i, q in enumerate(quotes, 1))
    return template.format(n=n, quotes=block)


def compose_reminder(questions, has_directives):
    """Build the reminder text from detected questions + the directive flag.

    ``n`` is the true total number of detected questions even when more than
    ``MAX_QUOTED_QUESTIONS`` are present; only the first few are quoted, each
    hard-sliced to ``MAX_QUOTE_CHARS``. The final clamp drops trailing quotes,
    then (defensively) hard-slices the quote block -- never the template text.
    """
    n = len(questions)
    template = MIXED_TEMPLATE if has_directives else PURE_TEMPLATE
    quotes = [q[:MAX_QUOTE_CHARS] for q in questions[:MAX_QUOTED_QUESTIONS]]
    reminder = _assemble(template, n, quotes)
    while len(reminder) > MAX_REMINDER_CHARS and quotes:
        quotes.pop()
        reminder = _assemble(template, n, quotes)
    if len(reminder) > MAX_REMINDER_CHARS:
        overhead = len(_assemble(template, n, []))
        budget = max(0, MAX_REMINDER_CHARS - overhead)
        block = "\n".join(f"{i}. {q}" for i, q in enumerate(quotes, 1))
        reminder = template.format(n=n, quotes=block[:budget])
    return reminder
````

- [ ] Re-run `cd plugins/question-guard && python3 -m unittest discover -s tests -v`. Expected: `Ran 24 tests` … `OK`.
- [ ] Run the shared ruff command. Expected: `All checks passed!`.
- [ ] Commit:

```bash
git add plugins/question-guard/scripts/_remind.py plugins/question-guard/tests/test_detector.py
git commit -m "feat(question-guard): add reminder composition and directive detection" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GsMUs3eWHPku9ss86owvuH"
```

```json:metadata
{"files": ["plugins/question-guard/scripts/_remind.py", "plugins/question-guard/tests/test_detector.py"], "verifyCommand": "cd plugins/question-guard && python3 -m unittest discover -s tests -v", "acceptanceCriteria": ["detect_directives is True for 'add a test', 'please add a test', 'let's ship it'; False for 'is it done?'", "compose_reminder uses the PURE template with count and numbered quote when no directives", "compose_reminder uses the MIXED template when directives are present", "with 7 questions only the first 5 are quoted but the count reports 7", "a 300-char question is hard-sliced to 200 chars with no ellipsis", "with 20 long questions the reminder is <= MAX_REMINDER_CHARS and template text survives", "all 24 tests pass and the module is ruff-clean"], "modelTier": "complex"}
```

---

### Task 4: IO + entry + tests

**Goal:** Add stdin JSON handling, the visible/quiet delivery split, and a fail-open `main()` to `_remind.py`, plus the `remind_questions.py` interpreter-guard entry, proven by `test_output.py` driving the real entry script as a subprocess.

**Files:**
- Modify `plugins/question-guard/scripts/_remind.py` (add `json`/`os`/`sys` imports, `_emit`, `main`, `__main__` guard)
- Create `plugins/question-guard/scripts/remind_questions.py`
- Create (Test) `plugins/question-guard/tests/test_output.py`

**Acceptance Criteria:**
- [ ] Quiet default: the entry emits `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": <reminder>}}` on stdout.
- [ ] `QUESTION_GUARD_VISIBLE` in `{1, true, yes, on}` (case-insensitive, surrounding whitespace ignored) → plain-text stdout with no JSON envelope; any other/empty value → quiet JSON.
- [ ] Silent exit 0 (no stdout, no stderr) on: no questions, malformed JSON stdin, missing `prompt` key.
- [ ] A question that appears only after the first `INPUT_SCAN_CAP` characters is not detected.
- [ ] The entry always exits 0 and writes no stderr.
- [ ] All 32 tests pass and both scripts are ruff-clean.

**Verify:**
```bash
cd plugins/question-guard && python3 -m unittest discover -s tests -v
```
Expected: `Ran 32 tests` … `OK`. Then (from repo root):
```bash
echo '{"prompt":"why is the cache cold"}' | QUESTION_GUARD_VISIBLE=1 python3 plugins/question-guard/scripts/remind_questions.py
```
Expected: plain text beginning `The user's latest message contains 1 question(s):`. Then the shared ruff command → `All checks passed!`.

**Steps:**
- [ ] Create `plugins/question-guard/tests/test_output.py` with exactly (FULL failing test):

````python
#!/usr/bin/env python3
"""IO + entry-point tests for question-guard.

These drive the real entry script (``scripts/remind_questions.py``) as a
subprocess with a JSON payload on stdin -- the exact contract Claude Code uses --
and assert the two delivery modes (quiet ``additionalContext`` JSON vs. visible
plain stdout), the env-var truthiness parsing, the input scan cap, and the
fail-open silences. The hook must ALWAYS exit 0 and never write stderr.

stdlib only; python3 >= 3.14 (the runner).
"""

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.abspath(os.path.join(HERE, "..", "scripts", "remind_questions.py"))


def run_entry(stdin_text, env=None):
    """Run the entry script once; return the CompletedProcess (exit asserted 0)."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, ENTRY],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )
    assert proc.returncode == 0, (
        f"hook must always exit 0, got {proc.returncode!r} (stderr={proc.stderr!r})"
    )
    return proc


def payload(prompt):
    """A UserPromptSubmit stdin payload carrying ``prompt``."""
    return json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
    )


class QuietModeTests(unittest.TestCase):
    """Default (quiet) delivery emits the additionalContext JSON envelope."""

    def test_additional_context_shape(self):
        proc = run_entry(payload("Is the build green?"))
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertIn("Questions are questions.", out["additionalContext"])
        self.assertEqual(proc.stderr, "")

    def test_quiet_when_visible_is_falsey(self):
        for value in ("0", "false", "nope", ""):
            with self.subTest(value=value):
                proc = run_entry(
                    payload("Is it done?"), env={"QUESTION_GUARD_VISIBLE": value}
                )
                self.assertIn("hookSpecificOutput", proc.stdout)


class VisibleModeTests(unittest.TestCase):
    """QUESTION_GUARD_VISIBLE truthy -> plain-text stdout, no JSON envelope."""

    def test_visible_plain_stdout(self):
        proc = run_entry(
            payload("Is it done?"), env={"QUESTION_GUARD_VISIBLE": "1"}
        )
        self.assertTrue(proc.stdout.startswith("The user's latest message"))
        self.assertNotIn("hookSpecificOutput", proc.stdout)

    def test_visible_truthy_values_parsed(self):
        for value in ("1", "true", "yes", "on", "  TRUE  ", "Yes"):
            with self.subTest(value=value):
                proc = run_entry(
                    payload("Is it done?"), env={"QUESTION_GUARD_VISIBLE": value}
                )
                self.assertNotIn("hookSpecificOutput", proc.stdout)
                self.assertTrue(proc.stdout.startswith("The user's latest message"))


class FailOpenTests(unittest.TestCase):
    """Every non-question / malformed input path is silent with exit 0."""

    def test_silent_on_no_questions(self):
        proc = run_entry(payload("add a test."))
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.stderr, "")

    def test_silent_on_malformed_json(self):
        proc = run_entry("this is not json")
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.stderr, "")

    def test_silent_on_missing_prompt_key(self):
        proc = run_entry(json.dumps({"hook_event_name": "UserPromptSubmit"}))
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.stderr, "")

    def test_scan_cap_ignores_question_beyond_20k(self):
        prompt = ("x" * _remind_scan_cap()) + " is this counted?"
        proc = run_entry(payload(prompt))
        self.assertEqual(proc.stdout.strip(), "")


def _remind_scan_cap():
    """Read INPUT_SCAN_CAP straight from the core module, no literal duplicated."""
    scripts = os.path.abspath(os.path.join(HERE, "..", "scripts"))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import _remind  # noqa: PLC0415

    return _remind.INPUT_SCAN_CAP


if __name__ == "__main__":
    unittest.main()
````

- [ ] Run `cd plugins/question-guard && python3 -m unittest discover -s tests -v`. Expected FAILURE: the `test_output` cases raise `AssertionError: hook must always exit 0, got 2 (... No such file or directory ... remind_questions.py)` because the entry script does not exist yet.
- [ ] Overwrite `plugins/question-guard/scripts/_remind.py` with exactly (adds `json`/`os`/`sys`, `_emit`, `main`, `__main__`; FULL final file):

````python
#!/usr/bin/env python3
"""Question-guard core: detect question sentences and compose the reminder.

Written for Python 3.14+. The sibling entry point ``remind_questions.py`` guards
the interpreter version and imports this module lazily, so this file may use any
3.14 syntax. Standard library only, no network.

All tuning knobs live in the constants block below -- the two levers the design
exposes: (1) detection (the pattern sets and caps) and (2) the reminder (its
templates and quote caps). Edit those constants to tune behaviour; nothing else
needs changing.

Failure policy is fail-OPEN: ``main`` swallows every error and stays silent, so a
malformed prompt or an internal bug never blocks or erases the user's prompt.
"""

import json
import os
import re
import string
import sys

# ---- Detection levers (lever 1) -----------------------------------------
# Only the first this-many characters of the prompt are scanned (latency guard).
INPUT_SCAN_CAP = 20_000

# A sentence whose first word is one of these AND that has >= 2 words is a
# question (interrogative lead), even without a trailing '?'.
WH_LEADS = frozenset(
    {"who", "what", "when", "where", "why", "how", "which", "whose", "whom"}
)

# A sentence whose first word is one of these is a question ONLY when its second
# word is a pronoun (see PRONOUN_NEXT) -- this blocks imperatives like
# "do the refactor" from matching.
AUX_LEADS = frozenset(
    {
        "should", "could", "would", "can", "will", "shall", "may", "might",
        "do", "does", "did", "is", "are", "am", "was", "were", "have", "has",
        "had",
    }
)

# Second-word gate for AUX_LEADS. "the" is deliberately NOT here, so an
# aux-led imperative such as "do the refactor" is never misread as a question.
PRONOUN_NEXT = frozenset(
    {
        "i", "we", "you", "it", "they", "he", "she", "this", "that", "these",
        "those", "there", "anyone", "anybody", "someone", "somebody", "one",
    }
)

# A '?'-less sentence ending with one of these tags is a (tag) question.
TAG_ENDINGS = (", right", ", no", ", correct", ", yeah")

# ---- Reminder + directive levers (lever 2) ------------------------------
# A sentence whose first word is one of these (or that starts with "please " /
# "let's ") is a directive. Directive detection ONLY selects the reminder
# variant, so false positives are low-stakes.
IMPERATIVE_LEADS = frozenset(
    {
        "add", "fix", "write", "implement", "update", "remove", "make", "run",
        "create", "change", "refactor", "delete", "rename", "deploy", "install",
        "build", "move", "use", "stop", "start", "revert", "merge", "push",
        "commit", "rebase", "split", "extract", "convert", "migrate", "rewrite",
        "document", "test", "ensure",
    }
)

# At most this many detected questions are quoted in the reminder ...
MAX_QUOTED_QUESTIONS = 5
# ... each hard-sliced to at most this many characters (plain slice, no ellipsis).
MAX_QUOTE_CHARS = 200
# The whole reminder is clamped to this (headroom under the platform's 10k cap).
MAX_REMINDER_CHARS = 3500

# {n} is the TOTAL number of distinct detected questions (may exceed the 5 quoted);
# {quotes} is the numbered quote block. Instruction text is never trimmed.
PURE_TEMPLATE = (
    "The user's latest message contains {n} question(s):\n"
    "{quotes}\n"
    "Questions are questions. Answer each one directly. A question NEVER "
    "authorizes action by itself. If a question reads like a request to act "
    "(e.g. 'can you clean this up?'), name the action a directive would trigger "
    "and stop there — do not perform it."
)
MIXED_TEMPLATE = (
    "The user's latest message mixes questions with directives. The question(s):\n"
    "{quotes}\n"
    "Answer EVERY question individually AND carry out the directives. The "
    "questions themselves add no scope: act only on what is explicitly directed."
)

_STRIP = string.punctuation
# A WH-led or aux-led sentence must have at least this many words to be a
# question (blocks bare "Why." from matching the interrogative-lead tests).
_MIN_LEAD_WORDS = 2


# ---- Sanitize + split ---------------------------------------------------
def _sanitize(text):
    """Strip material that is not the user's own asking, in a fixed order.

    Order is load-bearing: fenced blocks are removed before inline-code spans so
    a triple-backtick fence is not mangled by single-backtick removal.
    """
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)  # 1. fenced blocks
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.DOTALL)  # 1. fenced blocks
    text = re.sub(r"`[^`]*`", " ", text)  # 2. inline code spans
    text = re.sub(r"\bhttps?://\S+", " ", text)  # 3. URLs
    return re.sub(r"(?m)^[ \t]*>.*$", " ", text)  # 4. blockquote lines


def _split_sentences(text):
    """Split on newlines, then within a line on a run of .?! + whitespace/end.

    The terminator run stays attached to the sentence it ends.
    """
    out = []
    for line in text.split("\n"):
        for part in re.split(r"(?<=[.?!])\s+", line):
            stripped = part.strip()
            if stripped:
                out.append(stripped)
    return out


def _tokens(sentence):
    """Lowercased word tokens with surrounding punctuation stripped."""
    out = []
    for raw in sentence.lower().split():
        word = raw.strip(_STRIP)
        if word:
            out.append(word)
    return out


# ---- Classification -----------------------------------------------------
def _is_question(sentence):
    """True if the sentence matches any of the four question tests (OR-combined)."""
    s = sentence.strip()
    if not s:
        return False
    if s.endswith("?"):  # (a) '?'-terminated
        return True
    toks = _tokens(s)
    if toks:
        first = toks[0]
        enough = len(toks) >= _MIN_LEAD_WORDS
        if first in WH_LEADS and enough:  # (b) WH lead, >= 2 words
            return True
        if first in AUX_LEADS and enough and toks[1] in PRONOUN_NEXT:
            return True  # (c) aux lead + pronoun second word
    tail = s.lower().rstrip(" \t.!")
    return any(tail.endswith(t) for t in TAG_ENDINGS)  # (d) '?'-less tag question


def _is_directive(sentence):
    """True if the sentence is an imperative / "please " / "let's " directive."""
    s = sentence.strip()
    if not s:
        return False
    low = s.lower()
    if low.startswith("please ") or low.startswith("let's "):
        return True
    toks = _tokens(s)
    return bool(toks) and toks[0] in IMPERATIVE_LEADS


def detect_questions(text):
    """Order-preserving list of detected question sentences (one entry each).

    A sentence is appended at most once even when several tests match it, so the
    list length is the count of distinct detected question sentences.
    """
    return [s for s in _split_sentences(_sanitize(text)) if _is_question(s)]


def detect_directives(text):
    """True if any sanitized sentence looks like a directive."""
    return any(_is_directive(s) for s in _split_sentences(_sanitize(text)))


# ---- Reminder composition -----------------------------------------------
def _assemble(template, n, quotes):
    """Fill a template with the count and a numbered quote block."""
    block = "\n".join(f"{i}. {q}" for i, q in enumerate(quotes, 1))
    return template.format(n=n, quotes=block)


def compose_reminder(questions, has_directives):
    """Build the reminder text from detected questions + the directive flag.

    ``n`` is the true total number of detected questions even when more than
    ``MAX_QUOTED_QUESTIONS`` are present; only the first few are quoted, each
    hard-sliced to ``MAX_QUOTE_CHARS``. The final clamp drops trailing quotes,
    then (defensively) hard-slices the quote block -- never the template text.
    """
    n = len(questions)
    template = MIXED_TEMPLATE if has_directives else PURE_TEMPLATE
    quotes = [q[:MAX_QUOTE_CHARS] for q in questions[:MAX_QUOTED_QUESTIONS]]
    reminder = _assemble(template, n, quotes)
    while len(reminder) > MAX_REMINDER_CHARS and quotes:
        quotes.pop()
        reminder = _assemble(template, n, quotes)
    if len(reminder) > MAX_REMINDER_CHARS:
        overhead = len(_assemble(template, n, []))
        budget = max(0, MAX_REMINDER_CHARS - overhead)
        block = "\n".join(f"{i}. {q}" for i, q in enumerate(quotes, 1))
        reminder = template.format(n=n, quotes=block[:budget])
    return reminder


# ---- IO + entry ---------------------------------------------------------
def _emit(reminder):
    """Print the reminder: plain stdout when visible, else a JSON context payload."""
    flag = os.environ.get("QUESTION_GUARD_VISIBLE", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        print(reminder)
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": reminder,
                }
            }
        )
    )


def main():
    """Read the prompt on stdin, emit a reminder if it holds questions.

    Fail-open: any malformed input, missing prompt, or internal error stays
    silent and exits 0. Never exits non-zero, never emits decision:block.
    """
    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict):
            return
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            return
        text = prompt[:INPUT_SCAN_CAP]
        questions = detect_questions(text)
        if not questions:
            return
        _emit(compose_reminder(questions, detect_directives(text)))
    except Exception:
        return


if __name__ == "__main__":
    main()
````

- [ ] Create `plugins/question-guard/scripts/remind_questions.py` with exactly:

````python
#!/usr/bin/env python3
"""UserPromptSubmit entry point for question-guard.

The reminder logic lives in the sibling module ``_remind`` and is written against
Python 3.14+. On an older interpreter that module might not even parse, and a
crashing hook exits non-zero -- so this entry point is kept to syntax every
Python 3 accepts. It checks the interpreter first: too old -> it exits 0 SILENTLY
(fail-OPEN, deliberately the opposite of model-guard's fail-closed guard, because
a missing reminder is harmless while noise or a blocked prompt is not); new enough
-> it imports ``_remind`` lazily, by name, and hands off to its ``main``.

stdlib only. Always exits 0.
"""

import importlib
import sys

_REQUIRED = (3, 14)


def main():
    """Silently no-op on an unsupported interpreter, else run the real hook."""
    if sys.version_info < _REQUIRED:
        return
    # Imported by name so the 3.14-only module is compiled only after the guard
    # above passes (a top-level ``import _remind`` would be compiled eagerly).
    importlib.import_module("_remind").main()


if __name__ == "__main__":
    main()
````

- [ ] Re-run `cd plugins/question-guard && python3 -m unittest discover -s tests -v`. Expected: `Ran 32 tests` … `OK`.
- [ ] Run the visible-mode one-liner from the Verify block; confirm the plain-text reminder appears.
- [ ] Run the shared ruff command. Expected: `All checks passed!`.
- [ ] Commit:

```bash
git add plugins/question-guard/scripts/_remind.py plugins/question-guard/scripts/remind_questions.py plugins/question-guard/tests/test_output.py
git commit -m "feat(question-guard): add stdin IO, entry guard, and output tests" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GsMUs3eWHPku9ss86owvuH"
```

```json:metadata
{"files": ["plugins/question-guard/scripts/_remind.py", "plugins/question-guard/scripts/remind_questions.py", "plugins/question-guard/tests/test_output.py"], "verifyCommand": "cd plugins/question-guard && python3 -m unittest discover -s tests -v", "acceptanceCriteria": ["quiet default emits hookSpecificOutput with hookEventName UserPromptSubmit and additionalContext", "QUESTION_GUARD_VISIBLE in {1,true,yes,on} (case/space-insensitive) yields plain stdout; other/empty yields quiet JSON", "silent exit 0 on no questions, malformed JSON stdin, and missing prompt key", "a question beyond INPUT_SCAN_CAP characters is not detected", "the entry always exits 0 and writes no stderr", "all 32 tests pass and both scripts are ruff-clean"], "modelTier": "standard"}
```

---

### Task 5: Plugin README

**Goal:** Add the plugin README documenting behaviour, the four detection tests, the two reminder variants, the delivery toggle, the configuration levers, install, requirements, and limitations — following model-guard's section pattern.

**Files:**
- Create `plugins/question-guard/README.md`

**Acceptance Criteria:**
- [ ] README documents what it detects (the four question tests) and the two reminder variants (PURE / MIXED).
- [ ] README documents the quiet-default delivery and the `QUESTION_GUARD_VISIBLE` toggle with its truthy values.
- [ ] README documents Configuration (constants at the top of `scripts/_remind.py`) and Install (`claude plugin install question-guard@trackness`).
- [ ] README states Requirements Python 3.14+ with the silent no-op / fail-open note, and a Limitations section (heuristic false pos/neg, invisible additionalContext, the 20k scan cap and the 3500/10k reminder-size headroom, fail-open by design).

**Verify:**
```bash
test -f plugins/question-guard/README.md && grep -q "QUESTION_GUARD_VISIBLE" plugins/question-guard/README.md && grep -q "fail-open" plugins/question-guard/README.md && grep -q "claude plugin install question-guard@trackness" plugins/question-guard/README.md && echo ok
```
Expected: `ok`.

**Steps:**
- [ ] Create `plugins/question-guard/README.md` with exactly:

````markdown
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
````

- [ ] Run the Verify command; confirm it prints `ok`.
- [ ] Commit:

```bash
git add plugins/question-guard/README.md
git commit -m "docs(question-guard): add plugin README" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GsMUs3eWHPku9ss86owvuH"
```

```json:metadata
{"files": ["plugins/question-guard/README.md"], "verifyCommand": "test -f plugins/question-guard/README.md && grep -q \"QUESTION_GUARD_VISIBLE\" plugins/question-guard/README.md && grep -q \"fail-open\" plugins/question-guard/README.md && grep -q \"claude plugin install question-guard@trackness\" plugins/question-guard/README.md && echo ok", "acceptanceCriteria": ["README documents the four detection tests and the PURE/MIXED reminder variants", "README documents quiet-default delivery and the QUESTION_GUARD_VISIBLE toggle with its truthy values", "README documents Configuration (constants in scripts/_remind.py) and the Install command", "README states Python 3.14+ requirement with the fail-open note and a Limitations section (heuristic, invisible additionalContext, 20k scan cap / 3500 vs 10k headroom, fail-open)"], "modelTier": "mechanical"}
```

---

### Task 6: Registration

**Goal:** Register question-guard in the marketplace manifest (relative source, NO version field) and the root README (table row + section) matching the house format.

**Files:**
- Modify `.claude-plugin/marketplace.json`
- Modify `README.md`

**Acceptance Criteria:**
- [ ] `.claude-plugin/marketplace.json` gains a `question-guard` entry with relative `source` `"./plugins/question-guard"` and NO `version` field; the file stays valid JSON with 3 plugins.
- [ ] `README.md` gains a table row for `question-guard` (version `1.0.0`, linking to `plugins/question-guard`) and a `### question-guard` section following the model-guard format (Includes 1 enforcement hook, Requirements Python 3.14+ fail-open).

**Verify:**
```bash
python3 -c "import json; d=json.load(open('.claude-plugin/marketplace.json')); q=[p for p in d['plugins'] if p['name']=='question-guard'][0]; assert q['source']=='./plugins/question-guard'; assert 'version' not in q; print('ok')"
```
Expected: `ok`. Then:
```bash
grep -q "question-guard" README.md && echo ok
```
Expected: `ok`.

**Steps:**
- [ ] Edit `.claude-plugin/marketplace.json`: append this object as the last element of the `plugins` array (add a comma after the current last `model-guard` entry's closing brace):

```json
    {
      "name": "question-guard",
      "description": "UserPromptSubmit hook injecting a reminder that a question requires an answer, never action (questions are not directives)",
      "source": "./plugins/question-guard",
      "author": {
        "name": "trackness",
        "email": "trackness@users.noreply.github.com"
      }
    }
```

- [ ] Edit `README.md`: add this row to the Plugins table immediately after the `model-guard` row:

```markdown
| [`question-guard`](plugins/question-guard)    | 1.0.0   | UserPromptSubmit hook injecting a reminder that a question requires an answer, never action (questions are not directives) | [plugins/question-guard](plugins/question-guard) |
```

- [ ] Edit `README.md`: add this section immediately after the existing `### model-guard` section (after its Requirements bullet):

```markdown
### question-guard

Detects question sentences in each submitted prompt and injects a reminder that a question must be answered and never authorizes action by itself — so a question phrased like a request ("can you clean this up?") is answered, not acted on.

**Includes:**
- **1 enforcement hook** — `UserPromptSubmit` hook that detects question sentences and injects a reminder into Claude's context

**Requirements:**
- Python 3.14+ (standard library only). On an older `python3` the hook silently does nothing — fail-open by design, since a missing reminder is harmless.
```

- [ ] Run both Verify commands; confirm each prints `ok`.
- [ ] Commit:

```bash
git add .claude-plugin/marketplace.json README.md
git commit -m "feat: register question-guard in marketplace and root README" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GsMUs3eWHPku9ss86owvuH"
```

```json:metadata
{"files": [".claude-plugin/marketplace.json", "README.md"], "verifyCommand": "python3 -c \"import json; d=json.load(open('.claude-plugin/marketplace.json')); q=[p for p in d['plugins'] if p['name']=='question-guard'][0]; assert q['source']=='./plugins/question-guard'; assert 'version' not in q; print('ok')\"", "acceptanceCriteria": ["marketplace.json gains a question-guard entry with relative source ./plugins/question-guard and no version field; file stays valid JSON with 3 plugins", "README.md gains a question-guard table row (version 1.0.0) linking to plugins/question-guard", "README.md gains a ### question-guard section matching the model-guard format (Includes 1 enforcement hook, Requirements Python 3.14+ fail-open)"], "modelTier": "mechanical"}
```

---

### Task 7: End-to-end verification + push + PR

**Goal:** Run the full test suite, ruff, plugin validation, and a live in-session probe end-to-end, then push the branch and open exactly one pull request.

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation.

**Files:**
- None (verification, then `git push` + `gh pr create`)

**Acceptance Criteria:**
- [ ] `python3 -m unittest discover -s plugins/question-guard/tests -v` reports all 32 tests `OK`.
- [ ] The shared ruff command reports `All checks passed!` on `scripts` and `tests`.
- [ ] `claude plugin validate ./plugins/question-guard` reports `✔ Validation passed`.
- [ ] A live `claude --plugin-dir plugins/question-guard` session with `QUESTION_GUARD_VISIBLE=1` shows the reminder for a question prompt; quiet-mode injection is confirmed via `claude --debug-file`.
- [ ] The branch is pushed to origin and exactly one PR is open, titled `feat: add question-guard plugin to marketplace`.

**Verify:**
```bash
python3 -m unittest discover -s plugins/question-guard/tests -v
```
Expected: `Ran 32 tests` … `OK`. Then, after push:
```bash
gh pr list --head feat/question-guard-plugin
```
Expected: exactly one open PR listed.

**Steps:**
- [ ] Run `python3 -m unittest discover -s plugins/question-guard/tests -v` from the repo root; confirm `Ran 32 tests` … `OK`.
- [ ] Run the shared ruff command; confirm `All checks passed!`.
- [ ] Run `claude plugin validate ./plugins/question-guard`; confirm `✔ Validation passed`.
- [ ] Live probe (visible mode): start `claude --plugin-dir plugins/question-guard`, run `/hooks` to confirm the `UserPromptSubmit` hook is registered, then in a shell set `QUESTION_GUARD_VISIBLE=1` and submit a prompt such as `can you clean this up?`; observe the plain-text reminder. Then confirm quiet-mode injection by launching with `claude --debug-file <path>` and checking the debug log shows the `additionalContext` payload for the same prompt.
- [ ] Push the branch:

```bash
git push -u origin feat/question-guard-plugin
```

- [ ] Open exactly one PR (body ends with the two required footer lines):

```bash
gh pr create --title "feat: add question-guard plugin to marketplace" --body "$(cat <<'EOF'
Adds the **question-guard** plugin: a `UserPromptSubmit` hook that detects question sentences in each submitted prompt and injects a reminder that a question must be answered and NEVER authorizes action by itself (questions are not directives).

## What it does
- Sanitizes the prompt (fenced code, inline code, URLs, blockquotes), splits into sentences, and classifies each with a sentence heuristic: `?`-terminated, WH-lead, aux+pronoun, or `?`-less tag question.
- Composes an adaptive reminder quoting the detected questions, with a PURE variant (questions only) and a MIXED variant (questions + directives).
- Delivers quietly as `additionalContext` JSON by default; `QUESTION_GUARD_VISIBLE` prints it as plain stdout for tuning.

## Levers (constants at the top of `scripts/_remind.py`)
- **Detection:** `WH_LEADS`, `AUX_LEADS`, `PRONOUN_NEXT`, `TAG_ENDINGS`, `IMPERATIVE_LEADS`, `INPUT_SCAN_CAP`.
- **Reminder:** `PURE_TEMPLATE`, `MIXED_TEMPLATE`, `MAX_QUOTED_QUESTIONS`, `MAX_QUOTE_CHARS`, `MAX_REMINDER_CHARS`.

## Safety
- Fail-open everywhere: malformed input, a missing prompt, an old (<3.14) interpreter, or any internal error produces no output and exit 0. Never exit 2, never `decision: block`.

## Tests
- `python3 -m unittest discover -s plugins/question-guard/tests -v` → 32 tests, all passing; ruff clean; `claude plugin validate` passes.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01GsMUs3eWHPku9ss86owvuH
EOF
)"
```

- [ ] Confirm exactly one PR is open with `gh pr list --head feat/question-guard-plugin`.

```json:metadata
{"files": [], "verifyCommand": "python3 -m unittest discover -s plugins/question-guard/tests -v", "acceptanceCriteria": ["python3 -m unittest discover -s plugins/question-guard/tests -v reports all 32 tests OK", "ruff reports All checks passed on scripts and tests", "claude plugin validate ./plugins/question-guard reports validation passed", "a live claude --plugin-dir session with QUESTION_GUARD_VISIBLE=1 shows the reminder; quiet injection confirmed via claude --debug-file", "the branch is pushed and exactly one PR is open titled 'feat: add question-guard plugin to marketplace'"], "modelTier": "standard", "userGate": true, "tags": ["user-gate"]}
```

---

## Lifecycle note (NON-EXECUTABLE — do NOT run during plan execution)

This is **not** a task and has **no checkbox**. It records the post-merge lifecycle step only.

After the user approves the PR **and** gives explicit merge say-so — never during plan execution, and never on plan/PR approval alone — a final commit removes the superpowers docs so neither the spec nor this plan survives the squash-merge onto `main`:

```bash
git rm -r docs/superpowers
git commit -m "chore: remove superpowers docs before merge" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GsMUs3eWHPku9ss86owvuH"
```

Only then is the branch squash-merged. Squashing happens after PR approval; merging happens only on the user's explicit say-so to merge (approval of the plan, the code, the approach, or the PR itself is never say-so to merge).
