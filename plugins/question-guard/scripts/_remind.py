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
