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
    hard-sliced to ``MAX_QUOTE_CHARS``. The final clamp drops trailing quotes
    down to a single remaining one, then (defensively) hard-slices that last
    quote's block -- never the template text. This never fires under the
    shipped constants (max possible output is well under the 3500-char cap);
    it only engages if ``MAX_REMINDER_CHARS`` were tuned far smaller.
    """
    n = len(questions)
    template = MIXED_TEMPLATE if has_directives else PURE_TEMPLATE
    quotes = [q[:MAX_QUOTE_CHARS] for q in questions[:MAX_QUOTED_QUESTIONS]]
    reminder = _assemble(template, n, quotes)
    while len(reminder) > MAX_REMINDER_CHARS and len(quotes) > 1:
        quotes.pop()
        reminder = _assemble(template, n, quotes)
    if len(reminder) > MAX_REMINDER_CHARS and quotes:
        overhead = len(_assemble(template, n, []))
        budget = max(0, MAX_REMINDER_CHARS - overhead)
        block = f"1. {quotes[0]}"
        reminder = template.format(n=n, quotes=block[:budget])
    return reminder
